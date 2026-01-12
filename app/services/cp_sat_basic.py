from datetime import date, datetime, timedelta
import time
import numpy as np
from typing import List, Dict, Optional, Tuple
from db.roster_config import NurseRosterConfig
from db.nurse_config import Nurse
from services.roster_system import RosterSystem
import numpy as np
from collections import defaultdict
import random
from services.constraints.grade_constraints import add_grade_constraints
from services.objectives.team_objective import add_team_balance_objective_terms
# ─────────────────────────────  RL Neighborhood  ─────────────────────────
class RLNeighborhoodPolicy:
    """아주 가벼운 ε-greedy 정책"""
    def __init__(self, N, D, eps0=0.3, eps_end=0.05, decay=0.995):
        self.N, self.D = N, D
        self.eps, self.eps_end, self.decay = eps0, eps_end, decay
        self.n_w, self.d_w = np.ones(N), np.ones(D)

    def select(self, k_n=4, k_d=7):
        if random.random() < self.eps:                          # explore
            n_sel = random.sample(range(self.N), k=min(k_n,self.N))
            d_sel = random.sample(range(self.D), k=min(k_d,self.D))
        else:                                                   # exploit
            n_sel = list(np.random.choice(self.N,k_n,replace=False,
                                           p=self.n_w/self.n_w.sum()))
            d_sel = list(np.random.choice(self.D,k_d,replace=False,
                                           p=self.d_w/self.d_w.sum()))
        self.eps = max(self.eps_end, self.eps*self.decay)
        return n_sel, d_sel

    def update(self, ok: bool, n_sel, d_sel):
        delta = 2.0 if ok else -1.0
        self.n_w[n_sel] += delta;  self.n_w = np.clip(self.n_w, .1, None)
        self.d_w[d_sel] += delta;  self.d_w = np.clip(self.d_w, .1, None)


# ───────────────────────────────  Timer  ────────────────────────────────
class Timer:
    def __init__(self, msg): self.msg = msg
    def __enter__(self): print(f"\n{self.msg} 시작…"); self.t0=time.time()
    def __exit__(self,*a): print(f"{self.msg} 완료: {time.time()-self.t0:.2f}s")


def _build_shift_normalizer(shift_defs: list[dict] | None) -> tuple[dict[str, str], dict[str, str]]:
    """Shift ID를 알고리즘용 메인 코드(D/E/N/O)로 정규화하는 매핑을 생성한다.

    Args:
        shift_defs: shift_id, default_shift, shift_gb를 포함한 사전 리스트

    Returns:
        (id_to_main, main_to_id):
            - id_to_main: shift_id(대문자) → 메인 코드(D/E/N/O)
            - main_to_id: 메인 코드 → 대표 shift_id(초기값은 자기 자신, 매핑되면 우선 적용)
    """
    canonical = {"D", "E", "N", "O", "주", "W"}
    id_to_main: dict[str, str] = {}
    main_to_id: dict[str, str] = {c: c for c in canonical}

    for row in shift_defs or []:
        raw_id = str(row.get("shift_id") or "").strip()
        if not raw_id:
            continue
        sid_upper = raw_id.upper()
        raw_default = str(row.get("default_shift") or "").strip().upper()
        raw_gb = str(row.get("shift_gb") or "").strip().upper()
        shift_type = str(row.get("type") or "").strip()

        if sid_upper in {"OFF"}:
            sid_upper = "O"

        main_code = None
        skip_main_to_id = False
        if raw_default in canonical:
            main_code = raw_default
        elif raw_gb in canonical:
            main_code = raw_gb
        elif sid_upper in canonical:
            main_code = sid_upper
        elif shift_type in {"휴가", "공가"}:
            main_code = "O"
            if sid_upper != "O":
                skip_main_to_id = True  # 휴가/공가가 canonical O를 덮어쓰지 않도록 방지
        elif shift_type == "근무":
            main_code = "W"
            if sid_upper != "W":
                skip_main_to_id = True  # 근무형은 가상 코드 W만 사용

        if not main_code:
            continue

        id_to_main[sid_upper] = main_code
        if not skip_main_to_id:
            current = main_to_id.get(main_code)
            if current in {None, main_code} or sid_upper == main_code:
                main_to_id[main_code] = raw_id

    return id_to_main, main_to_id


def _normalize_shift_code(raw_code: object, id_to_main: dict[str, str]) -> str | None:
    """입력 근무코드를 메인 코드(D/E/N/O)로 정규화한다."""
    code = str(raw_code or "").strip()
    upper = code.upper()
    if upper in {"OFF"}:
        return "O"
    if upper in {"D", "E", "N", "O", "주", "W"}:
        return upper
    return id_to_main.get(upper)



class CPSATBasicEngine:
    """CP-SAT 기반 근무표 생성 엔진"""
    
    def __init__(self):
        self.logger_prefix = "[CP-SAT-Basic]"
    
    def create_config_from_db(self, config_data: dict) -> NurseRosterConfig:
        """DB에서 가져온 설정 데이터를 NurseRosterConfig 객체로 변환한다.

        Notes:
            - 엔진의 커버리지 제약은 `daily_shift_requirements`(및 by_day)를 기반으로 동작한다.
              이 값이 비어있거나 키가 D/E/N으로 정규화되지 않으면 OFF로 쏠릴 수 있다.
        """
        print('config_data', config_data.get('shift_priority'))
        # 법규 제약사항 (Hard Constraints)
        max_conseq_work = config_data.get('max_conseq_work', 5)
        banned_day_after_eve = config_data.get('banned_day_after_eve', True)
        three_seq_nig = config_data.get('three_seq_nig', True)
        two_offs_after_three_nig = config_data.get('two_offs_after_three_nig', True)
        two_offs_after_two_nig = config_data.get('two_offs_after_two_nig', False)
        max_nig_per_month = config_data.get('max_nig_per_month', 15)
        
        # 병원 내규 (Soft Constraints)
        min_exp_per_shift = config_data.get('min_exp_per_shift', 3)
        req_exp_nurses = config_data.get('req_exp_nurses', 1)
        two_offs_per_week = config_data.get('two_offs_per_week', True)
        sequential_offs = config_data.get('sequential_offs', True)
        even_nights = config_data.get('even_nights', True)
        off_placement_mode = int(config_data.get('off_placement_mode', 0) or 0)
        
        # 가중치 설정 - Night Keep은 E와 차별화
        shift_weights = config_data.get('shift_preference_weights', {
            'D': 5.0, 
            'E': 5.0, 
            'N': 7.0,  # Night Keep은 더 높은 가중치
            'O': 10.0
        })
        team_balance_enable = bool(config_data.get('team_balance_enable', False))
        team_balance_gauge = int(config_data.get('team_balance_gauge', 0) or 0)
        # team_balance_weight = int(config_data.get('team_balance_weight', 0) or 0)
        # team_balance_top_days = int(config_data.get('team_balance_top_days', 0) or 0)
        team_balance_focus = config_data.get('team_balance_focus_shifts')
        team_balance_mode = config_data.get('team_balance_mode', 'balanced')
        team_balance_shift_weights = config_data.get('team_balance_shift_weights') or {}

        def _normalize_requirements(req_map: dict | None) -> dict[str, int]:
            """요구 인력 맵을 D/E/N 기준으로 정규화한다.

            Args:
                req_map: 원본 요구치 맵(키가 대소문자/공백/다른 표기일 수 있음)

            Returns:
                'D','E','N'(필요 시 'W') 키만을 갖는 정수 요구치 맵
            """
            base_keys = {"D", "E", "N"}
            if isinstance(req_map, dict):
                for k in req_map.keys():
                    if str(k).strip().upper() == "W":
                        base_keys.add("W")
                        break
            base = {k: 0 for k in base_keys}
            if not isinstance(req_map, dict):
                return base
            for k, v in req_map.items():
                key = str(k).strip().upper()
                if key in base:
                    try:
                        base[key] = int(v or 0)
                    except Exception:
                        base[key] = 0
            return base

        # 요구치(기본) 방어 처리: 없으면 구 필드(day_req/eve_req/nig_req)에서 폴백
        raw_daily_req = config_data.get("daily_shift_requirements")
        if not raw_daily_req:
            raw_daily_req = {
                "D": config_data.get("day_req", 0),
                "E": config_data.get("eve_req", 0),
                "N": config_data.get("nig_req", 0),
            }
        daily_req = _normalize_requirements(raw_daily_req)
        if sum(daily_req.values()) <= 0:
            raise ValueError("daily_shift_requirements(D/E/N)가 비어있습니다. 설정을 확인해주세요.")

        cfg = NurseRosterConfig(
            daily_shift_requirements=daily_req,
            # daily_shift_requirements={
            #     'D': config_data.get('day_req', 3),
            #     'E': config_data.get('eve_req', 3), 
            #     'N': config_data.get('nig_req', 2)
            # },
            # 병원 내규 (Soft Constraints)
            min_experience_per_shift=min_exp_per_shift,
            required_experienced_nurses=req_exp_nurses,
            enforce_two_offs_per_week=two_offs_per_week,
            # 법규 제약사항 (Hard Constraints)
            max_night_shifts_per_month=max_nig_per_month,
            max_consecutive_nights=3 if three_seq_nig else 2,
            not_one_night=True,
            max_consecutive_work_days=max_conseq_work,
            # 추가된 새로운 제약사항들
            banned_day_after_eve=banned_day_after_eve,
            two_offs_after_three_nig=two_offs_after_three_nig,
            two_offs_after_two_nig=two_offs_after_two_nig,
            sequential_offs=sequential_offs,
            even_nights=even_nights,
            nod_noe=config_data.get('nod_noe', True),
            global_monthly_off_days=2,
            standard_personal_off_days=config_data.get('off_days', 8) - 2 if config_data.get('off_days', 8) > 2 else 0,
            # 수정
            shift_requirement_priority = max(0.05, config_data.get('shift_priority', 0.7)),
            shift_preference_weights=shift_weights,
            pair_preference_weight=config_data.get('pair_preference_weight', 3.0),
            # 프리셉터 관련 오버라이드 반영
            preceptor_enable=config_data.get('preceptor_enable', True),
            preceptor_strength_multiplier=config_data.get('preceptor_strength_multiplier', 1.0),
            preceptor_top_days=config_data.get('preceptor_top_days', 12),
            preceptor_min_pair_weight=config_data.get('preceptor_min_pair_weight', 5.0),
            preceptor_focus_shifts=config_data.get('preceptor_focus_shifts', None),
            # team_balance_enable=team_balance_enable,
            team_balance_enable=True, # test
            team_balance_gauge=10, # test
            # team_balance_gauge=team_balance_gauge,
            # team_balance_weight=team_balance_weight,
            # team_balance_weight=100, # test
            # team_balance_top_days=team_balance_top_days,
            team_balance_top_days=30,
            # team_balance_top_days=30, # test
            team_balance_focus_shifts=team_balance_focus,
            # team_balance_focus_shifts=['D', 'E', 'N'], # test
            team_balance_mode=team_balance_mode,
            team_balance_shift_weights=team_balance_shift_weights,
            # 휴무 상한 제어: 최소 필요 OFF 대비 허용 초과 일수
            # - 빡빡하게 off_days에 맞추다 보면 연속근무가 길어지는 현상이 생길 수 있어,
            #   기본값은 +1 정도의 여유를 두고(필요하면 0으로 낮출 수 있음),
            #   대신 extra_off_penalty_weight로 "불필요한 추가 OFF"는 억제한다.
            max_extra_off_days=int(config_data.get('max_extra_off_days', 1)),
            # 추가 OFF 기피(여유 인원은 D/E/N으로 분배 유도)
            extra_off_penalty_weight=int(config_data.get("extra_off_penalty_weight", 80) or 0),
            # 연속근무 소프트 상한(없으면 hard와 동일)
            soft_max_consecutive_work_days=int(config_data.get("soft_max_consecutive_work_days", max_conseq_work) or max_conseq_work),
            soft_consecutive_work_penalty_weight=int(config_data.get("soft_consecutive_work_penalty_weight", 180) or 0),
            # 분배 정책 모드/월단위 선호 가중치
            distribution_mode=str(config_data.get("distribution_mode", "hybrid") or "hybrid"),
            monthly_preference_weight=int(config_data.get("monthly_preference_weight", 60) or 0),
            # 여유 인원 균등화 제어
            oversupply_equalize_enable=bool(config_data.get('oversupply_equalize_enable', True)),
            oversupply_equalize_weight=int(config_data.get('oversupply_equalize_weight', 120)),
            # 주말 휴무 제약: is_weekend_off=True인 간호사가 주말에만 휴무를 받도록 강제
            weekend_off_only_enable=bool(config_data.get('weekend_off_only_enable', True)),
            off_placement_mode=1,
        )
        # 일자별 요구치가 있으면 구성에 부가 속성으로 저장
        try:
            ds_by_day = config_data.get('daily_shift_requirements_by_day')
            if isinstance(ds_by_day, list) and len(ds_by_day) > 0:
                norm_list = []
                for day_map in ds_by_day:
                    if not isinstance(day_map, dict):
                        norm_list.append(daily_req)
                        continue
                    m = _normalize_requirements(day_map)
                    # 일부 키가 누락된 경우 기본 요구치로 보완
                    for kk in ("D", "E", "N"):
                        if kk not in m:
                            m[kk] = daily_req.get(kk, 0)
                    norm_list.append(m)
                setattr(cfg, 'daily_shift_requirements_by_day', norm_list)
        except Exception:
            pass
        return cfg
    
    def create_shift_manage_from_db(self, shift_manage_data: List[dict]):
        shift_manage = []
        for row in shift_manage_data:
            shift_dict = {
                'office_id': row['office_id'],
                'group_id': row['group_id'],
                'nurse_class': row['nurse_class'],
                'shift_slot': row['shift_slot'],
                'main_code': row['main_code'],
                'codes': row['codes'],
            }
            shift_manage.append(ShiftManage(**shift_dict))
        return shift_manage

    def create_nurses_from_db(self, nurses_data: List[dict]) -> List[Nurse]:
        """DB에서 가져온 간호사 데이터를 Nurse 객체 리스트로 변환"""
        # sequence 기준 정렬(없으면 0) → 알고리즘 입력 순서 일관화
        def _to_date(x):
            if x is None:
                return None
            if isinstance(x, datetime):
                return x.date()
            if isinstance(x, date):
                return x
            if isinstance(x, str):
                # 날짜/시간 모두 허용
                try:
                    return datetime.strptime(x, "%Y-%m-%d").date()
                except:
                    try:
                        return datetime.fromisoformat(x).date()
                    except:
                        return None
            return None
        sorted_rows = sorted(nurses_data, key=lambda r: (r.get('sequence', 0), -int(r.get('experience', 0) or 0), str(r.get('nurse_id'))))
        nurses = []
        for i, nurse_data in enumerate(sorted_rows):
            # DB 모델을 Nurse 객체로 변환
            nurse_dict = {
                'id': i,  # 엔진에서 사용할 인덱스 ID
                'db_id': nurse_data['nurse_id'],  # DB ID
                'name': nurse_data['name'],
                'experience_years': nurse_data.get('experience', 0),
                # Grade(1~3): None 허용. 변환 정책은 Grade 제약 모듈에서 처리한다.
                'grade': nurse_data.get('grade'),
                'is_head_nurse': nurse_data.get('is_head_nurse', False),
                # 주말 고정 휴무(True)이면 토/일은 OFF('O')만 허용(하드 제약은 모델 빌더에서 적용)
                'is_weekend_off': bool(nurse_data.get('is_weekend_off', False)),
                'is_night_nurse': nurse_data.get('is_night_nurse', 0),
                'personal_off_adjustment': nurse_data.get('personal_off_adjustment', 0),
                'remaining_off_days': 0,  # 초기화, 나중에 계산됨
                'joining_date': _to_date(nurse_data.get('joining_date')),
                'resignation_date': _to_date(nurse_data.get('resignation_date')),
                'team_id': nurse_data.get('team_id'),
            }

            nurses.append(Nurse(**nurse_dict))
        
        return nurses
    def parse_preferences_from_db(
        self,
        prefs_data: List[dict],
        shift_id_to_main: dict[str, str] | None = None,
    ) -> Tuple[Dict, Dict, Dict]:
        """DB 선호도 데이터를 메인 코드(D/E/N/O) 기준으로 정규화한다."""
        shift_preferences: dict = {}
        off_requests: dict = {}
        pair_preferences = {"work_together": [], "work_apart": []}
        shift_id_to_main = shift_id_to_main or {}

        for pref in prefs_data:
            nurse_id = pref["nurse_id"]
            data = pref.get("data", {})
            if not data:
                continue

            if "shift" in data:
                shift_prefs = {}
                for shift_type, dates in data["shift"].items():
                    normalized = _normalize_shift_code(shift_type, shift_id_to_main)
                    if normalized in {"D", "E", "N"}:
                        shift_prefs[normalized] = dates
                    elif normalized == "O":
                        off_requests[nurse_id] = dates
                if shift_prefs:
                    shift_preferences[nurse_id] = shift_prefs

            if "preference" in data and data["preference"]:
                print(data["preference"])
                for d in data["preference"]:
                    if d["weight"] < 0:
                        pair_preferences["work_apart"].append(
                            {"nurse_1": nurse_id, "nurse_2": d["id"], "weight": d["weight"]}
                        )
                    elif d["weight"] > 0:
                        pair_preferences["work_together"].append(
                            {"nurse_1": nurse_id, "nurse_2": d["id"], "weight": d["weight"]}
                        )
        return shift_preferences, off_requests, pair_preferences

    def generate_roster(
        self, 
        nurses_data: List[dict], 
        prefs_data: List[dict], 
        config_data: dict,
        year: int, 
        month: int,
        grouped: List[dict],
        grade_strategy: str = "BASE",
        grade_config: dict | None = None,
        time_limit_seconds: int = 60,
        randomize: bool = True,           # ← 추가
        seed: int | None = None           # ← 추가 (재현 원하면 지정)
    ) -> Dict[str, List[str]]:
        """
        DB 데이터를 기반으로 CP-SAT를 사용해 근무표를 생성
        
        Args:
            nurses_data: DB에서 가져온 간호사 데이터 리스트
            prefs_data: DB에서 가져온 선호도 데이터 리스트  
            config_data: DB에서 가져온 설정 데이터
            year: 근무표 년도
            month: 근무표 월
            time_limit_seconds: CP-SAT 최적화 시간 제한
            
        Returns:
            Dict[nurse_id, List[shift]]: 간호사별 일일 근무 배정
        """
        
        print(f"{self.logger_prefix} 근무표 생성 시작: {year}년 {month}월")
        
        # 1. 설정 객체 생성
        with Timer("설정 생성"):
            config = self.create_config_from_db(config_data)
        shift_defs = config_data.get("shift_definitions") if isinstance(config_data, dict) else None
        shift_id_to_main, main_to_shift_id = _build_shift_normalizer(shift_defs)
        canonical_to_shift_id = main_to_shift_id or {"D": "D", "E": "E", "N": "N", "O": "O"}
        fixed_original_shift_map: dict[tuple[int, int], str] = {}
        # 2. 대상 월 설정
        target_month = date(year, month, 1)
        # 3. 간호사 객체 생성
        with Timer("간호사 객체 생성"):
            nurses = self.create_nurses_from_db(nurses_data)
            for nurse in nurses:
                nurse.initialize_off_days(config)
        # 4. 근무표 시스템 생성
        with Timer("근무표 시스템 초기화"):
            roster_system = RosterSystem(nurses, target_month, config)
            # 월단위 선호(개인 입력) - dict 형태로 전달됨을 가정
            # 예: {"441172": {"shift": "D", "strength": 7}, ...}
            try:
                msp = config_data.get("monthly_shift_preferences") or {}
                if isinstance(msp, dict):
                    setattr(roster_system, "monthly_shift_preferences", msp)
            except Exception:
                pass
            # Grade/Team/BASE 전략(모델 빌더에서 참조)
            # - 상위 서비스(roster_create_service)에서 roster_config 기반으로 결정된 값을 전달받는다.
            setattr(roster_system, "grade_strategy", str(grade_strategy or "BASE").upper())
            setattr(roster_system, "grade_config", grade_config)
            # 고정된 셀 정보 처리
            fixed_cells = list(config_data.get('fixed_cells', []) or [])
            # 주휴 등 휴무류 코드는 엔진에서 'O'로만 취급한다. (shift_types=['D','E','N','O'])
            for c in fixed_cells:
                try:
                    original_shift = str(c.get('shift') or '').strip()
                except Exception:
                    original_shift = ''
                normalized_shift = _normalize_shift_code(original_shift, shift_id_to_main)
                if not normalized_shift:
                    print(
                        f"{self.logger_prefix} 고정 셀 무시: 알 수 없는 근무코드 shift={original_shift}, "
                        f"nurse_index={c.get('nurse_index')}, day_index={c.get('day_index')}"
                    )
                    continue
                if original_shift and normalized_shift and normalized_shift != original_shift:
                    try:
                        n_idx = int(c.get('nurse_index'))
                        d_idx = int(c.get('day_index'))
                        fixed_original_shift_map[(n_idx, d_idx)] = original_shift
                    except Exception:
                        pass
                c['shift'] = normalized_shift
            # ── 경계 제약(강제 OFF/금지) 병합 ──
            initial_constraints = config_data.get('initial_constraints') or {}
            allow_override_by_law = bool(config_data.get('allow_override_by_law', False))
            rs_dbid_to_idx = {n.db_id: n.id for n in nurses}
            config.off_placement_mode = int(getattr(config, "off_placement_mode", 0) or 0)
            weekly_off_map_raw = config_data.get("weekly_off_map") or {}
            weekly_off_by_idx: dict[int, list[int]] = {}
            for dbid, day_list in (weekly_off_map_raw or {}).items():
                n_idx = rs_dbid_to_idx.get(str(dbid))
                if n_idx is None:
                    continue
                try:
                    weekly_off_by_idx[n_idx] = sorted({int(d) for d in day_list})
                except Exception:
                    continue
            roster_system.weekly_off_by_idx = weekly_off_by_idx
            prev_last_off_raw = config_data.get("prev_month_last_is_off") or {}
            prev_last_off_by_idx: dict[int, bool] = {}
            for dbid, flag in (prev_last_off_raw or {}).items():
                n_idx = rs_dbid_to_idx.get(str(dbid))
                if n_idx is None:
                    continue
                prev_last_off_by_idx[n_idx] = bool(flag)
            roster_system.prev_month_last_is_off = prev_last_off_by_idx
            # forced_off: { nurse_db_id: [day_idx,...] }
            forced_off = initial_constraints.get('forced_off') or {}
            if forced_off:
                for dbid, day_list in forced_off.items():
                    n_idx = rs_dbid_to_idx.get(dbid)
                    if n_idx is None:
                        continue
                    for d in day_list:
                        # 기존 고정과 충돌 검출
                        conflict = next((c for c in fixed_cells if c.get('nurse_index')==n_idx and c.get('day_index')==d and c.get('shift')!='O'), None)
                        if conflict:
                            msg = f"법규-유저 고정 충돌: nurse={dbid}, day={d+1}, user={conflict.get('shift')}, law=O"
                            print(f"{self.logger_prefix} {msg}")
                            if not allow_override_by_law:
                                raise ValueError(msg)
                            # override: 기존 고정 무시
                            fixed_cells = [c for c in fixed_cells if not (c.get('nurse_index')==n_idx and c.get('day_index')==d)]
                        fixed_cells.append({'nurse_index': n_idx, 'day_index': d, 'shift': 'O'})
            if fixed_cells:
                print(f"{self.logger_prefix} 고정된 셀 {len(fixed_cells)}개 처리 중...")
                roster_system.fixed_cells = fixed_cells
                for fixed_cell in fixed_cells:
                    print(f"{self.logger_prefix} 고정 셀: 간호사 {fixed_cell['nurse_index']}, 날짜 {fixed_cell['day_index']+1}, 근무 {fixed_cell['shift']}")
            # forbidden: { nurse_db_id: { day_idx: [codes...] } }
            forbidden = initial_constraints.get('forbidden') or {}
            if forbidden:
                # 내부 인덱스 매핑 구조로 저장
                init_forb = {}
                for dbid, day_map in forbidden.items():
                    n_idx = rs_dbid_to_idx.get(dbid)
                    if n_idx is None:
                        continue
                    for d_str, codes in day_map.items():
                        # 키는 정수 day_idx가 이미 주어졌다고 가정하지만, 혹시 str이면 변환
                        try:
                            d = int(d_str)
                        except Exception:
                            d = d_str
                        normalized_codes = []
                        for code in (codes or []):
                            norm_code = _normalize_shift_code(code, shift_id_to_main)
                            if norm_code:
                                normalized_codes.append(norm_code)
                        init_forb.setdefault((n_idx, d), set()).update(normalized_codes)
                roster_system.initial_forbidden = init_forb
        # 5. 선호도 데이터 파싱 및 적용
        with Timer("선호도 데이터 파싱"):
            shift_preferences, off_requests, pair_preferences = self.parse_preferences_from_db(
                prefs_data, shift_id_to_main
            )
        # ────────────────────────────── 프리셉터 페어링 반영 ──────────────────────────────
        # nurses_data 내 preceptor_id 를 사용해 자동으로 함께 근무 선호를 추가한다.
        try:
            valid_ids = {row.get('nurse_id') for row in nurses_data}
            seen_pairs = set()  # 중복 방지 (무방향)
            added_cnt = 0
            # 프리셉터-멘티 함께 근무 가중치: 기본 페어링 대비 강화
            preceptor_pair_weight = float(getattr(config, 'pair_preference_weight', 3.0)) * 2.5
            for row in nurses_data:
                mentee_id = row.get('nurse_id')
                preceptor_id = row.get('preceptor_id')
                if not mentee_id or not preceptor_id:
                    continue
                if preceptor_id not in valid_ids or preceptor_id == mentee_id:
                    continue
                key = frozenset((mentee_id, preceptor_id))
                if key in seen_pairs:
                    continue
                pair_preferences.setdefault('work_together', [])
                pair_preferences['work_together'].append({
                    'nurse_1': mentee_id,
                    'nurse_2': preceptor_id,
                    'weight': preceptor_pair_weight,
                    'source': 'preceptor'
                })
                seen_pairs.add(key)
                added_cnt += 1
            if added_cnt:
                print(f"[CP-SAT-Basic] 프리셉터 페어링 {added_cnt}건 추가 적용")
        except Exception as e:
            print(f"[CP-SAT-Basic] 프리셉터 페어링 반영 중 오류: {e}")
        # ────────────────────────────────────────────────────────────────────────
        # 6. 휴무 요청 적용
        if off_requests:
            with Timer("휴무 요청 적용"):
                print(f"{self.logger_prefix} 휴무 요청 적용 중...")
                # DB nurse_id를 키로 사용하여 매핑
                mapped_off_requests = {}
                for nurse_id, requests in off_requests.items():
                    # DB nurse_id를 그대로 키로 사용 (roster_system.py에서 n.db_id와 비교하므로)
                    mapped_off_requests[nurse_id] = {str(k): v for k, v in requests.items()}
                roster_system.apply_off_requests(mapped_off_requests)
        # 7. 선호 근무 유형 적용  
        if shift_preferences:
            with Timer("선호 근무 유형 적용"):
                print(f"{self.logger_prefix} 선호 근무 유형 적용 중...")
                # DB nurse_id를 키로 사용하여 매핑
                mapped_shift_preferences = {}
                for nurse_id, prefs in shift_preferences.items():
                    # DB nurse_id를 그대로 키로 사용
                    mapped_shift_preferences[nurse_id] = prefs
                
                roster_system.apply_shift_preferences(mapped_shift_preferences)
        # 8. 페어링 선호도 적용
        with Timer("페어링 선호도 적용"):
            print(f"{self.logger_prefix} 페어링 선호도 적용 중...")
            # 기본값으로 빈 페어링 선호도 설정
            roster_system.apply_pair_preferences(pair_preferences)
        # 9. CP-SAT으로 최적화 (새로운 제약사항 포함)
        with Timer("CP-SAT으로 최적화"):
            print(f"{self.logger_prefix} CP-SAT 최적화 시작 (시간 제한: {time_limit_seconds}초)...")
            success = self._optimize_with_enhanced_constraints(roster_system, time_limit_seconds, nurses, grouped, randomize=randomize, seed=seed)
            if not success:
                print(f"{self.logger_prefix} 개선된 제약사항으로 실패, 기본 알고리즘으로 폴백...")
                self._optimize_fallback_lex_hard_first(roster_system, time_limit_seconds=time_limit_seconds, grouped=grouped)
        # W 배정 디버그 로그
        try:
            if "W" in roster_system.config.shift_types:
                w_idx = roster_system.config.shift_types.index("W")
                w_cells = []
                for n_idx, nurse in enumerate(nurses):
                    for d in range(roster_system.num_days):
                        if int(roster_system.roster[n_idx, d, w_idx]) == 1:
                            original = fixed_original_shift_map.get((n_idx, d))
                            label = original or "W"
                            w_cells.append({"nurse_id": nurse.db_id, "day": d + 1, "shift": label})
                if w_cells:
                    print(f"{self.logger_prefix} W 배정 {len(w_cells)}건: {w_cells}")
                else:
                    print(f"{self.logger_prefix} W 배정 없음")
        except Exception as e:
            print(f"{self.logger_prefix} W 배정 로그 실패: {e}")
        # 10. 결과 변환
        with Timer("결과 변환"):
            result = self._convert_result_to_db_format(
                roster_system,
                nurses,
                canonical_to_shift_id=canonical_to_shift_id,
                fixed_original_shift_map=fixed_original_shift_map,
            )
        
        # 11. 최적화 결과 출력 및 만족도 데이터 수집
        # 프리셉터 쌍 수집 (DB id 기준)
        preceptor_pairs = []
        try:
            valid_ids = {row.get('nurse_id') for row in nurses_data}
            seen = set()
            for row in nurses_data:
                mentee_id = row.get('nurse_id')
                preceptor_id = row.get('preceptor_id')
                if not mentee_id or not preceptor_id:
                    continue
                if mentee_id not in valid_ids or preceptor_id not in valid_ids or mentee_id == preceptor_id:
                    continue
                k = frozenset((mentee_id, preceptor_id))
                if k in seen:
                    continue
                preceptor_pairs.append((mentee_id, preceptor_id))
                seen.add(k)
        except Exception:
            pass
        satisfaction_data = self._print_optimization_results(roster_system, preceptor_pairs)
        
        # 12. 대시보드 분석 데이터 저장 (스케줄 생성 후)
        try:
            from services.dashboard_service import save_roster_analytics
            # 스케줄 ID는 roster_create_service에서 생성된 후 전달받아야 함
            # 여기서는 임시로 None을 전달하고, 실제 저장은 roster_create_service에서 처리
            print(f"{self.logger_prefix} 대시보드 분석 데이터 저장 준비 완료")
        except ImportError:
            print(f"{self.logger_prefix} 대시보드 서비스를 찾을 수 없습니다.")
        
        print(f"{self.logger_prefix} 근무표 생성 완료")

        # Grade 배치 요약 출력/CSV 저장 (GRADE 전략일 때만)
        try:
            grade_strategy_norm = str(grade_strategy or "BASE").upper()
            if grade_strategy_norm == "GRADE" and grade_config:
                _dump_grade_summary(roster_system, nurses, grade_config, self.logger_prefix)
        except Exception as e:
            print(f"{self.logger_prefix} Grade 요약 출력 중 오류: {e}")

        # Grade-aware Local Repair (Phase 3)
        try:
            grade_strategy_norm = str(grade_strategy or "BASE").upper()
            if grade_strategy_norm == "GRADE" and grade_config:
                from services.repairs.grade_repair import grade_local_repair
                updated_roster, repair_log = grade_local_repair(
                    roster_system,
                    grade_config,
                    max_iterations=100,
                    max_moves_per_nurse=1,
                )
                roster_system.roster = updated_roster
                # repair 로그 간단 출력
                if repair_log:
                    print(f"{self.logger_prefix} [REPAIR SUMMARY] moves={len([r for r in repair_log if 'before_short' in r])}, failures={len([r for r in repair_log if r.get('reason')])}")
                    for r in repair_log[:10]:
                        print(f"{self.logger_prefix} [REPAIR] {r}")
                # repair 이후 결과로 DB 변환 갱신
                result = self._convert_result_to_db_format(
                    roster_system,
                    nurses,
                    canonical_to_shift_id=canonical_to_shift_id,
                    fixed_original_shift_map=fixed_original_shift_map,
                )
        except Exception as e:
            print(f"{self.logger_prefix} Grade Repair 중 오류: {e}")

        # 13. 최종 근무표 로그 출력
        self._log_final_roster(nurses, result)

        return {
            "roster": result,
            "satisfaction_data": satisfaction_data,
            "roster_system": roster_system
        }


    def _optimize_with_enhanced_constraints(    # <- generate_roster 에서 호출
        self, roster_system: RosterSystem,
        time_limit_seconds: int,
        nurses_data, grouped=None,
        randomize: bool = True,
        seed: int | None = None
    )->bool:
        from ortools.sat.python import cp_model
        # randomize=False 여도 run_seed는 항상 정의되어야 한다.
        # (e.g., 테스트/재현성 평가 스크립트에서 seed 고정 실행)
        run_seed = seed if seed is not None else ((int(time.time() * 1000) ^ random.getrandbits(31)) & 0x7fffffff)
        # ① 0.3× time_limit 으로 “전체 모델” 한번 돌려 feasible 확보
        # time_limit_seconds가 작을 때도(테스트/평가) 입력된 제한을 존중한다.
        # 예: time_limit_seconds=10이면 base_tl은 최대 3초 정도로 제한.
        base_tl = max(1, int(time_limit_seconds * 0.3))
        base_tl = min(base_tl, max(1, int(time_limit_seconds)))
        roster_system.is_quick_phase = True
        feasible = self._quick_initial_solve(
            roster_system, base_tl, grouped, run_seed)
        roster_system.is_quick_phase = False
        # hard 위반 수 세는 헬퍼
        HARD_TYPES = {
            'shift_requirement', 'max_consecutive_night',
            'max_consecutive_work', 'night_after_limit',
            'day_after_evening', 'night_monthly_limit'
        }
        def hard_violation_cnt():
            return sum(1 for v in roster_system._find_violations()
                       if v['type'] in HARD_TYPES)
        best_viol = hard_violation_cnt()
        best_roster = roster_system.roster.copy()
        # ② RL 정책
        policy = RLNeighborhoodPolicy(len(roster_system.nurses),
                                      roster_system.num_days)
        remaining = time_limit_seconds - base_tl
        per_iter = 8
        max_iter = max(0, remaining // per_iter)

        if max_iter==0:
            return best_viol==0
        for it in range(max_iter):
            try:
                n_sel, d_sel = policy.select()
                ok = _solve_neighbourhood(roster_system, n_sel, d_sel,
                                      per_iter, grouped, run_seed, it = it)
            except Exception as e:
                print(f"{self.logger_prefix} 근무표 생성 중 오류: {e}")
                raise e
            if not ok: policy.update(False, n_sel, d_sel); continue
            curr_viol = hard_violation_cnt()
            improved  = curr_viol < best_viol
            if improved:
                best_viol = curr_viol;  best_roster = roster_system.roster.copy()
            else:  # rollback
                roster_system.roster = best_roster.copy()
            policy.update(improved, n_sel, d_sel)
            if best_viol==0: break
        roster_system.roster = best_roster
        return best_viol==0


    # ────────────────────────────────────────────────────────────────────
    #                    ※ 아래는 helper 들 – 모두 완전판               │
    # ────────────────────────────────────────────────────────────────────
    def _optimize_fallback_lex_hard_first(self, roster_system: RosterSystem, time_limit_seconds: int, grouped=None) -> bool:
        """하드 제약을 최우선으로 하는 서열(lexicographic) 폴백 최적화 수행.

        단계 개요:
        1단계(커버리지 우선): 일/교대 커버리지 부족(short) 최소화. 식: assigned + short - over == need.
        2단계(안전/법규): 1단계 최솟값(short 합)과 over 상한을 고정, 전이/연속/월간/주2OFF/회복/NOD/NOE/야간전담 위반을 정량 슬랙으로 최소화.
        3단계(품질/선호): 1,2단계 결과를 고정(특히 2단계에서 0이었던 위반 위치는 0으로 잠금)한 채 선호/공정성 최대화. 새 위반 생성 금지.

        Args:
            roster_system: 근무표 시스템 객체
            time_limit_seconds: 총 시간 제한(초)
            grouped: 교대 코드 매핑 정보(고정셀 main_code 정규화에 사용)

        Returns:
            bool: 최종적으로 하드 위반 합이 0인 해를 달성했는지 여부
        """
        from ortools.sat.python import cp_model

        print(f"{self.logger_prefix} 폴백(서열) 최적화 시작…")

        # 동적 시간 배분(대략): 45% / 35% / 20%
        tl1 = max(5, int(time_limit_seconds * 0.45))
        tl2 = max(5, int(time_limit_seconds * 0.35))
        tl3 = max(3, time_limit_seconds - tl1 - tl2)

        N, D, S = len(roster_system.nurses), roster_system.num_days, roster_system.config.num_shifts
        cfg = roster_system.config

        # 공통 인덱스/구간
        idx = {c: roster_system.config.shift_types.index(c) for c in ('D', 'E', 'N', 'O')}
        day_idx, eve_idx, night_idx, off_idx = idx['D'], idx['E'], idx['N'], idx['O']
        has_w = "W" in roster_system.config.shift_types
        w_idx = roster_system.config.shift_types.index("W") if has_w else None

        first_day = roster_system.target_month
        last_day = first_day + timedelta(days=D - 1)
        weekend_days = {d for d in range(D) if (first_day + timedelta(days=d)).weekday() >= 5}
        join, leave = [], []
        for nu in roster_system.nurses:
            j = (nu.joining_date - first_day).days if nu.joining_date else 0
            if nu.resignation_date:
                if nu.resignation_date < first_day:
                    # 이번 달에 근무하지 않는 인원은 범위 밖으로 설정하여 변수 생성을 건너뛴다.
                    join.append(1)
                    leave.append(0)
                    continue
                l = (nu.resignation_date - first_day).days
                if nu.resignation_date > last_day:
                    l = D - 1
            else:
                l = D - 1
            j = max(j, 0)
            l = min(l, D - 1)
            join.append(j)
            leave.append(l)

        # 고정셀(메인코드 정규화)
        code2main = {c: r['main_code'] for r in (grouped or []) for c in r['codes']}
        fixed, fixed_cnt = {}, [[0] * S for _ in range(D)]
        for c in getattr(roster_system, 'fixed_cells', []) or []:
            n, d = c['nurse_index'], c['day_index']
            s_main = code2main.get(c['shift'], c['shift'])
            s_idx = roster_system.config.shift_types.index(s_main)
            fixed[(n, d)] = s_idx
            fixed_cnt[d][s_idx] += 1

        # 초기 금지(경계) 맵
        initial_forbidden = getattr(roster_system, 'initial_forbidden', {}) if isinstance(getattr(roster_system, 'initial_forbidden', {}), dict) else {}

        # ── 폴백 사전 진단 로그(불가능 원인 빠른 파악용) ──
        try:
            # 월간 N 총 요구(고정셀로 이미 채워진 N은 제외)
            total_need_n = 0
            for d in range(D):
                if hasattr(cfg, 'daily_shift_requirements_by_day') and isinstance(cfg.daily_shift_requirements_by_day, list) and d < len(cfg.daily_shift_requirements_by_day):
                    need_map = cfg.daily_shift_requirements_by_day[d]
                else:
                    need_map = cfg.daily_shift_requirements
                need_n = int((need_map or {}).get("N", 0) or 0)
                need_n = max(0, need_n - int(fixed_cnt[d][night_idx] or 0))
                total_need_n += need_n

            # 간호사별 N 가능 여부(허용 근무유형 기반: []=제한없음, ['N']=N전담)
            n_allowed_indices: list[int] = []
            n_only_cnt = 0
            for i, nu in enumerate(roster_system.nurses):
                raw = getattr(nu, "is_night_nurse", None)
                if isinstance(raw, list):
                    allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
                    # [] => 제한 없음 (N 가능)
                    if not allowed:
                        n_allowed_indices.append(i)
                        continue
                    if "N" in allowed:
                        n_allowed_indices.append(i)
                        if allowed == {"N"}:
                            n_only_cnt += 1
                        continue
                    # N이 없으면 N 불가
                else:
                    # 레거시 타입(int/bool/None 등)은 허용 제약에서 무시했으므로 "N 가능"으로 간주
                    n_allowed_indices.append(i)

            # N 용량 상한(1) 단순: 개인 월 상한 + 재직일수(입/퇴사) 클램프
            cap_basic = 0
            cap_recovery = 0
            for n in n_allowed_indices:
                T0, T1 = join[n], leave[n]
                avail_days = max(0, int(T1 - T0 + 1))
                cap_basic += min(int(cfg.max_night_shifts_per_month), avail_days)
                # 2N→2OFF hard가 켜지면, 한 사람의 N은 대략 2일 중 1일 수준(최대 0.5 비율)로 제한되는 경향이 있다.
                # 예) avail_days=30 이면 (30+1)//2 = 15 가 상한 근사치.
                cap_recovery += min(int(cfg.max_night_shifts_per_month), int((avail_days + 1) // 2))

            # 일별 N 요구 최대값(피크 일자 확인용)
            max_daily_need_n = 0
            for d in range(D):
                if hasattr(cfg, 'daily_shift_requirements_by_day') and isinstance(cfg.daily_shift_requirements_by_day, list) and d < len(cfg.daily_shift_requirements_by_day):
                    need_map = cfg.daily_shift_requirements_by_day[d]
                else:
                    need_map = cfg.daily_shift_requirements
                need_n = int((need_map or {}).get("N", 0) or 0)
                need_n = max(0, need_n - int(fixed_cnt[d][night_idx] or 0))
                max_daily_need_n = max(max_daily_need_n, need_n)

            print(
                f"{self.logger_prefix} [FallbackFeasibility] "
                f"need_N(total)={total_need_n}, need_N(daily_max)={max_daily_need_n}, "
                f"N_allowed_nurses={len(n_allowed_indices)}/{N}, N_only={n_only_cnt}, "
                f"cap_N_basic≈{cap_basic}, "
                f"cap_N_2N2OFF≈{cap_recovery if cfg.two_offs_after_two_nig else 'n/a'}, "
                f"maxN={cfg.max_night_shifts_per_month}, two_offs_after_two_nig={bool(cfg.two_offs_after_two_nig)}"
            )
            if total_need_n > cap_basic:
                print(
                    f"{self.logger_prefix} [FallbackFeasibility][WARN] "
                    f"월간 N 요구({total_need_n})가 단순 상한(cap≈{cap_basic})을 초과합니다. "
                    f"→ 하드 상한을 강제하면 infeasible 가능성이 큽니다."
                )
            if bool(cfg.two_offs_after_two_nig) and total_need_n > cap_recovery:
                print(
                    f"{self.logger_prefix} [FallbackFeasibility][WARN] "
                    f"2N→2OFF 기준 상한(cap≈{cap_recovery})도 초과합니다. "
                    f"→ 2N→2OFF를 hard로 두면 폴백1부터 infeasible 가능성이 큽니다."
                )
            # 핵심: 일별 피크 요구 vs N 가능 인원 비교(2N→2OFF 하드가 있으면 특정 날짜에서 N 배정 가능 인원이 급감할 수 있음)
            if bool(cfg.two_offs_after_two_nig) and max_daily_need_n > len(n_allowed_indices) * 0.5:
                print(
                    f"{self.logger_prefix} [FallbackFeasibility][WARN] "
                    f"일별 N 피크 요구({max_daily_need_n})가 N 가능 인원({len(n_allowed_indices)})의 절반 이상입니다. "
                    f"→ 2N→2OFF 하드 + 다른 제약(주2OFF/연속근무K 등)과 겹치면 특정 날짜에서 N 배정 불가능할 수 있습니다."
                )
            # 월 최대 OFF 상한 vs 2N→2OFF 강제 OFF 충돌 확인
            try:
                base_min_off = int(getattr(cfg, 'global_monthly_off_days', 0) + getattr(cfg, 'standard_personal_off_days', 0))
                extra_allowed = int(getattr(cfg, 'max_extra_off_days', 1) or 1)
                max_off_allowed_per_person = base_min_off + extra_allowed
                # 2N→2OFF가 켜지면, N 가능 인원이 N을 배정받을 때마다 "연속 2N → 다음 2일 OFF"가 강제됨
                # 대략: N 1회당 추가 OFF 1일 정도로 근사 (2N→2OFF가 강제하는 OFF의 평균)
                # 예: 월간 N 요구가 높으면 (특히 N-only 간호사), 2N→2OFF가 강제하는 OFF가 많아져 월 최대 OFF 상한을 초과할 수 있음
                if bool(cfg.two_offs_after_two_nig) and max_off_allowed_per_person < base_min_off + 5:
                    # N_only 간호사가 많거나 N 요구가 높으면 2N→2OFF가 강제하는 OFF가 많아질 수 있음
                    est_extra_off_from_2n2o = int(total_need_n / len(n_allowed_indices) * 0.5) if n_allowed_indices else 0
                    if est_extra_off_from_2n2o > extra_allowed:
                        print(
                            f"{self.logger_prefix} [FallbackFeasibility][WARN] "
                            f"2N→2OFF 하드가 예상 강제 OFF({est_extra_off_from_2n2o})가 월 최대 OFF 여유({extra_allowed})를 초과할 수 있습니다. "
                            f"(min_off={base_min_off}, max_allowed={max_off_allowed_per_person}) "
                            f"→ 2N→2OFF 하드 + 월 최대 OFF 상한 하드가 충돌하여 infeasible 가능성이 큽니다."
                        )
            except Exception:
                pass
        except Exception as e:
            print(f"{self.logger_prefix} [FallbackFeasibility] 진단 로그 실패: {e}")

        # 모델 빌더: stage에 따라 목적 및 고정 제약 선택, 안전 위반 변수 구조도 반환
        def build_model(stage: int,
                        coverage_eq: Optional[int] = None,
                        over_le: Optional[int] = None,
                        stage2_zero_locks: Optional[Dict[str, list]] = None,
                        relax_level: int = 0):
            m = cp_model.CpModel()
            Xv = {}
            def X(n, d, s):
                return Xv.get((n, d, s), 0)

            for n in range(N):
                for d in range(join[n], leave[n] + 1):
                    for s in range(S):
                        Xv[n, d, s] = m.NewBoolVar(f'x_{n}_{d}_{s}')
            active_days = {(n, d) for n in range(N) for d in range(join[n], leave[n] + 1)}
            # 고정 셀
            for (n, d), s_idx in fixed.items():
                if (n, d) not in active_days:
                    print(f"{self.logger_prefix} 고정 셀 무시: n={n}, d={d+1} (퇴사/입사 범위 밖)")
                    continue
                m.Add(X(n, d, s_idx) == 1)
                for s in range(S):
                    if s != s_idx:
                        m.Add(X(n, d, s) == 0)
            # W(특별 근무)는 고정 셀 외에는 전부 금지
            if has_w and w_idx is not None:
                for n in range(N):
                    for d in range(join[n], leave[n] + 1):
                        if (n, d) in fixed and fixed[(n, d)] == w_idx:
                            continue
                        m.Add(X(n, d, w_idx) == 0)

            # 주말 휴무 제약: is_weekend_off=True인 간호사는 주말에만 휴무, 평일에는 휴무 금지
            if getattr(cfg, 'weekend_off_only_enable', True):
                for n, nu in enumerate(roster_system.nurses):
                    if not bool(getattr(nu, "is_weekend_off", False)):
                        continue
                    for d in range(join[n], leave[n] + 1):
                        if d in weekend_days:
                            # 주말(토/일): OFF만 허용
                            if (n, d) in fixed and fixed[(n, d)] != off_idx:
                                raise ValueError(
                                    f"주말 고정 휴무 충돌: nurse_index={n}, day={d+1}, "
                                    f"fixed_shift={cfg.shift_types[fixed[(n, d)]]}, required=O"
                                )
                            m.Add(X(n, d, off_idx) == 1)
                        else:
                            # 평일(월~금): OFF 금지(D/E/N만 가능)
                            if (n, d) in fixed and fixed[(n, d)] == off_idx:
                                raise ValueError(
                                    f"주말 휴무 대상 간호사는 평일에 휴무를 받을 수 없습니다. "
                                    f"nurse_index={n}, day={d+1}"
                                )
                            m.Add(X(n, d, off_idx) == 0)

            off_placement_mode = int(getattr(cfg, "off_placement_mode", 0) or 0)
            weekly_off_by_idx = (
                getattr(roster_system, "weekly_off_by_idx", {})
                if isinstance(getattr(roster_system, "weekly_off_by_idx", {}), dict)
                else {}
            )
            prev_month_last_is_off = (
                getattr(roster_system, "prev_month_last_is_off", {})
                if isinstance(getattr(roster_system, "prev_month_last_is_off", {}), dict)
                else {}
            )
            if off_placement_mode > 0 and weekly_off_by_idx:
                for n, day_list in weekly_off_by_idx.items():
                    if n >= len(join):
                        continue
                    T0, T1 = join[n], leave[n]
                    for d_raw in day_list or []:
                        try:
                            d = int(d_raw)
                        except Exception:
                            continue
                        if d < T0 or d > T1:
                            continue
                        if d == D - 1:
                            continue
                        if d == 0:
                            if bool(prev_month_last_is_off.get(n, False)):
                                continue
                            if d + 1 <= T1:
                                m.Add(X(n, d + 1, off_idx) == 1)
                            continue
                        if off_placement_mode == 1:
                            # print('주휴 앞 O!!!!!!!!')
                            neighbours = []
                            if d - 1 >= T0:
                                neighbours.append(X(n, d - 1, off_idx))
                            if d + 1 <= T1:
                                neighbours.append(X(n, d + 1, off_idx))
                            if not neighbours:
                                continue
                            if len(neighbours) == 1:
                                m.Add(neighbours[0] == 1)
                            else:
                                m.Add(sum(neighbours) >= 1)
                        else:
                            if d - 1 >= T0:
                                m.Add(X(n, d - 1, off_idx) == 1)
                            elif d + 1 <= T1:
                                m.Add(X(n, d + 1, off_idx) == 1)

            # 초기 금지: 고정과 충돌하면 금지 무시(로그만)
            try:
                if initial_forbidden:
                    for (n, d), code_list in initial_forbidden.items():
                        for code in (code_list or []):
                            if code not in roster_system.config.shift_types:
                                continue
                            s_idx = roster_system.config.shift_types.index(code)
                            if (n, d) not in active_days:
                                print(f"{self.logger_prefix} 초기 금지 무시: n={n}, d={d+1}, code={code} (퇴사/입사 범위 밖)")
                                continue
                            if (n, d) in fixed and fixed[(n, d)] == s_idx:
                                print(f"{self.logger_prefix} 경계 금지-고정 충돌 무시: n={n}, d={d+1}, code={code}")
                                continue
                            m.Add(X(n, d, s_idx) == 0)
            except Exception as e:
                print(f"{self.logger_prefix} 초기 금지 셀 적용 중 오류: {e}")

            # exactly-one
            for n in range(N):
                for d in range(join[n], leave[n] + 1):
                    if (n, d) in fixed:
                        continue
                    m.AddExactlyOne(X(n, d, s) for s in range(S))

            # 1) 커버리지 등식: assigned + short - over == need (날짜별 요구치 적용)
            short_terms, over_terms = [], []
            over_vars_by_day = {}
            short_vars_by_day_code: Dict[tuple[int, str], cp_model.IntVar] = {}
            over_vars_by_day_code: Dict[tuple[int, str], cp_model.IntVar] = {}
            for d in range(D):
                if hasattr(cfg, 'daily_shift_requirements_by_day') and isinstance(cfg.daily_shift_requirements_by_day, list) and d < len(cfg.daily_shift_requirements_by_day):
                    need_map = cfg.daily_shift_requirements_by_day[d]
                else:
                    need_map = cfg.daily_shift_requirements
                for code, req in need_map.items():
                    if code not in roster_system.config.shift_types:
                        continue
                    s = roster_system.config.shift_types.index(code)
                    need = int(req) - fixed_cnt[d][s]
                    if need <= 0:
                        continue
                    assigned = sum(
                        X(n, d, s)
                        for n in range(N)
                        if join[n] <= d <= leave[n] and (n, d) not in fixed
                    )
                    sh = m.NewIntVar(0, N, f'short_{d}_{code}')
                    ov = m.NewIntVar(0, N, f'over_{d}_{code}')
                    # Coverage 우선: assigned + shortage >= need (hard), oversupply 추적은 선택
                    m.Add(assigned + sh >= need)
                    m.Add(assigned - ov <= need)
                    short_terms.append(sh)
                    over_terms.append(ov)
                    over_vars_by_day.setdefault(d, {})[code] = ov
                    short_vars_by_day_code[(d, code)] = sh
                    over_vars_by_day_code[(d, code)] = ov

            # 2) 안전/법규 위반(정량 슬랙) 구성
            safety = {
                'trans_nd': [],   # N→D 위반 (Bool)
                'trans_ed': [],   # E→D 위반 (Bool)
                'trans_ne': [],   # N→E 위반 (Bool)
                'cwork_missing': [],   # 연속근무 창에서 필요한 OFF 부족량(Int)
                'cnight_excess': [],   # 연속 N 초과(Int)
                'mnight_excess': [],   # 월간 N 초과(Int)
                'night_only_de': [],   # 야간전담의 D/E 배정 위반(Bool/Int)
                'week_off_missing': [],# 주별 2OFF 부족(Int)
                'rec_3n2o': [],       # N3→2O 회복 부족(Int)
                'rec_2n2o': [],       # N2→2O 회복 부족(Int)
                'pattern_nod': [],    # N-O-D 패턴(Int)
                'pattern_noe': [],    # N-O-E 패턴(Int)
                'min_off_missing': [] # 월 최소 OFF 부족(Int)
            }

            # 전이 위반: 정확한 reification (iff)
            for n in range(N):
                T0, T1 = join[n], leave[n]
                for d in range(T0 + 1, T1 + 1):
                    xn = X(n, d - 1, night_idx)
                    xd = X(n, d, day_idx)
                    b_nd = m.NewBoolVar(f'viol_nd_{n}_{d}')
                    # (N∧D) → b_nd, b_nd → N, b_nd → D
                    m.AddBoolOr([b_nd, xn.Not(), xd.Not()])
                    m.AddImplication(b_nd, xn)
                    m.AddImplication(b_nd, xd)
                    safety['trans_nd'].append(b_nd)
                    if cfg.banned_day_after_eve:
                        xe = X(n, d - 1, eve_idx)
                        b_ed = m.NewBoolVar(f'viol_ed_{n}_{d}')
                        m.AddBoolOr([b_ed, xe.Not(), xd.Not()])
                        m.AddImplication(b_ed, xe)
                        m.AddImplication(b_ed, xd)
                        safety['trans_ed'].append(b_ed)
                        
                        # N→E 금지 추가
                        xe2 = X(n, d, eve_idx)
                        b_ne = m.NewBoolVar(f'viol_ne_{n}_{d}')
                        m.AddBoolOr([b_ne, xn.Not(), xe2.Not()])
                        m.AddImplication(b_ne, xn)
                        m.AddImplication(b_ne, xe2)
                        safety['trans_ne'].append(b_ne)

            # 1N 금지: N 배정 시 인접일 중 최소 1일은 N 이어야 한다.
            if bool(getattr(cfg, "not_one_night", False)):
                for n in range(N):
                    T0, T1 = join[n], leave[n]
                    for d in range(T0, T1 + 1):
                        neighbors = []
                        if d - 1 >= T0:
                            neighbors.append(X(n, d - 1, night_idx))
                        if d + 1 <= T1:
                            neighbors.append(X(n, d + 1, night_idx))
                        if not neighbors:
                            continue
                        m.Add(X(n, d, night_idx) <= sum(neighbors))

            # 연속 근무 K+1 창에서 최소 1 OFF 필요 → 부족량 정량화
            K = cfg.max_consecutive_work_days
            for n in range(N):
                T0, T1 = join[n], leave[n]
                for d0 in range(T0, max(T0, T1 - K + 1)):
                    pass
                for d0 in range(T0, T1 - K + 1):
                    sum_off = sum(X(n, d0 + t, off_idx) for t in range(K + 1))
                    miss = m.NewIntVar(0, K + 1, f'cwork_miss_{n}_{d0}')
                    m.Add(miss >= 1 - sum_off)
                    safety['cwork_missing'].append(miss)

            # 연속 Night 상한 L → 초과량 정량화
            L = cfg.max_consecutive_nights
            for n in range(N):
                T0, T1 = join[n], leave[n]
                for d0 in range(T0, T1 - L + 1):
                    sum_n = sum(X(n, d0 + t, night_idx) for t in range(L + 1))
                    exc = m.NewIntVar(0, L + 1, f'cnight_exc_{n}_{d0}')
                    m.Add(exc >= sum_n - L)
                    safety['cnight_excess'].append(exc)

            # 월 Night 상한 초과량
            for n in range(N):
                T0, T1 = join[n], leave[n]
                sum_m = sum(X(n, d, night_idx) for d in range(T0, T1 + 1))
                # 법정 필수(요청): 폴백에서도 하드로 강제
                m.Add(sum_m <= cfg.max_night_shifts_per_month)

            # 야간전담의 D/E 금지 위반(OR: D or E)
            for n, nu in enumerate(roster_system.nurses):
                if nu.is_night_nurse != 0:
                    continue
                T0, T1 = join[n], leave[n]
                for d in range(T0, T1 + 1):
                    v = m.NewIntVar(0, 1, f'nonly_de_{n}_{d}')
                    m.Add(v >= X(n, d, day_idx))
                    m.Add(v >= X(n, d, eve_idx))
                    m.Add(v <= X(n, d, day_idx) + X(n, d, eve_idx))
                    safety['night_only_de'].append(v)

            # 주별 2OFF 부족량
            if cfg.enforce_two_offs_per_week:
                weeks = D // 7
                for n in range(N):
                    for w in range(weeks):
                        d0, d1 = w * 7, min(w * 7 + 7, D)
                        offs = sum(X(n, d, off_idx) for d in range(d0, d1)
                                   if join[n] <= d <= leave[n])
                        miss = m.NewIntVar(0, 2, f'week_miss_{n}_{w}')
                        m.Add(miss >= 2 - offs)
                        safety['week_off_missing'].append(miss)

            # 회복 규칙: N3→2O, N2→2O 부족량
            if cfg.two_offs_after_three_nig:
                for n in range(N):
                    T0, T1 = join[n], leave[n]
                    for d in range(T0 + 2, T1 - 1):
                        # (N_d-2 ∧ N_d-1 ∧ N_d)일 때, 다음 2일 OFF 부족량(0~2)을 패널티로 둔다.
                        xn0 = X(n, d, night_idx)
                        xn1 = X(n, d - 1, night_idx)
                        xn2 = X(n, d - 2, night_idx)
                        need = X(n, d + 1, off_idx) + X(n, d + 2, off_idx)  # 0..2
                        miss = m.NewIntVar(0, 2, f'rec3n2o_{n}_{d}')
                        # 연속 3N이 아니면 miss=0 (패널티 없음)
                        m.Add(miss == 0).OnlyEnforceIf(xn0.Not())
                        m.Add(miss == 0).OnlyEnforceIf(xn1.Not())
                        m.Add(miss == 0).OnlyEnforceIf(xn2.Not())
                        # 연속 3N이면 miss == 2 - need
                        m.Add(miss == 2 - need).OnlyEnforceIf([xn0, xn1, xn2])
                        safety['rec_3n2o'].append(miss)
            if cfg.two_offs_after_two_nig:
                for n in range(N):
                    T0, T1 = join[n], leave[n]
                    for d in range(T0 + 1, T1 - 1):
                        # 블록이 2N 이상이고, d가 블록의 끝일 때만 회복 부족량을 계상 (3N 내부는 0)
                        xn_prev = X(n, d - 1, night_idx)
                        xn_curr = X(n, d, night_idx)
                        xn_next = X(n, d + 1, night_idx)
                        end_block = m.NewBoolVar(f'end_2n_soft_{n}_{d}')
                        m.Add(end_block == xn_next.Not())
                        need = X(n, d + 1, off_idx) + X(n, d + 2, off_idx)  # 0..2
                        miss = m.NewIntVar(0, 2, f'rec2n2o_{n}_{d}')
                        m.Add(miss == 0).OnlyEnforceIf(end_block.Not())
                        m.Add(miss == 0).OnlyEnforceIf(xn_prev.Not())
                        m.Add(miss == 0).OnlyEnforceIf(xn_curr.Not())
                        m.Add(miss == 2 - need).OnlyEnforceIf([xn_prev, xn_curr, end_block])
                        safety['rec_2n2o'].append(miss)

            # 금지 패턴 N-O-D/E
            if getattr(cfg, 'nod_noe', True):
                for n in range(N):
                    T0, T1 = join[n], leave[n]
                    for d in range(T0, T1 - 2):
                        v1 = m.NewIntVar(0, 1, f'nod_{n}_{d}')
                        m.Add(v1 >= X(n, d, night_idx) + X(n, d + 1, off_idx) + X(n, d + 2, day_idx) - 2)
                        safety['pattern_nod'].append(v1)
                        v2 = m.NewIntVar(0, 1, f'noe_{n}_{d}')
                        m.Add(v2 >= X(n, d, night_idx) + X(n, d + 1, off_idx) + X(n, d + 2, eve_idx) - 2)
                        safety['pattern_noe'].append(v2)

            # 월 최소 OFF 부족량(가능일수 클램프)
            try:
                for n in range(N):
                    T0, T1 = join[n], leave[n]
                    # N 전담 간호사 여부 확인 (월 최대 OFF 상한 적용 제외용)
                    nu = roster_system.nurses[n]
                    is_n_only = False
                    raw = getattr(nu, "is_night_nurse", None)
                    if isinstance(raw, list):
                        allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
                        is_n_only = (allowed == {"N"})
                    elif raw == 3 or (raw is not None and raw != 0 and raw != False):
                        is_n_only = True
                    
                    base_min_off = int(getattr(cfg, 'global_monthly_off_days', 0) + getattr(cfg, 'standard_personal_off_days', 0))
                    min_off_required = min(base_min_off, T1 - T0 + 1)
                    if min_off_required > 0:
                        offs = sum(X(n, d, off_idx) for d in range(T0, T1 + 1))
                        miss = m.NewIntVar(0, D, f'min_off_miss_{n}')
                        m.Add(miss >= min_off_required - offs)
                        safety['min_off_missing'].append(miss)
                    # 월 최대 OFF 상한(하드): 최소 필요 OFF + 허용 초과 일수
                    # N 전담 간호사는 OFF 상한을 동적으로 계산: 해당월 날짜수 - 최대 근무 가능일(15일)
                    # relax_level > 0이면 점진적으로 완화 (hard 충돌 해소용)
                    extra_allowed = int(getattr(cfg, 'max_extra_off_days', 0))
                    if extra_allowed >= 0:
                        if is_n_only:
                            # N 전담: 실제 근무 가능 일수(T1-T0+1)에서 최대 N 근무(15일)을 뺀 값
                            # 예) 28일 월 → 28-15=13, 30일 월 → 30-15=15, 31일 월 → 31-15=16
                            avail_days = T1 - T0 + 1
                            max_off_allowed_n_only = max(0, avail_days - 15) + relax_level
                            offs2 = sum(X(n, d, off_idx) for d in range(T0, T1 + 1))
                            m.Add(offs2 <= max_off_allowed_n_only)
                        else:
                            max_off_allowed = min(min_off_required + extra_allowed + relax_level, T1 - T0 + 1)
                            offs2 = sum(X(n, d, off_idx) for d in range(T0, T1 + 1))
                            m.Add(offs2 <= max_off_allowed)
            except Exception:
                pass

            # stage별 목적/고정
            if stage == 1:
                # 커버리지: shortage를 압도적으로 최소화 (coverage-first)
                m.Minimize(100000 * sum(short_terms) + sum(over_terms))
            elif stage == 2:
                # 1단계 최솟값 고정 + over 상한 유지
                if coverage_eq is not None:
                    m.Add(sum(short_terms) == coverage_eq)
                if over_le is not None:
                    m.Add(sum(over_terms) <= over_le)
                # 모든 안전 위반의 합 최소화(정량)
                safety_sum = []
                for k, arr in safety.items():
                    safety_sum.extend(arr)
                m.Minimize(sum(safety_sum))
            else:
                # 1,2단계 고정 + 2단계에서 0이었던 위반은 0으로 잠금(새 위반 금지)
                if coverage_eq is not None:
                    m.Add(sum(short_terms) == coverage_eq)
                if over_le is not None:
                    m.Add(sum(over_terms) <= over_le)
                if stage2_zero_locks:
                    for k, arr in stage2_zero_locks.items():
                        for v in arr:
                            # v는 0/1 또는 정수슬랙(>=0). 0 고정.
                            m.Add(v == 0)
                # 선호/공정성 최대화
                obj = []
                P = roster_system.preference_matrix
                for n in range(N):
                    nu = roster_system.nurses[n]
                    # N 전담 간호사 여부 확인
                    is_n_only = False
                    raw = getattr(nu, "is_night_nurse", None)
                    if isinstance(raw, list):
                        allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
                        is_n_only = (allowed == {"N"})
                    elif raw == 3 or (raw is not None and raw != 0 and raw != False):
                        is_n_only = True
                    
                    for d in range(join[n], leave[n] + 1):
                        for s in range(S):
                            base_score = int(P[n, d, s] * 100)
                            # N 전담 간호사가 N을 배정받으면 높은 보너스 (N 우선 배정 유도)
                            if is_n_only and s == night_idx:
                                base_score += 500  # N 전담의 N 근무에 큰 보너스
                            obj.append(base_score * X(n, d, s))

                # 추가 OFF(여유 OFF) 기피: 가능한 한 off_days 이상으로 OFF를 늘리지 않도록 유도
                try:
                    off_penalty = int(getattr(cfg, "extra_off_penalty_weight", 0) or 0)
                    if off_penalty > 0:
                        for n in range(N):
                            for d in range(join[n], leave[n] + 1):
                                obj.append(-off_penalty * X(n, d, off_idx))
                except Exception:
                    pass

                # 월단위 선호(개인 입력) 유도: 폴백 3단계에서도 동일하게 반영
                try:
                    msp = getattr(roster_system, "monthly_shift_preferences", None)
                    base_w = int(getattr(cfg, "monthly_preference_weight", 0) or 0)
                    if base_w > 0 and isinstance(msp, dict) and msp:
                        for n, nu in enumerate(roster_system.nurses):
                            pref = msp.get(str(getattr(nu, "db_id", ""))) or msp.get(getattr(nu, "db_id", ""))
                            if not isinstance(pref, dict):
                                continue
                            code = str(pref.get("shift") or "").strip().upper()
                            if code not in {"D", "E", "N"}:
                                continue
                            try:
                                strength = int(pref.get("strength", 5) or 0)
                            except Exception:
                                strength = 5
                            strength = max(0, min(10, strength))
                            w = int(round(base_w * (strength / 10.0)))
                            if w <= 0:
                                continue
                            s_idx = cfg.shift_types.index(code)
                            for d in range(join[n], leave[n] + 1):
                                obj.append(w * X(n, d, s_idx))
                except Exception:
                    pass

                # 연속근무 소프트 상한(폴백 3단계에서도 동일하게 적용)
                try:
                    soft_k = int(getattr(cfg, "soft_max_consecutive_work_days", 0) or 0)
                    w_soft = int(getattr(cfg, "soft_consecutive_work_penalty_weight", 0) or 0)
                    if soft_k > 0 and w_soft > 0:
                        for n in range(N):
                            T0, T1 = join[n], leave[n]
                            for d0 in range(T0, T1 - soft_k + 1):
                                sum_off = sum(X(n, d0 + t, off_idx) for t in range(soft_k + 1))
                                miss = m.NewIntVar(0, 1, f"soft_cwork_miss_fb_{n}_{d0}")
                                m.Add(miss >= 1 - sum_off)
                                obj.append(-w_soft * miss)
                except Exception:
                    pass
                # 경력자 부족 약벌
                for d in range(D):
                    for code in ('D', 'E', 'N'):
                        s = roster_system.config.shift_types.index(code)
                        exp_assigned = sum(X(n, d, s)
                                           for n, nu in enumerate(roster_system.nurses)
                                           if join[n] <= d <= leave[n] and nu.experience_years >= cfg.min_experience_per_shift)
                        shortage = m.NewIntVar(0, cfg.required_experienced_nurses, f'expShort_fb_{d}_{code}')
                        m.Add(shortage >= cfg.required_experienced_nurses - exp_assigned)
                        obj.append(-100 * shortage)
                # 여유 인원 L1 균등화(일별 D/E/N): |ov_d,c1 - ov_d,c2| 최소화
                try:
                    if bool(getattr(cfg, 'oversupply_equalize_enable', True)):
                        w_eq = int(getattr(cfg, 'oversupply_equalize_weight', 120))
                        for d, code2ov in over_vars_by_day.items():
                            work_codes = [code for code in code2ov.keys() if code in roster_system.config.daily_shift_requirements.keys()]
                            for i in range(len(work_codes)):
                                for j in range(i + 1, len(work_codes)):
                                    c1, c2 = work_codes[i], work_codes[j]
                                    ov1, ov2 = code2ov[c1], code2ov[c2]
                                    diff = m.NewIntVar(0, N, f'ov_diff_fb_{d}_{c1}_{c2}')
                                    m.Add(diff >= ov1 - ov2)
                                    m.Add(diff >= ov2 - ov1)
                                    obj.append(-w_eq * diff)
                except Exception:
                    pass

                # 팀/프리셉터 soft objective를 폴백(3단계)에서도 반영한다.
                # - 폴백이 타는 케이스가 많으면, 여기 반영이 없을 경우 gauge가 "먹지 않는" 것처럼 보인다.
                try:
                    obj.extend(_add_preceptor_objective_terms(m, roster_system, X, join, leave))
                except Exception as e:
                    print('preceptor_objective_terms 예외 발생')
                    print('e', e)
                    pass
                # 전략:
                # - GRADE: Team OFF
                # - TEAM : Team ON
                # - BASE : Team OFF
                try:
                    grade_strategy = str(getattr(roster_system, "grade_strategy", "BASE") or "BASE").upper()
                    print('grade_strategy', grade_strategy)
                    # grade_strategy = "TEAM"
                    if grade_strategy == "TEAM":
                        obj.extend(add_team_balance_objective_terms(m, roster_system, X, join, leave))
                except Exception as e:
                    print('team_balance_objective_terms 예외 발생')
                    print('e', e)
                    pass

                # Grade 제약을 soft penalty로 추가 (distribution 전용)
                try:
                    grade_terms = add_grade_constraints(
                        m=m,
                        rs=roster_system,
                        X=X,
                        join=join,
                        leave=leave,
                        grade_strategy=str(getattr(roster_system, "grade_strategy", "BASE")),
                        grade_config=getattr(roster_system, "grade_config", None),
                    )
                    obj.extend(grade_terms or [])
                except Exception as e:
                    print('grade_constraints 예외 발생')
                    print('e', e)
                    pass

                m.Maximize(sum(obj))

            return m, X, short_terms, over_terms, safety, short_vars_by_day_code, over_vars_by_day_code

        # ───── 1단계: 커버리지 (완화 재시도 포함) ─────
        m1, X1, short1, over1, safety1 = None, None, None, None, None
        short_map1 = {}
        over_map1 = {}
        s1 = None
        best_short, best_over = None, None
        used_relax_level = 0  # 1단계에서 성공한 완화 레벨
        max_relax_attempts = 5  # 최대 5회까지 완화 재시도
        time_per_attempt = max(3, tl1 // max_relax_attempts)  # 각 시도당 시간 (최소 3초)
        
        for relax_level in range(max_relax_attempts):
            with Timer(f"폴백 1단계: 커버리지 부족 최소화 (완화레벨={relax_level})"):
                m1, X1, short1, over1, safety1, short_map1, over_map1 = build_model(stage=1, relax_level=relax_level)
                s1 = cp_model.CpSolver()
                s1.parameters.max_time_in_seconds = time_per_attempt
                s1.parameters.num_search_workers = 8
                s1.parameters.relative_gap_limit = 0.15
                st = s1.Solve(m1)
                if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    best_short = int(s1.Value(sum(short1)))
                    best_over = int(s1.Value(sum(over1)))
                    used_relax_level = relax_level
                    if relax_level > 0:
                        print(f"{self.logger_prefix} 폴백1 성공: 완화레벨 {relax_level} 적용 (월 최대 OFF 상한 +{relax_level})")
                    print(f"{self.logger_prefix} 최소 커버리지 부족: {best_short}, 과잉: {best_over}")
                    # 일/교대별 부족·과잉 상세 로그
                    try:
                        short_items = []
                        for (d, code), var in short_map1.items():
                            val = s1.Value(var)
                            if val > 0:
                                short_items.append((d, code, val))
                        if short_items:
                            short_items.sort()
                            print(f"{self.logger_prefix} [Stage1 부족 상세] day,shift,shortage =", short_items)
                        over_items = []
                        for (d, code), var in over_map1.items():
                            val = s1.Value(var)
                            if val > 0:
                                over_items.append((d, code, val))
                        if over_items:
                            over_items.sort()
                            print(f"{self.logger_prefix} [Stage1 과잉 상세] day,shift,over =", over_items)
                    except Exception as exc:
                        print(f"{self.logger_prefix} [Stage1 상세로그 실패]: {exc}")
                    break
                else:
                    if relax_level < max_relax_attempts - 1:
                        print(f"{self.logger_prefix} 폴백1 실패 (완화레벨={relax_level}): 재시도...")
                    else:
                        print(f"{self.logger_prefix} 폴백1 최종 실패: 모든 완화 시도 실패")
        
        if best_short is None or best_over is None:
            return False

        # ───── 2단계: 안전/법규 ─────
        with Timer("폴백 2단계: 안전/법규 위반 최소화"):
            m2, X2, short2, over2, safety2, short_map2, over_map2 = build_model(stage=2, coverage_eq=best_short, over_le=best_over, relax_level=used_relax_level)
            s2 = cp_model.CpSolver()
            s2.parameters.max_time_in_seconds = tl2
            s2.parameters.num_search_workers = 8
            s2.parameters.relative_gap_limit = 0.15
            st2 = s2.Solve(m2)
            if st2 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                print(f"{self.logger_prefix} 폴백2 실패: 단계 불가능 → 1단계 해 사용")
                roster_system.roster.fill(0)
                for n in range(N):
                    for d in range(join[n], leave[n] + 1):
                        for s in range(S):
                            if s1.Value(X1(n, d, s)):
                                roster_system.roster[n, d, s] = 1
                return best_short == 0
            # 안전 위반 총합 및 0-위치 목록 수집
            stage2_zero_locks = {}
            best_safe_sum = 0
            for k, arr in safety2.items():
                zeros = []
                for v in arr:
                    val = s2.Value(v)
                    if val == 0:
                        zeros.append(v)
                    if isinstance(val, bool):
                        best_safe_sum += int(val)
                    else:
                        best_safe_sum += int(val)
                stage2_zero_locks[k] = zeros
            print(f"{self.logger_prefix} 최소 안전 위반 합: {best_safe_sum}")
            # 안전 위반 카테고리별 합계 로그
            try:
                for k, arr in safety2.items():
                    total_k = sum(int(s2.Value(v)) for v in arr)
                    if total_k > 0:
                        print(f"{self.logger_prefix} [Stage2 위반] {k} = {total_k}")
                # 부족·과잉은 stage1과 동일하게 고정되므로 참조 로그만 표시
                short_items = [(d, code, int(s2.Value(var))) for (d, code), var in short_map2.items() if int(s2.Value(var)) > 0]
                over_items = [(d, code, int(s2.Value(var))) for (d, code), var in over_map2.items() if int(s2.Value(var)) > 0]
                if short_items:
                    print(f"{self.logger_prefix} [Stage2 부족 참고] day,shift,shortage =", sorted(short_items))
                if over_items:
                    print(f"{self.logger_prefix} [Stage2 과잉 참고] day,shift,over =", sorted(over_items))
            except Exception as exc:
                print(f"{self.logger_prefix} [Stage2 상세로그 실패]: {exc}")

        # ───── 3단계: 선호/공정성 ─────
        with Timer("폴백 3단계: 선호/공정성 최대화"):
            m3, X3, short3, over3, safety3, short_map3, over_map3 = build_model(stage=3, coverage_eq=best_short, over_le=best_over, stage2_zero_locks=stage2_zero_locks, relax_level=used_relax_level)
            # 합계 동일성(위반 재배치 억제): 각 카테고리 합은 stage2와 동일하게 유지
            for k in safety3.keys():
                m3.Add(sum(safety3[k]) == sum(safety2[k]))
            # 힌트: stage2 해를 힌트로 제공
            for n in range(N):
                for d in range(join[n], leave[n] + 1):
                    for s in range(S):
                        try:
                            m3.AddHint(X3(n, d, s), s2.Value(X2(n, d, s)))
                        except Exception:
                            pass
            s3 = cp_model.CpSolver()
            s3.parameters.max_time_in_seconds = tl3
            s3.parameters.num_search_workers = 8
            s3.parameters.relative_gap_limit = 0.05
            st3 = s3.Solve(m3)
            if st3 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                print(f"{self.logger_prefix} 폴백3 실패: 선호 단계 불가능 → 2단계 해 사용")
                roster_system.roster.fill(0)
                for n in range(N):
                    for d in range(join[n], leave[n] + 1):
                        for s in range(S):
                            if s2.Value(X2(n, d, s)):
                                roster_system.roster[n, d, s] = 1
                return best_short == 0 and best_safe_sum == 0
            # 선호 단계 상세 로그
            try:
                short_items = [(d, code, int(s3.Value(var))) for (d, code), var in short_map3.items() if int(s3.Value(var)) > 0]
                over_items = [(d, code, int(s3.Value(var))) for (d, code), var in over_map3.items() if int(s3.Value(var)) > 0]
                if short_items:
                    print(f"{self.logger_prefix} [Stage3 부족 참고] day,shift,shortage =", sorted(short_items))
                if over_items:
                    print(f"{self.logger_prefix} [Stage3 과잉 참고] day,shift,over =", sorted(over_items))
                for k, arr in safety3.items():
                    total_k = sum(int(s3.Value(v)) for v in arr)
                    if total_k > 0:
                        print(f"{self.logger_prefix} [Stage3 위반] {k} = {total_k}")
            except Exception as exc:
                print(f"{self.logger_prefix} [Stage3 상세로그 실패]: {exc}")

        # stage3 해 반영
        roster_system.roster.fill(0)
        for n in range(N):
            for d in range(join[n], leave[n] + 1):
                for s in range(S):
                    if s3.Value(X3(n, d, s)):
                        roster_system.roster[n, d, s] = 1
        # 후처리: 불필요한 O 재배치로 연속근무 완화
        try:
            print(f"{self.logger_prefix} [PostOff] 시작: 최종 stage3 해 기반 후처리 시도")
            before_viol = len(roster_system._find_violations())
            self._postprocess_rebalance_off(roster_system)
            after_viol = len(roster_system._find_violations())
            print(
                f"{self.logger_prefix} [PostOff] 종료: viol {before_viol}->{after_viol} "
                f"(감소={before_viol - after_viol})"
            )
        except Exception as exc:
            print(f"{self.logger_prefix} [PostOff] 후처리 실패: {exc}")

        print(f"{self.logger_prefix} 폴백 완료: 커버리지부족={best_short}, 안전위반합={best_safe_sum}")
        return best_short == 0 and best_safe_sum == 0

    def _quick_initial_solve(self, rs: RosterSystem,
                             tl:int, grouped, run_seed: int | None = None):
        from ortools.sat.python import cp_model
        try:
            model,X,j,l,fixed = _build_full_model(rs,grouped)
            # print('model', model)
            print('X', X)
            print('j', j)
            print('l', l)
            print('fixed', fixed)
            solver=cp_model.CpSolver()
            # ▼▼ 랜덤화 추가 ▼▼
            # seed = getattr(rs.config, 'random_seed', None)
            # if seed is None:
            #     # 매 실행마다 다르게: 시간+랜덤믹스
            #     seed = (int(time.time()*1000) ^ random.getrandbits(31)) & 0x7fffffff
            if run_seed is None:
                run_seed = random.randint(1, 1_000_000_000)
            solver.parameters.randomize_search = True
            solver.parameters.random_seed = (run_seed ^ 0x9E3779B1) & 0x7fffffff
            solver.parameters.solution_pool_size = 10
            # ▲▲ 랜덤화 추가 ▲▲

            solver.parameters.max_time_in_seconds=tl
            solver.parameters.num_search_workers=2
            solver.parameters.relative_gap_limit = 0.1
            stat=solver.Solve(model)
            print('stat', stat)
            if stat not in (cp_model.OPTIMAL,cp_model.FEASIBLE): return False
            rs.roster.fill(0)
            N,D,S=len(rs.nurses),rs.num_days,rs.config.num_shifts
            for n in range(N):
                for d in range(j[n],l[n]+1):
                    for s in range(S):
                        if solver.Value(X(n,d,s)): rs.roster[n,d,s]=1
        except Exception as e:
            print(f"[ERR] _quick_initial_solve:", e)
            return False
        return True
    
    def _convert_result_to_db_format(
        self,
        roster_system: RosterSystem,
        nurses: List[Nurse],
        canonical_to_shift_id: dict[str, str] | None = None,
        fixed_original_shift_map: dict[tuple[int, int], str] | None = None,
    ) -> Dict[str, List[str]]:
        """RosterSystem 결과를 DB 형식으로 변환한다.

        Args:
            roster_system: 계산이 완료된 RosterSystem
            nurses: 간호사 객체 리스트
            canonical_to_shift_id: 메인 코드(D/E/N/O) → 실제 shift_id 매핑
            fixed_original_shift_map: 고정 셀의 원본 shift_id 매핑
        """
        result = {}
        canonical_map = {k.upper(): v for k, v in (canonical_to_shift_id or {}).items() if v}
        if not canonical_map:
            canonical_map = {"D": "D", "E": "E", "N": "N", "O": "O", "주": "주"}
        fixed_original_shift_map = fixed_original_shift_map or {}
        shift_map = {i: s for i, s in enumerate(roster_system.config.shift_types)}
        fixed = getattr(roster_system, 'fixed_cells', None)
        fixed_lookup = {}
        if fixed:
            for cell in fixed:
                fixed_lookup[(cell['nurse_index'], cell['day_index'])] = cell['shift']
        for n_idx, nurse in enumerate(nurses):
            nurse_schedule = []
            for day_idx in range(roster_system.num_days):
                # 고정된 셀은 원래 값으로 반환
                if (n_idx, day_idx) in fixed_lookup:
                    original = fixed_original_shift_map.get((n_idx, day_idx))
                    if original:
                        nurse_schedule.append(original)
                        continue
                    fixed_shift = fixed_lookup[(n_idx, day_idx)]
                    nurse_schedule.append(canonical_map.get(str(fixed_shift).upper(), fixed_shift))
                    continue
                shift_vector = roster_system.roster[n_idx, day_idx]
                shift_idx = np.where(shift_vector == 1)[0]
                if len(shift_idx) > 0:
                    shift_id = shift_map[shift_idx[0]]
                    mapped = canonical_map.get(str(shift_id).upper(), shift_id)
                    nurse_schedule.append(mapped)
                else:
                    nurse_schedule.append('-')
            result[nurse.db_id] = nurse_schedule
        return result

    def _postprocess_rebalance_off(
        self,
        roster_system: RosterSystem,
        max_attempts: int = 30,
    ) -> None:
        """후처리로 불필요한 O를 당겨와 연속근무 위반을 완화합니다.

        수식(연속근무 길이): run_len = work_days 연속 길이 (예: DDDDDEE → run_len=6)

        전략:
            - 한 간호사 내에서 O가 있는 다른 날짜와 장시간 연속근무 구간의 근무를 교환
            - 밤근무(N)는 2N2O/야간 연속 규칙을 해치지 않도록 스왑 대상에서 제외
            - 하드 제약 위반 수가 늘어나면 롤백

        Args:
            roster_system: 근무표 시스템
            max_attempts: 최대 스왑 시도 횟수
        """
        cfg = roster_system.config
        off_idx = cfg.shift_types.index('O') if 'O' in cfg.shift_types else None
        night_idx = cfg.shift_types.index('N') if 'N' in cfg.shift_types else None
        if off_idx is None:
            return

        fixed_cells = {(c['nurse_index'], c['day_index']) for c in (getattr(roster_system, 'fixed_cells', []) or [])}
        K = getattr(cfg, "max_consecutive_work_days", None)
        if not isinstance(K, int) or K <= 0:
            return

        def _max_run(n_idx: int) -> tuple[int, int, int] | None:
            """가장 긴 연속근무 구간(start, end, length)을 반환."""
            best = None
            run_start = None
            run_len = 0
            D = roster_system.num_days
            for d in range(D):
                is_off = roster_system.roster[n_idx, d, off_idx] == 1
                if is_off:
                    if run_len > 0 and (best is None or run_len > best[2]):
                        best = (run_start, d - 1, run_len)
                    run_start, run_len = None, 0
                else:
                    if run_start is None:
                        run_start = d
                    run_len += 1
            if run_len > 0 and (best is None or run_len > best[2]):
                best = (run_start, D - 1, run_len)
            return best

        def _swap_off(n_idx: int, work_day: int, off_day: int, shift_idx: int) -> None:
            roster_system.roster[n_idx, work_day, shift_idx] = 0
            roster_system.roster[n_idx, work_day, off_idx] = 1
            roster_system.roster[n_idx, off_day, off_idx] = 0
            roster_system.roster[n_idx, off_day, shift_idx] = 1

        def _find_shift_idx(n_idx: int, day: int) -> int | None:
            vec = roster_system.roster[n_idx, day]
            ones = np.where(vec == 1)[0]
            if len(ones) == 1:
                return int(ones[0])
            return None

        base_viol = len(roster_system._find_violations())
        accepted = 0
        N = len(roster_system.nurses)

        for n_idx in range(N):
            if accepted >= max_attempts:
                break
            best_run = _max_run(n_idx)
            if not best_run or best_run[2] <= K:
                continue
            start, end, run_len = best_run
            # 연속근무 중앙부부터 완화 시도
            target_days = list(range(start, end + 1))
            target_days.sort(key=lambda x: abs(x - (start + end) // 2))

            # O 후보: 동일 간호사의 O 날짜 중 고정 아닌 날
            off_candidates = [
                d for d in range(roster_system.num_days)
                if roster_system.roster[n_idx, d, off_idx] == 1 and (n_idx, d) not in fixed_cells
            ]
            if not off_candidates:
                continue

            for work_day in target_days:
                if (n_idx, work_day) in fixed_cells:
                    continue
                shift_idx = _find_shift_idx(n_idx, work_day)
                if shift_idx is None or shift_idx == off_idx:
                    continue
                # N 이동은 2N2O 리스크가 크므로 제외
                if night_idx is not None and shift_idx == night_idx:
                    continue

                for off_day in off_candidates:
                    if off_day == work_day:
                        continue
                    if (n_idx, off_day) in fixed_cells:
                        continue

                    # 스왑 적용
                    _swap_off(n_idx, work_day, off_day, shift_idx)
                    new_run = _max_run(n_idx)
                    new_viol = len(roster_system._find_violations())

                    ok = True
                    if new_run and new_run[2] > K:
                        ok = False
                    if new_viol > base_viol:
                        ok = False

                    if ok:
                        accepted += 1
                        base_viol = new_viol
                        print(
                            f"{self.logger_prefix} [PostOff] swap accepted n={n_idx}, "
                            f"work_day={work_day+1}→O, off_day={off_day+1}→{cfg.shift_types[shift_idx]}, "
                            f"max_run {run_len}->{new_run[2] if new_run else 0}, viol {new_viol}"
                        )
                        break
                    else:
                        # 롤백
                        _swap_off(n_idx, off_day, work_day, shift_idx)
                if accepted >= max_attempts:
                    break
        final_viol = len(roster_system._find_violations())
        print(
            f"{self.logger_prefix} [PostOff] 완료: 수용 스왑 {accepted}건, "
            f"위반 {base_viol}->{final_viol}, max_attempts={max_attempts}"
        )

    def _log_final_roster(self, nurses: List[Nurse], roster_map: Dict[str, List[str]]) -> None:
        """최종 근무표를 간호사별로 출력합니다.

        Args:
            nurses: 간호사 객체 리스트
            roster_map: DB ID를 키로 하는 간호사별 근무표
        """
        try:
            for nurse in nurses:
                schedule = roster_map.get(nurse.db_id, [])
                schedule_str = " ".join(schedule) if schedule else "-"
                print(f"{self.logger_prefix} 배정표 {nurse.name}({nurse.db_id}): {schedule_str}")
        except Exception as exc:
            print(f"{self.logger_prefix} 근무표 출력 중 오류: {exc}")
    
    def _print_optimization_results(self, roster_system: RosterSystem, preceptor_pairs=None):
        """최적화 결과 출력 및 만족도 데이터 반환"""
        print(f"\n{self.logger_prefix} 최적화 결과:")
        
        # 위반사항 확인
        violations = roster_system._find_violations()
        if violations:
            print(f"  - {len(violations)}개의 제약 위반 사항 발견")
            for v in violations[:5]:  # 처음 5개만 표시
                print(f"    • {v}")
            if len(violations) > 5:
                print(f"    ... 및 {len(violations) - 5}개 더")
        else:
            print("  - 모든 제약 조건 충족!")
        
        # 만족도 데이터 수집
        satisfaction_data = {
            "off_satisfaction": 0.0,
            "shift_satisfaction": 0.0,
            "pair_satisfaction": 0.0,
            "individual_satisfaction": {},
            "detailed_analysis": {}
        }
        
        # 선호도 만족도 계산
        try:
            off_satisfaction = roster_system._calculate_off_preference_satisfaction()
            satisfaction_data["off_satisfaction"] = off_satisfaction
            print(f"  - 선호 휴무일 만족도: {off_satisfaction:.2f}%")
            
            shift_satisfaction = roster_system._calculate_shift_preference_satisfaction()
            satisfaction_data["shift_satisfaction"] = shift_satisfaction
            print(f"  - 근무 유형 선호도 만족도: {shift_satisfaction:.2f}%")
            
            if hasattr(roster_system, 'pair_matrix'):
                pair_satisfaction = roster_system._calculate_pair_preference_satisfaction()
                satisfaction_data["pair_satisfaction"] = pair_satisfaction.get('overall', 0.0)
                print(f"  - 페어링 선호도 만족도: {pair_satisfaction.get('overall', 0.0):.2f}%")
            
            # 개인별 만족도 계산
            individual_satisfaction = roster_system.calculate_individual_satisfaction()
            satisfaction_data["individual_satisfaction"] = individual_satisfaction
            # 개인별 페어링 만족도 출력
            if hasattr(roster_system, 'pair_matrix'):
                print("  - 개인별 페어링 만족도:")
                for nurse_id, info in individual_satisfaction.items():
                    pair_req_cnt = info.get('pair_request_count', 0)
                    # if pair_req_cnt == 0:
                    #     print(f"    • {info['name']}({info['nurse_id']}): -")
                    # else:
                    #     print(f"    • {info['name']}({info['nurse_id']}): {info['pair_satisfaction']:.2f}%")

            # 개인별 선호 휴무일/근무 유형 만족도 출력
            print("  - 개인별 선호 휴무일 만족도:")
            for nurse_id, info in individual_satisfaction.items():
                off_req_cnt = info.get('off_request_count', 0)
                # if off_req_cnt == 0:
                #     print(f"    • {info['name']}({info['nurse_id']}): -")
                # else:
                #     print(f"    • {info['name']}({info['nurse_id']}): {info['off_satisfaction']:.2f}%")
            
            print("  - 개인별 근무 유형 선호도 만족도:")
            for nurse_id, info in individual_satisfaction.items():
                shift_req_cnt = info.get('shift_request_count', 0)
                # if shift_req_cnt == 0:
                #     print(f"    • {info['name']}({info['nurse_id']}): -")
                # else:
                #     print(f"    • {info['name']}({info['nurse_id']}): {info['shift_satisfaction']:.2f}%")
            
            # 상세 요청 분석
            detailed_analysis = roster_system.calculate_detailed_request_analysis()
            satisfaction_data["detailed_analysis"] = detailed_analysis
            
        except Exception as e:
            print(f"  - 만족도 계산 중 오류: {e}")
        
        # ───────────────── 프리셉터 겹침률 출력 ─────────────────
        try:
            if preceptor_pairs:
                print("\n  - 프리셉터 페어링 반영률:")
                off_idx = roster_system.config.shift_types.index('O')
                # 근무 코드 중 실제 근무(D/E/N) 인덱스
                work_shift_idxs = [roster_system.config.shift_types.index(code) for code in roster_system.config.daily_shift_requirements.keys()]
                dbid_to_idx = {n.db_id: n.id for n in roster_system.nurses}
                for mentee_dbid, preceptor_dbid in preceptor_pairs:
                    n1 = dbid_to_idx.get(mentee_dbid)
                    n2 = dbid_to_idx.get(preceptor_dbid)
                    if n1 is None or n2 is None:
                        continue
                    same_shift_days = 0
                    both_worked_days = 0
                    for d in range(roster_system.num_days):
                        s1 = int(np.argmax(roster_system.roster[n1, d]))
                        s2 = int(np.argmax(roster_system.roster[n2, d]))
                        if s1 != off_idx and s2 != off_idx:
                            both_worked_days += 1
                            if s1 == s2 and s1 in work_shift_idxs:
                                same_shift_days += 1
                    rate = (same_shift_days / both_worked_days * 100.0) if both_worked_days > 0 else 0.0
                    mentee_name = next((n.name for n in roster_system.nurses if n.id == n1), str(mentee_dbid))
                    preceptor_name = next((n.name for n in roster_system.nurses if n.id == n2), str(preceptor_dbid))
                    # print(f"    • {mentee_name}({mentee_dbid}) - {preceptor_name}({preceptor_dbid}): 같은 근무 {same_shift_days}일 / 동시근무 {both_worked_days}일 ({rate:.2f}%)")
                # 만족도 데이터에 요약 저장
                satisfaction_data.setdefault("preceptor_overlap", {})
                satisfaction_data["preceptor_overlap"]["note"] = "같은 교대 배정일수 / 두 사람 모두 근무한 일수 기준"
        except Exception as e:
            print(f"  - 프리셉터 반영률 계산 중 오류: {e}")
        
        return satisfaction_data

# ================== Helper 함수 ==========================

def _build_full_model(rs: RosterSystem, grouped, include_pair_objective: bool = True):
    from ortools.sat.python import cp_model
    m = cp_model.CpModel()
    N,D,S = len(rs.nurses), rs.num_days, rs.config.num_shifts
    # join / leave index
    join, leave = [], []
    has_w = "W" in rs.config.shift_types
    w_idx = rs.config.shift_types.index("W") if has_w else None
    first_day = rs.target_month
    last_day = first_day + timedelta(days=D-1)

    for nu in rs.nurses:
        # join
        if nu.joining_date:
            j = (nu.joining_date - first_day).days
        else:
            j = 0

        # leave
        if nu.resignation_date:
            if nu.resignation_date < first_day:
                # 이번 달 이전 퇴사 → 이번 달 근무 대상 아님
                join.append(1)      # dummy
                leave.append(0)     # join > leave → 아래에서 제외 처리
                continue
            elif nu.resignation_date > last_day:
                l = D-1
            else:
                l = (nu.resignation_date - first_day).days
        else:
            l = D-1

        j = max(j, 0)
        l = min(l, D-1)

        join.append(j)
        leave.append(l)
    # 고정 셀 (수간호사 등)
    code2main = {c:r['main_code']
                 for r in (grouped or []) for c in r['codes']}
    fixed, fixed_cnt = {}, [[0]*S for _ in range(D)]
    for c in getattr(rs,'fixed_cells',[]) or []:
        n,d = c['nurse_index'], c['day_index']
        s_main = code2main.get(c['shift'], c['shift'])
        s_idx  = rs.config.shift_types.index(s_main)
        fixed[(n,d)] = s_idx; fixed_cnt[d][s_idx]+=1


    # 변수
    Xv={}
    for n in range(N):
        for d in range(join[n], leave[n]+1):
            for s in range(S):
                Xv[n,d,s]=m.NewBoolVar(f'x_{n}_{d}_{s}')
    def X(n,d,s):  return Xv.get((n,d,s),0)
    active_days = {(n, d) for n in range(N) for d in range(join[n], leave[n] + 1)}
    
    # ───────────── 2-A. 고정 셀  ─────────────
    for (n,d),s_idx in fixed.items():
        if (n, d) not in active_days:
            print(f"[CP-SAT-Basic] 고정 셀 무시: n={n}, d={d+1} (퇴사/입사 범위 밖)")
            continue
        m.Add(X(n,d,s_idx)==1)
        for s in range(S):
            if s!=s_idx: m.Add(X(n,d,s)==0)
    # W(특별 근무)는 고정 셀 외에는 전부 금지
    if has_w and w_idx is not None:
        for n in range(N):
            for d in range(join[n], leave[n] + 1):
                if (n, d) in fixed and fixed[(n, d)] == w_idx:
                    continue
                m.Add(X(n, d, w_idx) == 0)
    # ───────────── 2-A2. 초기 금지 셀(경계 제약) ─────────────
    try:
        if hasattr(rs, 'initial_forbidden') and isinstance(rs.initial_forbidden, dict):
            for (n, d), code_list in rs.initial_forbidden.items():
                for code in (code_list or []):
                    if code not in rs.config.shift_types:
                        continue
                    s_idx = rs.config.shift_types.index(code)
                    if (n, d) not in active_days:
                        print(f"[CP-SAT-Basic] 초기 금지 무시: n={n}, d={d+1}, code={code} (퇴사/입사 범위 밖)")
                        continue
                    if (n,d) in fixed and fixed[(n,d)] == s_idx:
                        print(f"[CP-SAT-Basic] 경고: 초기 금지와 고정 충돌 (n={n}, d={d+1}, code={code})")
                    m.Add(X(n,d,s_idx)==0)
    except Exception as e:
        print(f"[CP-SAT-Basic] 초기 금지 셀 적용 중 오류: {e}")

    # ───────────── 2-B. Exactly-one ──────────
    for n in range(N):
        for d in range(join[n], leave[n]+1):
            if (n,d) in fixed: continue
            m.AddExactlyOne(X(n,d,s) for s in range(S))

    # ───────────── 2-C. Shift requirements (per-day, slack 허용) ───
    coverage_shortage_vars = []
    over_vars_by_day = {}
    cfg = rs.config
    for d in range(D):
        # 일자별 요구치 우선 사용
        if hasattr(cfg, 'daily_shift_requirements_by_day') and isinstance(cfg.daily_shift_requirements_by_day, list) and d < len(cfg.daily_shift_requirements_by_day):
            need_map = cfg.daily_shift_requirements_by_day[d]
        else:
            need_map = cfg.daily_shift_requirements
        for code, req in need_map.items():
            if code not in rs.config.shift_types:
                continue
            s = rs.config.shift_types.index(code)
            need = int(req) - fixed_cnt[d][s]
            if need <= 0:
                continue
            assigned = sum(
                X(n, d, s)
                for n in range(N)
                if join[n] <= d <= leave[n] and (n, d) not in fixed
            )
            sh = m.NewIntVar(0, N, f'short_{d}_{code}')
            m.Add(sh >= need - assigned)
            coverage_shortage_vars.append((sh, code))
            # oversupply 추적: 추가 투입 인원 수 ov ≥ assigned - need
            ov = m.NewIntVar(0, N, f'over_{d}_{code}')
            m.Add(ov >= assigned - need)
            over_vars_by_day.setdefault(d, {})[code] = ov

    # shorthand indices
    idx = {c:rs.config.shift_types.index(c) for c in ('D','E','N','O')}
    day,eve,night,off = idx['D'],idx['E'],idx['N'],idx['O']
    # 주말(토/일) day_idx 집합(0-based)
    weekend_days = {d for d in range(D) if (rs.target_month + timedelta(days=d)).weekday() >= 5}

    # ───────────── 3. Hard 법규 ───────────────
    cfg = rs.config
    K   = cfg.max_consecutive_work_days
    L   = cfg.max_consecutive_nights

    for n,nu in enumerate(rs.nurses):
        T0,T1 = join[n], leave[n]
        # 주말 휴무 제약: is_weekend_off=True인 간호사는 주말에만 휴무, 평일에는 휴무 금지
        if bool(getattr(nu, "is_weekend_off", False)) and getattr(cfg, 'weekend_off_only_enable', True):
            for d in range(T0, T1 + 1):
                if d in weekend_days:
                    # 주말(토/일): OFF만 허용
                    if (n, d) in fixed and fixed[(n, d)] != off:
                        raise ValueError(
                            f"주말 고정 휴무 충돌: nurse_index={n}, day={d+1}, "
                            f"fixed_shift={rs.config.shift_types[fixed[(n, d)]]}, required=O"
                        )
                    m.Add(X(n, d, off) == 1)
                else:
                    # 평일(월~금): OFF 금지(D/E/N만 가능)
                    if (n, d) in fixed and fixed[(n, d)] == off:
                        raise ValueError(
                            f"주말 휴무 대상 간호사는 평일에 휴무를 받을 수 없습니다. "
                            f"nurse_index={n}, day={d+1}"
                        )
                    m.Add(X(n, d, off) == 0)
        # 연속 근무 K+1 중 OFF ≥1
        for d0 in range(T0, T1-K+1):
            m.Add(sum(X(n,d0+t,off) for t in range(K+1)) >= 1)

        # E→D, N→D, N→E
        for d in range(T0+1, T1+1):
            m.Add(X(n,d,day)+X(n,d-1,night)<=1)  # N→D 금지
            if cfg.banned_day_after_eve:
                m.Add(X(n,d,day)+X(n,d-1,eve)<=1)   # E→D 금지
                m.Add(X(n,d,eve)+X(n,d-1,night)<=1) # N→E 금지

        # Night-전담 (레거시 + 새로운 방식 모두 고려)
        raw = getattr(nu, "is_night_nurse", None)
        is_n_only = False
        if isinstance(raw, list):
            allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
            is_n_only = (allowed == {"N"})  # ["N"]만 허용 = N 전담
        elif raw == 3 or (raw is not None and raw != 0 and raw != False):
            # 레거시: is_night_nurse == 3도 N 전담으로 간주
            is_n_only = True
        if is_n_only:
            for d in range(T0,T1+1):
                m.Add(X(n,d,day)==0); m.Add(X(n,d,eve)==0)

        # 1N 금지: N 배정 시 인접일 중 최소 1일은 N 이어야 한다.
        # print(f"[NotOneNight] not_one_night: {bool(getattr(cfg, 'not_one_night', False))}")
        if bool(getattr(cfg, "not_one_night", False)):
            for d in range(T0, T1 + 1):
                neighbors = []
                if d - 1 >= T0:
                    neighbors.append(X(n, d - 1, night))
                if d + 1 <= T1:
                    neighbors.append(X(n, d + 1, night))
                if not neighbors:
                    continue
                m.Add(X(n, d, night) <= sum(neighbors))

        # 연속 Night
        for d0 in range(T0, T1-L+1):
            m.Add(sum(X(n,d0+t,night) for t in range(L+1)) <= L)

        # 월 Night 상한
        m.Add(sum(X(n,d,night) for d in range(T0,T1+1))
              <= cfg.max_night_shifts_per_month)

        # 월 최소 OFF 일수 하드 제약 (프론트 전달 off_days를 최소값으로 해석)
        try:
            base_min_off = int(getattr(cfg, 'global_monthly_off_days', 0) + getattr(cfg, 'standard_personal_off_days', 0))
            # 근무 가능 일수보다 클 수 있으므로 클램프
            min_off_required = min(base_min_off, T1 - T0 + 1)
            if min_off_required > 0:
                m.Add(sum(X(n,d,off) for d in range(T0, T1+1)) >= min_off_required)
            # 월 최대 OFF 상한: 최소 필요 OFF + 허용 초과 일수
            # N 전담 간호사는 OFF 상한을 동적으로 계산: 해당월 날짜수 - 최대 근무 가능일(15일)
            extra_allowed = int(getattr(cfg, 'max_extra_off_days', 0))
            if extra_allowed >= 0:
                if is_n_only:
                    # N 전담: 실제 근무 가능 일수(T1-T0+1)에서 최대 N 근무(15일)을 뺀 값
                    # 예) 28일 월 → 28-15=13, 30일 월 → 30-15=15, 31일 월 → 31-15=16
                    avail_days = T1 - T0 + 1
                    max_off_allowed_n_only = max(0, avail_days - 15)
                    m.Add(sum(X(n,d,off) for d in range(T0, T1+1)) <= max_off_allowed_n_only)
                else:
                    max_off_allowed = min(min_off_required + extra_allowed, T1 - T0 + 1)
                    m.Add(sum(X(n,d,off) for d in range(T0, T1+1)) <= max_off_allowed)
        except Exception:
            pass

        # N2/3→2OFF
        # 주의: "N 2회/3회 후 OFF 2회"는 다음 2일이 모두 OFF여야 한다.
        # 기존 구현은 (sum_n - 1 <= off1 + off2) 형태여서 연속 N일 때 OFF 1개만 허용되는 버그가 있었다.
        if cfg.two_offs_after_three_nig:
            for d in range(T0 + 2, T1 - 1):
                # (N_d-2 ∧ N_d-1 ∧ N_d) → (O_d+1 + O_d+2 == 2)
                m.Add(X(n, d + 1, off) + X(n, d + 2, off) == 2).OnlyEnforceIf(
                    [X(n, d, night), X(n, d - 1, night), X(n, d - 2, night)]
                )
        if cfg.two_offs_after_two_nig:
            for d in range(T0 + 1, T1 - 1):
                # 블록이 2N 이상이고 d가 블록의 끝일 때만 2O 강제 (2N1O 금지, 3N 허용)
                xn_prev = X(n, d - 1, night)
                xn_curr = X(n, d, night)
                xn_next = X(n, d + 1, night)
                end_block = m.NewBoolVar(f'end_2n_main_{n}_{d}')
                m.Add(end_block == xn_next.Not())
                m.Add(X(n, d + 1, off) + X(n, d + 2, off) == 2).OnlyEnforceIf(
                    [xn_prev, xn_curr, end_block]
                )

    # ───────────── 4. Soft (패널티 변수) ───────
    obj=[]
    P = rs.preference_matrix
    for n in range(N):
        nu = rs.nurses[n]
        # N 전담 간호사 여부 확인
        is_n_only = False
        raw = getattr(nu, "is_night_nurse", None)
        if isinstance(raw, list):
            allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
            is_n_only = (allowed == {"N"})
        elif raw == 3 or (raw is not None and raw != 0 and raw != False):
            is_n_only = True
        
        for d in range(join[n], leave[n]+1):
            for s in range(S):
                base_score = int(P[n,d,s]*100)
                # N 전담 간호사가 N을 배정받으면 높은 보너스 (N 우선 배정 유도)
                if is_n_only and s == night:
                    base_score += 500  # N 전담의 N 근무에 큰 보너스
                obj.append(base_score * X(n,d,s))

    # (4-0) 추가 OFF(여유 OFF) 기피: off_days만큼은 하드로 만족시키되, 남는 인원은 D/E/N으로 분배 유도
    try:
        off_penalty = int(getattr(cfg, "extra_off_penalty_weight", 0) or 0)
        if off_penalty > 0:
            for n in range(N):
                for d in range(join[n], leave[n] + 1):
                    obj.append(-off_penalty * X(n, d, off))
    except Exception:
        pass

    # (4-0a) 월단위 선호(개인 입력) 유도: 선호 교대에 소프트 보너스 부여
    # - Wanted(날짜 지정형)와 달리 "약한 선호"로 간주하며, weight는 req 게이지로 조절한다.
    try:
        msp = getattr(rs, "monthly_shift_preferences", None)
        base_w = int(getattr(cfg, "monthly_preference_weight", 0) or 0)
        if base_w > 0 and isinstance(msp, dict) and msp:
            for n, nu in enumerate(rs.nurses):
                pref = msp.get(str(getattr(nu, "db_id", ""))) or msp.get(getattr(nu, "db_id", ""))
                if not isinstance(pref, dict):
                    continue
                code = str(pref.get("shift") or "").strip().upper()
                if code not in {"D", "E", "N"}:
                    continue
                try:
                    strength = int(pref.get("strength", 5) or 0)
                except Exception:
                    strength = 5
                strength = max(0, min(10, strength))
                w = int(round(base_w * (strength / 10.0)))
                if w <= 0:
                    continue
                s_idx = cfg.shift_types.index(code)
                for d in range(join[n], leave[n] + 1):
                    obj.append(w * X(n, d, s_idx))
    except Exception:
        pass

    # (4-0b) 연속근무 소프트 상한: hard 제약은 유지하면서, "가능하면 더 짧게" 끊도록 유도한다.
    # - 창 크기 (soft_k + 1) 안에 OFF가 1개도 없으면 miss=1 → 패널티.
    # - 예: soft_k=5 이면 6연속근무(OFF 0) 창이 생길 때마다 패널티가 붙는다.
    try:
        soft_k = int(getattr(cfg, "soft_max_consecutive_work_days", 0) or 0)
        w_soft = int(getattr(cfg, "soft_consecutive_work_penalty_weight", 0) or 0)
        if soft_k > 0 and w_soft > 0:
            for n in range(N):
                T0, T1 = join[n], leave[n]
                for d0 in range(T0, T1 - soft_k + 1):
                    sum_off = sum(X(n, d0 + t, off) for t in range(soft_k + 1))
                    miss = m.NewIntVar(0, 1, f"soft_cwork_miss_{n}_{d0}")
                    m.Add(miss >= 1 - sum_off)
                    obj.append(-w_soft * miss)
    except Exception:
        pass

    # (4-1) 경력자 부족
    for d in range(D):
        for code in ('D','E','N'):
            s=rs.config.shift_types.index(code)
            exp_assigned = sum(X(n,d,s)
                               for n,nu in enumerate(rs.nurses)
                               if join[n]<=d<=leave[n] and
                               nu.experience_years>=cfg.min_experience_per_shift)
            shortage = m.NewIntVar(0, cfg.required_experienced_nurses, f'expShort_{d}_{code}')
            m.Add(shortage >= cfg.required_experienced_nurses - exp_assigned)
            obj.append(-200*shortage)

    # (4-2) 주 2 OFF
    if cfg.enforce_two_offs_per_week:
        weeks=D//7
        for n in range(N):
            for w in range(weeks):
                d0,d1=w*7,min(w*7+7,D)
                offs=sum(X(n,d,off) for d in range(d0,d1)
                         if join[n]<=d<=leave[n])
                slack = m.NewIntVar(0,2,f'weekSlack_{n}_{w}')
                m.Add(slack >= 2-offs); obj.append(-300*slack)

    # (4-3) 야간 균등 (편차에 선형 패널티)
    if cfg.even_nights:
        normals=[i for i,nu in enumerate(rs.nurses) if nu.is_night_nurse != 3]
        if normals:
            total_req=sum(cfg.daily_shift_requirements['N'] for _ in range(D))
            target=total_req//len(normals)
            for n in normals:
                totN=sum(X(n,d,night) for d in range(join[n],leave[n]+1))
                devP=m.NewIntVar(0,D,f'devP_{n}')
                devN=m.NewIntVar(0,D,f'devN_{n}')
                m.Add(devP-devN==totN-target)
                obj.extend([-50*devP,-50*devN])

    # (4-4) N-O-D/E 패터
    if getattr(cfg, 'nod_noe', True):
        for n in range(N):
            for d in range(join[n], leave[n]-2):
                pat=m.NewIntVar(0,1,f'NOD_{n}_{d}')
                m.Add(pat >= X(n,d,night)+X(n,d+1,off)+X(n,d+2,day)-2)
                obj.append(-100*pat)
                pat2=m.NewIntVar(0,1,f'NOE_{n}_{d}')
                m.Add(pat2 >= X(n,d,night)+X(n,d+1,off)+X(n,d+2,eve)-2)
                obj.append(-100*pat2)

    # (4-5) 고립 OFF
    for n in range(N):
        for d in range(join[n], leave[n]+1):
            iso=m.NewIntVar(0,1,f'iso_{n}_{d}')
            m.Add(iso >= X(n,d,off)-X(n,d-1,off)-X(n,d+1,off))
            m.Add(iso <= X(n,d,off))
            m.Add(iso <= 1-X(n,d-1,off))
            m.Add(iso <= 1-X(n,d+1,off))
            obj.append(-100*iso)
 
    # (4-6) 프리셉터/팀 보너스 항 모듈화
    if include_pair_objective:
        obj.extend(_add_preceptor_objective_terms(m, rs, X, join, leave))
        # 전략:
        # - GRADE: Team OFF
        # - TEAM : Team ON
        # - BASE : Team OFF
        grade_strategy = str(getattr(rs, "grade_strategy", "BASE") or "BASE").upper()
        # grade_strategy = "TEAM"
        print('grade_strategy', grade_strategy)
        if grade_strategy == "TEAM":
            obj.extend(add_team_balance_objective_terms(m, rs, X, join, leave))

    # (4-7) 커버리지 부족 패널티(메인 경로 slack 허용) – 날짜별 요구치 기반
    try:
        pr = float(getattr(cfg, 'shift_requirement_priority', 0.8))
        base = int(1000 * max(0.05, min(1.0, pr)))
        for sh, code in coverage_shortage_vars:
            w = base
            if code == 'N':
                w = int(base * 1.2)
            obj.append(-w * sh)
    except Exception:
        pass

    # (4-8) 여유 인원 L1 균등화: 동일 날짜의 D/E/N 간 초과 인원 차이를 최소화
    try:
        if bool(getattr(cfg, 'oversupply_equalize_enable', True)):
            w_eq = int(getattr(cfg, 'oversupply_equalize_weight', 120))
            for d, code2ov in over_vars_by_day.items():
                # 실제 근무 코드만 사용('O' 제외)
                work_codes = [code for code in code2ov.keys() if code in rs.config.daily_shift_requirements.keys()]
                for i in range(len(work_codes)):
                    for j in range(i + 1, len(work_codes)):
                        c1, c2 = work_codes[i], work_codes[j]
                        ov1, ov2 = code2ov[c1], code2ov[c2]
                        diff = m.NewIntVar(0, N, f'ov_diff_{d}_{c1}_{c2}')
                        m.Add(diff >= ov1 - ov2)
                        m.Add(diff >= ov2 - ov1)
                        obj.append(-w_eq * diff)
            
    except Exception:
        pass
    
    m.Maximize(sum(obj))

    return m,X,join,leave,fixed


# ─────────────────────────────────────────────────────────────
#           Neighbourhood solver  (전역 변수·제약 그대로)     │
# ─────────────────────────────────────────────────────────────
def _solve_neighbourhood(rs, n_set, d_set, tl, grouped, run_seed: int | None = None, it:int=0):
    from ortools.sat.python import cp_model
    model,X,j,l,fixed=_build_full_model(rs,grouped, include_pair_objective=False)

    # neighbourhood 외 셀은 현재 값 고정
    N,D,S=len(rs.nurses),rs.num_days,rs.config.num_shifts
    for n in range(N):
        for d in range(D):
            if (n in n_set) and (d in d_set): continue
            assigned=np.where(rs.roster[n,d]==1)[0]
            if len(assigned):
                s0=assigned[0]
                model.Add(X(n,d,s0)==1)
                for s in range(S):
                    if s!=s0: model.Add(X(n,d,s)==0)

    # 옵션: 프리셉터-포커스 이웃일 때만 해당 셀에 한정하여 보너스 항 주입
    try:
        focus_preceptor = bool(getattr(rs.config, 'preceptor_enable', True))
        if focus_preceptor and hasattr(rs, 'pair_matrix') and isinstance(rs.pair_matrix, dict):
            # 선택된 n_set,d_set의 셀만 대상으로 제한된 항 생성
            def X_sub(n,d,s):
                if (n in n_set) and (d in d_set):
                    return X(n,d,s)
                # neighbourhood 밖은 고정되어 있으므로 항의 영향 없음 처리(0 반환)
                return 0
            # join/leave를 그대로 써도 되지만 날짜 필터를 d_set로 제한
            # 간단하게 전역 헬퍼 재사용은 어려우므로, 최소한의 제한형 항만 주입
            together = rs.pair_matrix.get('together')
            if together is not None:
                cfg = rs.config
                strength = float(getattr(cfg, 'preceptor_strength_multiplier', 1.0))
                base_min = float(getattr(cfg, 'preceptor_min_pair_weight', 5.0))
                if getattr(cfg, 'preceptor_focus_shifts', None):
                    focus_codes = [c for c in cfg.preceptor_focus_shifts if c in cfg.daily_shift_requirements.keys()]
                else:
                    focus_codes = list(cfg.daily_shift_requirements.keys())
                shift_indices = [cfg.shift_types.index(c) for c in focus_codes]
                pref = rs.preference_matrix
                pref_sum_threshold = 1.2
                for n1 in n_set:
                    for n2 in n_set:
                        if n1>=n2: continue
                        base = together[n1,n2]
                        if base < base_min: continue
                        w = int(base * 100 * strength)
                        for d in d_set:
                            # 해당 날짜에서 최선의 교대만 선택
                            best=None; best_s=None
                            for s in shift_indices:
                                sc = pref[n1,d,s] + pref[n2,d,s]
                                if sc < pref_sum_threshold: continue
                                if best is None or sc>best:
                                    best,best_s=sc,s
                            if best is None: continue
                            z = model.NewBoolVar(f'pc_lns_{n1}_{n2}_{d}_{best_s}')
                            model.Add(z <= X_sub(n1,d,best_s))
                            model.Add(z <= X_sub(n2,d,best_s))
                            # 목적함수에 추가
                            # CP-SAT Python API에서는 Maximize가 호출 이전이면 terms 누적 가능
                            # 여기서는 model에 저장된 선형식이 없으므로 CpSolver쪽에서 자동 합산되도록 유지
                            # 간단하게: 목적은 build 단계의 obj에만 존재. 여기서는 AddMaxEquality 대신
                            # 리니어식으로 보정: solver Maximize 전에 선호 obj는 이미 존재하므로, 이 항을 모델의
                            # 계수화가 필요. OR-Tools는 명시 obj 합산 인터페이스 없음 → trick: 저장 후 아래에서 사용 안함
                            # 안전하게는 풀빌드에서만 목적에 넣고, LNS에서는 제약만으로 유도 불가 → 여기선 생략
                            # 대신 neighbourhood 풀빌드 자체가 include_pair_objective=False 이므로 성능 우선.
                            # 필요시 풀빌드 쪽 강도 상향으로 보정.
                            pass
    except Exception:
        pass

    solver=cp_model.CpSolver()
    if run_seed is not None:
        # 이웃/반복에 따라 seed 살짝 변조 → 다양성
        tweak = (hash(tuple(sorted(n_set))) ^ hash(tuple(sorted(d_set))) ^ (it * 0x9E3779B1)) & 0x7fffffff
        solver.parameters.randomize_search = True
        solver.parameters.random_seed = (run_seed ^ tweak) & 0x7fffffff
        solver.parameters.solution_pool_size = 10
    solver.parameters.max_time_in_seconds=tl
    solver.parameters.num_search_workers = 10
    solver.parameters.relative_gap_limit = 0.1
    st=solver.Solve(model)
    if st not in (cp_model.OPTIMAL,cp_model.FEASIBLE): return False

    # 반영
    for n in n_set:
        for d in d_set:
            for s in range(S):
                rs.roster[n,d,s]=1 if solver.Value(X(n,d,s)) else 0
    return True


def _add_preceptor_objective_terms(m, rs: RosterSystem, X, join, leave):
    """프리셉터(페어 together) 보너스 항을 생성하여 obj 리스트로 반환.
    - 하드 제약은 건드리지 않음. 소프트 보너스만 추가.
    - 설정 파라미터로 강도/탑-K/교대/하한값을 제어.
    - LNS에서는 호출자가 생략하거나 별도 이웃 주입으로 사용 가능.
    """
    obj_terms = []
    cfg = rs.config
    if not getattr(cfg, 'preceptor_enable', True):
        return obj_terms
    if not hasattr(rs, 'pair_matrix') or not isinstance(rs.pair_matrix, dict):
        return obj_terms
    together = rs.pair_matrix.get('together')
    if together is None:
        return obj_terms

    N, D = len(rs.nurses), rs.num_days
    # 유효 쌍 필터
    base_min = float(getattr(cfg, 'preceptor_min_pair_weight', 5.0))
    pairs = [(i, j2, together[i, j2]) for i in range(N) for j2 in range(i+1, N) if together[i, j2] >= base_min]
    if not pairs:
        return obj_terms

    # 교대 필터
    if getattr(cfg, 'preceptor_focus_shifts', None):
        focus_codes = [c for c in cfg.preceptor_focus_shifts if c in cfg.daily_shift_requirements.keys()]
    else:
        focus_codes = list(cfg.daily_shift_requirements.keys())
    shift_indices = [cfg.shift_types.index(c) for c in focus_codes]

    pref = rs.preference_matrix
    pref_sum_threshold = 1.2
    K_default = int(getattr(cfg, 'preceptor_top_days', 12))
    strength = float(getattr(cfg, 'preceptor_strength_multiplier', 1.0))

    import time as _t
    _t0 = _t.time(); _added=0
    for n1, n2, base in pairs:
        w = int(base * 100 * strength)
        d0, d1 = max(join[n1], join[n2]), min(leave[n1], leave[n2])
        scored = []
        for d in range(d0, d1+1):
            best=None; best_s=None
            for s in shift_indices:
                sc = pref[n1,d,s] + pref[n2,d,s]
                if sc < pref_sum_threshold:
                    continue
                if best is None or sc>best:
                    best, best_s = sc, s
            if best is not None:
                scored.append((best,d,best_s))
        K = min(K_default, len(scored))
        for _, d, s in sorted(scored, reverse=True)[:K]:
            z = m.NewBoolVar(f'pc_{n1}_{n2}_{d}_{s}')
            m.Add(z <= X(n1,d,s))
            m.Add(z <= X(n2,d,s))
            obj_terms.append(w * z)
            _added += 1
    _dt = _t.time()-_t0
    print(f"[CP-SAT-Basic] 프리셉터 항: 쌍 {len(pairs)}개, 변수 {_added}개, {_dt:.2f}s, 강도 {strength}x, K={K_default}, shifts={focus_codes}")
    return obj_terms


# ─────────────────────────────────────────────────────────────
# Grade 배치 요약/CSV 덤프
# ─────────────────────────────────────────────────────────────
import csv
import os
import hashlib


def _dump_grade_summary(rs: RosterSystem, nurses, grade_config: dict, logger_prefix: str = "[CP-SAT-Basic]"):
    """일자/교대별 Grade 배치 현황과 요구치 대비 비율을 출력하고 CSV로 저장한다."""
    constraints_map = grade_config.get("constraints") or grade_config.get("constraints_json") or {}
    if not constraints_map:
        print(f"{logger_prefix} Grade 요약: constraints 없음, 스킵")
        return

    shift_types = rs.config.shift_types
    # 추출된 grade 값 정의역
    grades = set()
    for _, gmap in constraints_map.items():
        if not isinstance(gmap, dict):
            continue
        for k in gmap.keys():
            try:
                grades.add(int(k))
            except Exception:
                continue
    grade_values = sorted(grades) or [1, 2, 3]

    # NULL Grade 처리 정책
    null_policy = str(grade_config.get("null_grade_policy") or "LOWEST").upper()
    valid_grades = [n.grade for n in nurses if getattr(n, "grade", None) is not None]
    avg_grade = round(sum(valid_grades) / len(valid_grades)) if valid_grades else max(grade_values)

    def _resolve_grade(nurse):
        g = getattr(nurse, "grade", None)
        if g is not None:
            try:
                gi = int(g)
                if gi in grade_values:
                    return gi
            except Exception:
                pass
        if null_policy == "AVERAGE":
            return avg_grade if avg_grade in grade_values else max(grade_values)
        if null_policy == "RANDOM":
            h = hashlib.md5(str(getattr(nurse, "db_id", nurse.name)).encode()).hexdigest()
            return grade_values[int(h[:8], 16) % len(grade_values)]
        # LOWEST
        return max(grade_values)

    nurse_grades = [ _resolve_grade(n) for n in nurses ]

    rows = []
    for d in range(rs.num_days):
        for shift_code, gmap in constraints_map.items():
            s_code = str(shift_code or "").upper()
            if s_code not in shift_types:
                continue
            s_idx = shift_types.index(s_code)
            # 배정된 간호사/grade 카운트
            assigned_by_grade = {g: 0 for g in grade_values}
            for n_idx in range(len(nurses)):
                # roster는 one-hot
                if int(rs.roster[n_idx, d, s_idx]) == 1:
                    g = nurse_grades[n_idx]
                    assigned_by_grade[g] = assigned_by_grade.get(g, 0) + 1
            # 요구치
            req_by_grade = {}
            for k, v in (gmap or {}).items():
                try:
                    gi = int(k)
                    req_by_grade[gi] = int(v)
                except Exception:
                    continue

            # 비율 계산 및 로그/CSV
            for g in grade_values:
                req = req_by_grade.get(g, 0)
                got = assigned_by_grade.get(g, 0)
                ratio = (got / req) if req > 0 else None
                rows.append({
                    "day": d + 1,
                    "shift": s_code,
                    "grade": g,
                    "required": req,
                    "assigned": got,
                    "ratio": ratio if ratio is not None else "",
                })

    # 로그 요약 (상위 몇 개)
    print(f"{logger_prefix} Grade 배치 요약 (일부)")
    for r in rows[: min(15, len(rows))]:
        print(f"  day={r['day']:2}, shift={r['shift']}, grade={r['grade']}: req={r['required']}, got={r['assigned']}, ratio={r['ratio']}")

    # CSV 저장
    out_path = os.path.join("/tmp", "grade_summary.csv")
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["day", "shift", "grade", "required", "assigned", "ratio"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"{logger_prefix} Grade 요약 CSV 저장: {out_path}")


cp_sat_engine = CPSATBasicEngine()

def generate_roster_cp_sat(
    nurses_data,
    prefs_data,
    config_data,
    year,
    month,
    shift_manage_data,
    time_limit_seconds=60,
    randomize=True,
    seed=None,
    grade_strategy: str = "BASE",
    grade_config: dict | None = None,
):
    """
    기존 roster_engine.generate_roster 함수와 호환되는 인터페이스
    
    Args:
        nurses_data: DB에서 가져온 간호사 데이터 리스트  
        prefs_data: DB에서 가져온 선호도 데이터 리스트
        config_data: DB에서 가져온 설정 데이터
        year: 근무표 년도
        month: 근무표 월
        time_limit_seconds: CP-SAT 최적화 시간 제한
        
    Returns:
        Dict[nurse_id, List[shift]]: 간호사별 일일 근무 배정
    """
    return cp_sat_engine.generate_roster(
        nurses_data,
        prefs_data,
        config_data,
        year,
        month,
        shift_manage_data,
        grade_strategy=grade_strategy,
        grade_config=grade_config,
        time_limit_seconds=time_limit_seconds,
        randomize=randomize,
        seed=seed,
    ) 