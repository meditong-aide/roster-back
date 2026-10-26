from typing import List, Dict, Tuple, Optional, Union
import numpy as np
from datetime import date, datetime, timedelta
import calendar
import time
import pandas as pd
import logging
from db.roster_config import NurseRosterConfig, DEFAULT_CONFIG
from db.nurse_config import Nurse
from services.holiday_pack import get_weekends   # ← 주말 헬퍼

def _weekend_set(year: int, month: int) -> set[int]:
    """해당 월의 주말 날짜(1‑based)를 {0‑based day_idx} 로 반환."""
    return {d.day - 1 for d in get_weekends(year, month)}


class RosterSystem:
    """간호사 근무표 생성 및 관리를 위한 주요 클래스."""
    
    def __init__(
        self,
        nurses: List[Nurse],
        target_month: date = None,
        config: NurseRosterConfig = DEFAULT_CONFIG,
        year: int = None,
        month: int = None,
        shift_preferences: Dict = None,
        day_preferences: Dict = None,
        off_preferences: Dict = None,
        preference_matrix: Optional[np.ndarray] = None
    ):
        print("\nRosterSystem 초기화 중...")
        start_time = time.time()
        
        self.nurses = nurses
        # DB ID → 내부 인덱스 매핑 (연속하지 않은 ID를 안전하게 처리)
        self.dbid_to_idx = {n.db_id: idx for idx, n in enumerate(nurses)}
        self.config = config
        # target_month 설정 (하위 호환성을 위해)
        if target_month is not None:
            self.target_month = target_month
        elif year is not None and month is not None:
            self.target_month = date(year, month, 1)
        else:
            raise ValueError("target_month 또는 year, month가 필요합니다.")
            
        self.num_days = calendar.monthrange(self.target_month.year, self.target_month.month)[1]
        
        # 근무표 행렬 초기화: [간호사 × 일수 × 교대]
        self.roster = np.zeros((len(nurses), self.num_days, config.num_shifts))
        
        # 선호도 데이터 저장
        self.shift_preferences = shift_preferences or {}
        self.day_preferences = day_preferences or {}
        self.off_preferences = off_preferences or {}
        
        # 고정된 셀 정보
        self.fixed_cells = []
        
        # 선호도 행렬 설정
        if preference_matrix is not None:
            self.preference_matrix = preference_matrix
            print("외부 제공 선호도 행렬 사용.")
        else:
            self.preference_matrix = np.zeros_like(self.roster)
            # 선호도 행렬 초기화
            self._initialize_preferences()

        #### 수정된곳
        self.max_off_per_nurse = []
        for nurse in self.nurses:
            # 글로벌 + 기본 개인 + 개인 조정치(음/양수) = 총 허용 OFF
            max_allowed = (
                self.config.global_monthly_off_days
                + self.config.standard_personal_off_days
                + nurse.personal_off_adjustment
            )
            # 음수라도 0 이하로 떨어지지 않도록 보정
            self.max_off_per_nurse.append(max(0, max_allowed))
        ####
        # 로깅 설정
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
        
        print(f"초기화 완료: {time.time() - start_time:.4f}초 소요")
        
    def _initialize_preferences(self):
        """모든 간호사와 날짜에 대한 선호도 행렬을 초기화합니다."""
        print("선호도 행렬 계산 중...")
        start_time = time.time()
        
        weekend_days = _weekend_set(self.target_month.year, self.target_month.month)

        for n_idx, nurse in enumerate(self.nurses):
            for day in range(self.num_days):
                self.preference_matrix[n_idx, day] = nurse.get_shift_preferences(
                    day, self.num_days, self.config, weekend_days
                )
                
        print(f"선호도 행렬 계산 완료: {time.time() - start_time:.4f}초 소요")
       
    # ───────── 2. 야간 관련 개별 함수 🔄 ─────────
    def _check_consecutive_night_limit(self, nurse_idx: int, day: int) -> bool:
        """연속 야간 근무 수 초과 여부"""
        night_idx = self.config.shift_types.index('N')
        L = self.config.max_consecutive_nights+1
        
        if day < L:               # 검사할 이력이 부족
            return True
        return not np.all(self.roster[nurse_idx, day-L:day, night_idx] == 1)

    def _check_day_after_night(self, nurse_idx: int, day: int) -> bool:
        """전날 Night 근무 후 Day 근무 여부(N→D 금지)"""
        if day == 0:
            return True
        night_idx = self.config.shift_types.index('N')
        day_idx   = self.config.shift_types.index('D')
        return not (self.roster[nurse_idx, day-1, night_idx] == 1 and
                    self.roster[nurse_idx, day,   day_idx]   == 1)

    def _check_night_before_evening(self, nurse_idx: int, day: int) -> bool:
        """전날 Night 근무 후 당일 Evening 근무 여부(N→E 금지). 위반 시 False."""
        if day == 0:
            return True
        night_idx = self.config.shift_types.index('N')
        eve_idx   = self.config.shift_types.index('E')
        return not (self.roster[nurse_idx, day - 1, night_idx] == 1 and
                    self.roster[nurse_idx, day, eve_idx] == 1)

    def _check_eve_before_day(self, nurse_idx: int, day: int) -> bool:
        """전날 Evening 근무 후 당일 Day 근무 여부(E→D 금지). 위반 시 False."""
        if day == 0:
            return True
        eve_idx = self.config.shift_types.index('E')
        day_idx = self.config.shift_types.index('D')
        return not (self.roster[nurse_idx, day - 1, eve_idx] == 1 and
                    self.roster[nurse_idx, day, day_idx] == 1)

    def _check_monthly_night_limit(self, nurse_idx: int, day: int) -> bool:
        """월 누적 야간 근무 제한 초과 여부"""
        night_idx = self.config.shift_types.index('N')
        total_nights = np.sum(self.roster[nurse_idx, :day+1, night_idx])
        return total_nights <= self.config.max_night_shifts_per_month

    def _check_two_offs_after_three_night(self, nurse_idx: int, end_day: int) -> bool:
        """3연속 N 끝(end_day) 다음 2일이 모두 OFF인지(3N→2O). 위반 시 False."""
        if end_day < 2 or end_day + 2 >= self.num_days:
            return True
        night_idx = self.config.shift_types.index('N')
        off_idx = self.config.shift_types.index('O')
        r = self.roster[nurse_idx]
        if r[end_day - 2, night_idx] != 1 or r[end_day - 1, night_idx] != 1 or r[end_day, night_idx] != 1:
            return True
        return r[end_day + 1, off_idx] == 1 and r[end_day + 2, off_idx] == 1

    def _check_two_offs_after_two_night(self, nurse_idx: int, end_day: int) -> bool:
        """2연속 N 끝(end_day)이고 다음 날이 N이 아닐 때, 그 다음 2일이 모두 OFF인지(2N→2O). 위반 시 False."""
        if end_day < 1 or end_day + 2 >= self.num_days:
            return True
        night_idx = self.config.shift_types.index('N')
        off_idx = self.config.shift_types.index('O')
        r = self.roster[nurse_idx]
        if r[end_day - 1, night_idx] != 1 or r[end_day, night_idx] != 1:
            return True
        if r[end_day + 1, night_idx] == 1:
            return True  # 블록이 2N으로 끝나지 않음(다음 날도 N)
        return r[end_day + 1, off_idx] == 1 and r[end_day + 2, off_idx] == 1

    # ───────── 3. 연속 근무일 함수 리네이밍·정돈 🔄 ─────────
    def _check_max_consecutive_work_days(self, nurse_idx: int, day: int) -> bool:
        """max_consecutive_work_days 초과 여부만 판단"""
        max_work = self.config.max_consecutive_work_days
        if day < max_work:
            return True

        off_idx = self.config.shift_types.index('O')
        consecutive = 0
        for d in range(day, day-max_work-1, -1):
            if d < 0:
                break
            is_working = np.sum(self.roster[nurse_idx, d, :off_idx]) > 0
            if is_working:
                consecutive += 1
                if consecutive > max_work:
                    return False
            else:
                break
        return True

    def _check_experience_requirements(self, day: int) -> bool:
        """각 교대에 대한 경력 요구사항이 충족되는지 확인합니다."""
        experienced_nurses = [n for n in self.nurses if (n.experience_years or 0) >= self.config.min_experience_per_shift]
        
        for shift in self.config.daily_shift_requirements.keys():
            shift_idx = self.config.shift_types.index(shift)
            exp_count = sum(
                1 for n_idx, nurse in enumerate(experienced_nurses)
                if self.roster[n_idx, day, shift_idx] == 1
            )
            if exp_count < self.config.required_experienced_nurses:
                return False
        return True
        
    def apply_off_requests(self, off_requests: Dict[int, List[int]]):
        """
        간호사의 휴무(OFF) 요청을 날짜·강도(Δ-weight) 단위로 적용합니다.

        Args
        ----
        off_requests : {
            "간호사ID(문자열)": { "날짜(문자열)": delta_weight, ... }, ...
        }
        - 날짜는 1-based(예: "6" → 6일).
        - delta_weight 는 기본 OFF-가중치(config.shift_preference_weights["OFF"])
            에 더해질 추가점수.   (총점 = 기본 + delta)
        """
        off_idx     = self.config.shift_types.index("O")
        base_weight = self.config.shift_preference_weights.get("O", 10.0)
        for target_nurse_id, day_map in off_requests.items():

            valid_days = []
            for day_str, delta in day_map.items():
                d = int(day_str)
                if 1 <= d <= self.num_days:
                    valid_days.append(d)
            
            if not valid_days:
                continue


            # ★ 가중치 반영
            for d in valid_days:
                day_idx = d-1
                delta   = day_map[str(d)]
                n_idx = self.dbid_to_idx.get(target_nurse_id)
                if n_idx is None:
                    print(f"경고: 휴무 요청 대상 간호사(ID={target_nurse_id})를 찾을 수 없어 건너뜁니다.")
                    break
                self.preference_matrix[n_idx, day_idx, off_idx] = base_weight + delta
                # [S0 계측] 사용자 선호가 닿은 칸을 **원본 delta 와 함께** 기록한다.
                #   ★ 최종 점수(P)의 부호로 요청/기피를 가르면 안 된다 — baseline 이
                #     양수(default_weight=5 등)라 기피 -1.5 도 P=3.5 로 양수가 된다.
                #     신호는 delta 이므로 그것을 남긴다.
                #   ★ preference_matrix 전체로 재도 안 된다. 기본값이 전 칸에 깔린
                #     '스코어 매트릭스' 라 선호와 무관한 값이 나온다(실측 0.9484).
                self._pref_cells = getattr(self, "_pref_cells", {})
                self._pref_cells[(n_idx, day_idx, off_idx)] = float(delta)
            
    def apply_shift_preferences(self, shift_preferences: Dict[str, Dict[str, Dict[str, float]]]):
        """
        간호사의 특정 근무 유형(D, E, N)에 대한 날짜별 선호도를 반영합니다.

        shift_preferences 예시:
        {
            "1": {
                "D": {"4": 1.0, "5": 3.2},
                "E": {"10": 1.0, "11": 1.5}
            }
        }
        """
        print("근무 유형 선호도 적용 중...")
        for target_nurse_id, shifts in shift_preferences.items():
            
            if target_nurse_id is None:
                print(f"경고: ID {target_nurse_id}인 간호사를 찾을 수 없습니다.")
                continue
            
            for shift_type, day_weight_map in shifts.items():
                if shift_type not in self.config.shift_types:
                    print(f"경고: 유효하지 않은 근무 유형: {shift_type}")
                    continue
                
                shift_idx = self.config.shift_types.index(shift_type)
                default_weight = self.config.shift_preference_weights.get(shift_type, 5.0)
                for day_str, delta in day_weight_map.items():
                    day = int(day_str)
                    if 1 <= day <= self.num_days:
                        day_idx = day - 1
                        n_idx = self.dbid_to_idx.get(target_nurse_id)
                        if n_idx is None:
                            print(f"경고: 근무 유형 선호 대상 간호사(ID={target_nurse_id})를 찾을 수 없어 건너뜁니다.")
                            break
                        self.preference_matrix[n_idx, day_idx, shift_idx] = default_weight + delta
                        self._pref_cells = getattr(self, "_pref_cells", {})
                        self._pref_cells[(n_idx, day_idx, shift_idx)] = float(delta)
        print("근무 유형 선호도 적용 완료")
        
    def apply_pair_preferences(self, pair_preferences: Dict[str, List[Dict[str, Union[int, float]]]]):
        """간호사 간의 페어링 선호도를 적용합니다.
        
        Args:
            pair_preferences: 함께 또는 따로 일하고 싶은 간호사 쌍의 정보
                예: {
                    "work_together": [{"nurse_1": 1, "nurse_2": 5, "weight": 3.0}, ...],
                    "work_apart": [{"nurse_1": 1, "nurse_2": 6, "weight": 3.0}, ...]
                }
        """
        print("간호사 페어링 선호도 초기화 중...")
        # [S0 계측] pair 요청 건수. stage3 목적이 pair 도 최적화하므로 stage3 가
        #   INFEASIBLE 이면 같이 소실된다. 다만 pair 는 (간호사,일자,시프트) 셀이 아니라
        #   쌍 단위라 _pref_cells 로는 못 잰다 — 존재만 드러내고 별도 지표는 후속 과제로 둔다.
        try:
            self._pair_req_count = sum(
                len(v or []) for v in (pair_preferences or {}).values()
            )
        except Exception:
            self._pair_req_count = 0
        
        # 간호사 페어링 선호도 매트릭스 초기화 (간호사 × 간호사)
        self.pair_matrix = {
            "together": np.zeros((len(self.nurses), len(self.nurses))),
            "apart": np.zeros((len(self.nurses), len(self.nurses)))
        }
        
        # 요청자 기준(방향성) 저장 구조: (requester_idx, target_idx)
        self.pair_requests = {
            "together": set(),
            "apart": set()
        }
        
        # together (함께 일하기 원하는 쌍) 처리
        if "work_together" in pair_preferences:
            for pair in pair_preferences["work_together"]:
                nurse_1_id = pair["nurse_1"]
                nurse_2_id = pair["nurse_2"]
                weight = pair.get("weight", self.config.pair_preference_weight)
                source = pair.get("source")
                nurse_1_idx = self.dbid_to_idx.get(nurse_1_id)
                nurse_2_idx = self.dbid_to_idx.get(nurse_2_id)
                
                if nurse_1_idx is not None and nurse_2_idx is not None:
                    self.pair_matrix["together"][nurse_1_idx, nurse_2_idx] = weight
                    self.pair_matrix["together"][nurse_2_idx, nurse_1_idx] = weight  # 대칭적으로 설정
                    # 요청자 기준 저장 (대칭 저장하지 않음) — 사용자 요청만 기록
                    if source != 'preceptor':
                        self.pair_requests["together"].add((nurse_1_idx, nurse_2_idx))
                else:
                    if nurse_1_idx is None:
                        print(f"경고: ID {nurse_1_id}인 간호사를 찾을 수 없습니다.")
                    if nurse_2_idx is None:
                        print(f"경고: ID {nurse_2_id}인 간호사를 찾을 수 없습니다.")
        
        # apart (따로 일하기 원하는 쌍) 처리
        if "work_apart" in pair_preferences:
            for pair in pair_preferences["work_apart"]:
                nurse_1_id = pair["nurse_1"]
                nurse_2_id = pair["nurse_2"]
                weight = pair.get("weight", self.config.pair_preference_weight)
                nurse_1_idx = self.dbid_to_idx.get(nurse_1_id)
                nurse_2_idx = self.dbid_to_idx.get(nurse_2_id)
                
                if nurse_1_idx is not None and nurse_2_idx is not None:
                    self.pair_matrix["apart"][nurse_1_idx, nurse_2_idx] = weight
                    self.pair_matrix["apart"][nurse_2_idx, nurse_1_idx] = weight  # 대칭적으로 설정
                    # 요청자 기준 저장 (대칭 저장하지 않음)
                    self.pair_requests["apart"].add((nurse_1_idx, nurse_2_idx))
                else:
                    if nurse_1_idx is None:
                        print(f"경고: ID {nurse_1_id}인 간호사를 찾을 수 없습니다.")
                    if nurse_2_idx is None:
                        print(f"경고: ID {nurse_2_id}인 간호사를 찾을 수 없습니다.")
        print("간호사 페어링 선호도 초기화 완료")
    
    # ───────── 1. find_violations 수정 ─────────
    def _find_violations(self) -> List[dict]:
        violations = []
        shift_types = list(self.config.shift_types or [])

        off_idx = shift_types.index('O') if 'O' in shift_types else None
        night_idx = shift_types.index('N') if 'N' in shift_types else None
        day_idx = shift_types.index('D') if 'D' in shift_types else None
        eve_idx = shift_types.index('E') if 'E' in shift_types else None

        def _need_map_for_day(day: int) -> dict:
            if (
                hasattr(self.config, 'daily_shift_requirements_by_day')
                and isinstance(self.config.daily_shift_requirements_by_day, list)
                and day < len(self.config.daily_shift_requirements_by_day)
                and isinstance(self.config.daily_shift_requirements_by_day[day], dict)
            ):
                return self.config.daily_shift_requirements_by_day[day]
            return self.config.daily_shift_requirements

        def _assigned_shift_idx(n_idx: int, day: int) -> int | None:
            row = self.roster[n_idx, day, :]
            ones = np.where(row == 1)[0]
            if len(ones) == 0:
                return None
            return int(ones[0])

        for day in range(self.num_days):
            need_map = _need_map_for_day(day)
            for shift, required_raw in need_map.items():
                if shift not in shift_types:
                    continue
                required = int(required_raw or 0)
                if required <= 0:
                    continue
                shift_idx = shift_types.index(shift)
                actual = int(np.sum(self.roster[:, day, shift_idx]))
                if actual < required:
                    violations.append(
                        {
                            'type': 'shift_requirement',
                            'day': day,
                            'shift': shift,
                            'required': required,
                            'actual': actual,
                        }
                    )

        for n_idx, nurse in enumerate(self.nurses):
            nurse_id = getattr(nurse, 'db_id', None)
            for day in range(self.num_days):
                if not self._check_consecutive_night_limit(n_idx, day):
                    violations.append({'type': 'night_consecutive', 'nurse_idx': n_idx, 'day': day})
                if not self._check_day_after_night(n_idx, day):
                    violations.append({'type': 'night_nd', 'nurse_idx': n_idx, 'day': day})
                if getattr(self.config, 'ban_n_to_e', True) and not self._check_night_before_evening(n_idx, day):
                    violations.append({'type': 'night_ne', 'nurse_idx': n_idx, 'day': day})
                if getattr(self.config, 'ban_e_to_d', True) and not self._check_eve_before_day(n_idx, day):
                    violations.append({'type': 'eve_ed', 'nurse_idx': n_idx, 'day': day})

                if (
                    getattr(self.config, 'ban_d_to_n', True)
                    and day > 0
                    and day_idx is not None
                    and night_idx is not None
                    and self.roster[n_idx, day - 1, day_idx] == 1
                    and self.roster[n_idx, day, night_idx] == 1
                ):
                    violations.append({'type': 'day_night_dn', 'nurse_idx': n_idx, 'day': day})

                if (
                    getattr(self.config, 'ban_single_night', False)
                    and night_idx is not None
                    and self.roster[n_idx, day, night_idx] == 1
                ):
                    left_n = day > 0 and self.roster[n_idx, day - 1, night_idx] == 1
                    if day == 0:
                        left_n = bool(getattr(self, 'prev_month_n_tail_by_idx', {}).get(n_idx, 0) > 0)
                    right_n = day + 1 < self.num_days and self.roster[n_idx, day + 1, night_idx] == 1
                    if not left_n and not right_n:
                        violations.append({'type': 'not_one_night', 'nurse_idx': n_idx, 'day': day})  # nm:external

                if (
                    getattr(self.config, 'weekend_off_only_enable', True)
                    and bool(getattr(nurse, 'is_weekend_off', False))
                    and off_idx is not None
                    and (self.target_month + timedelta(days=day)).weekday() < 5
                    and self.roster[n_idx, day, off_idx] == 1
                ):
                    violations.append({'type': 'weekend_off_only', 'nurse_idx': n_idx, 'day': day})

                if not self._check_max_consecutive_work_days(n_idx, day):
                    violations.append({'type': 'consecutive_work', 'nurse_idx': n_idx, 'day': day})

                init_forbidden = getattr(self, 'initial_forbidden', None)
                if isinstance(init_forbidden, dict):
                    forbidden_codes = set(init_forbidden.get((n_idx, day), set()) or set())
                    assigned_idx = _assigned_shift_idx(n_idx, day)
                    if assigned_idx is not None:
                        assigned_code = str(shift_types[assigned_idx]).upper()
                        if assigned_code in forbidden_codes:
                            violations.append(
                                {
                                    'type': 'initial_forbidden',
                                    'nurse_idx': n_idx,
                                    'nurse_id': nurse_id,
                                    'day': day,
                                    'shift': assigned_code,
                                }
                            )

            if night_idx is not None:
                monthly_nights = int(np.sum(self.roster[n_idx, :, night_idx]))
                max_nights = int(getattr(self.config, 'max_night_shifts_per_month', 0) or 0)
                if monthly_nights > max_nights:
                    violations.append(
                        {
                            'type': 'night_month_limit',
                            'nurse_idx': n_idx,
                            'nurse_id': nurse_id,
                            'required': max_nights,
                            'actual': monthly_nights,
                        }
                    )

        if getattr(self.config, 'two_offs_after_three_nig', False):
            for n_idx in range(len(self.nurses)):
                for end_d in range(2, self.num_days - 2):
                    if not self._check_two_offs_after_three_night(n_idx, end_d):
                        violations.append({'type': 'rec_3n2o', 'nurse_idx': n_idx, 'day': end_d})
        if getattr(self.config, 'two_offs_after_two_nig', False):
            for n_idx in range(len(self.nurses)):
                for end_d in range(1, self.num_days - 2):
                    if not self._check_two_offs_after_two_night(n_idx, end_d):
                        violations.append({'type': 'rec_2n2o', 'nurse_idx': n_idx, 'day': end_d})

        # ── 4O 연속 OFF 위반 체크 (당월 내 + 월경계) ──
        if off_idx is not None:
            # 고정 OFF 셀 (vacation/공가 포함): 후처리에서 이동되지 않으므로
            # 4O 위반 감지 시에는 모든 OFF를 동일하게 취급
            # 당월 내 4연속 OFF 체크
            for n_idx in range(len(self.nurses)):
                for d in range(self.num_days - 3):
                    if all(self.roster[n_idx, d + k, off_idx] == 1 for k in range(4)):
                        violations.append({'type': 'consecutive_4off', 'nurse_idx': n_idx, 'day': d})
            # 월경계 4O 체크: 전월 꼬리 연속 OFF + 현월 초 연속 OFF 합산 4 이상
            prev_off_tail = getattr(self, 'prev_month_off_tail_by_idx', {}) or {}
            for n_idx, tail_cnt in prev_off_tail.items():
                if not isinstance(tail_cnt, int) or tail_cnt <= 0 or tail_cnt >= 4:
                    continue
                if n_idx < 0 or n_idx >= len(self.nurses):
                    continue
                current_head_offs = 0
                for d in range(min(4 - tail_cnt, self.num_days)):
                    if self.roster[n_idx, d, off_idx] == 1:
                        current_head_offs += 1
                    else:
                        break
                if tail_cnt + current_head_offs >= 4:
                    violations.append({
                        'type': 'cross_month_4off',
                        'nurse_idx': n_idx,
                        'prev_tail': tail_cnt,
                        'current_head': current_head_offs,
                    })

        return violations


    def calculate_metrics(self) -> Dict:
        """Calculate roster metrics and statistics.
        
        Returns:
            Dict containing various roster metrics
        """
        metrics = {}
        
        # Shift distribution metrics
        shift_counts = {shift: 0 for shift in self.config.shift_types}
        nurse_shift_counts = {nurse.name: {shift: 0 for shift in self.config.shift_types} 
                             for nurse in self.nurses}
        
        unassigned_slots = 0
        staffing_violations = 0
        experience_violations = 0
        
        for day in range(self.num_days):
            # Check staffing requirements
            for shift in self.config.daily_shift_requirements.keys():
                shift_idx = self.config.shift_types.index(shift)
                assigned = np.sum(self.roster[:, day, shift_idx])
                required = self.config.daily_shift_requirements[shift]
                
                if assigned < required:
                    staffing_violations += 1
                    
            # Check experience requirements
            exp_violations = 0
            for shift in self.config.daily_shift_requirements.keys():
                shift_idx = self.config.shift_types.index(shift)
                exp_nurses = sum(
                    1 for n_idx, nurse in enumerate(self.nurses)
                    if ((nurse.experience_years or 0) >= self.config.min_experience_per_shift and
                        self.roster[n_idx, day, shift_idx] == 1)
                )
                if exp_nurses < self.config.required_experienced_nurses:
                    exp_violations += 1
            experience_violations += exp_violations
            
            # Count shifts per nurse
            for n_idx, nurse in enumerate(self.nurses):
                assigned = False
                for shift in self.config.shift_types:
                    shift_idx = self.config.shift_types.index(shift)
                    if self.roster[n_idx, day, shift_idx] == 1:
                        shift_counts[shift] += 1
                        nurse_shift_counts[nurse.name][shift] += 1
                        assigned = True
                if not assigned:
                    unassigned_slots += 1
                    
        # Calculate weekend distribution
        weekend_shifts = {nurse.name: 0 for nurse in self.nurses}
        for day in range(self.num_days):
            if self._is_weekend(day):
                for n_idx, nurse in enumerate(self.nurses):
                    if np.any(self.roster[n_idx, day, :-1]):  # Exclude OFF shifts
                        weekend_shifts[nurse.name] += 1
                        
        # Calculate consecutive work days violations
        consecutive_violations = 0
        for n_idx in range(len(self.nurses)):
            for day in range(self.num_days):
                if not self._check_consecutive_work_days(n_idx, day):
                    consecutive_violations += 1
                    
        # Calculate night shift violations
        night_violations = 0
        for n_idx in range(len(self.nurses)):
            for day in range(self.num_days):
                if not self._check_night_constraints(n_idx, day):
                    night_violations += 1
                    
        # Compile metrics
        metrics['shift_distribution'] = shift_counts
        metrics['nurse_shift_counts'] = nurse_shift_counts
        metrics['unassigned_slots'] = unassigned_slots
        metrics['staffing_violations'] = staffing_violations
        metrics['experience_violations'] = experience_violations
        metrics['consecutive_violations'] = consecutive_violations
        metrics['night_violations'] = night_violations
        metrics['weekend_distribution'] = weekend_shifts
        
        return metrics

    def _is_weekend(self, day):
        """Check if given day is weekend."""
        return day % 7 >= 5

    def calculate_detailed_metrics(self) -> Dict:
        """Calculate detailed metrics for roster evaluation."""
        metrics = {
            'constraint_violations': self._count_constraint_violations(),
            'workload_distribution': self._analyze_workload_distribution(),
            'shift_patterns': self._analyze_shift_patterns(),
            'nurse_satisfaction': self._estimate_nurse_satisfaction(),
            'coverage_metrics': self._analyze_coverage(),
            'fairness_metrics': self._analyze_fairness()
        }
        return metrics
        
    def _count_constraint_violations(self) -> Dict:
        """Count different types of constraint violations."""
        violations = self._find_violations()

        counts = {}
        for v in violations:
            v_type = v['type']
            counts[v_type] = counts.get(v_type, 0) + 1
        return counts
        
    def _analyze_workload_distribution(self) -> Dict:
        """Analyze the distribution of workload among nurses."""
        workloads = {}
        for n_idx, nurse in enumerate(self.nurses):
            shifts = {
                'total': np.sum(self.roster[n_idx, :, :-1]),  # Exclude OFF
                'day': np.sum(self.roster[n_idx, :, self.config.shift_types.index('D')]),
                'evening': np.sum(self.roster[n_idx, :, self.config.shift_types.index('E')]),
                'night': np.sum(self.roster[n_idx, :, self.config.shift_types.index('N')]),
                'off': np.sum(self.roster[n_idx, :, self.config.shift_types.index('O')])
            }
            workloads[nurse.name] = shifts
            
        return {
            'per_nurse': workloads,
            'statistics': {
                'mean_shifts': np.mean([w['total'] for w in workloads.values()]),
                'std_shifts': np.std([w['total'] for w in workloads.values()]),
                'min_shifts': min(w['total'] for w in workloads.values()),
                'max_shifts': max(w['total'] for w in workloads.values())
            }
        }
        
    def _analyze_shift_patterns(self) -> Dict:
        """Analyze patterns in shift assignments."""
        patterns = {
            'consecutive_shifts': self._analyze_consecutive_shifts(),
            'weekend_distribution': self._analyze_weekend_distribution(),
            'shift_transitions': self._analyze_shift_transitions()
        }
        return patterns


    def _calculate_off_preference_satisfaction(self):
        """선호 휴무일 만족도를 계산합니다."""
        off_idx = self.config.shift_types.index('O')
        print('off_index:',off_idx)
        total_preferences = 0
        satisfied_preferences = 0
        
        for n_idx in range(len(self.nurses)):
            for day in range(self.num_days):
                # 선호도가 높은 휴무일 (4점 이상)인 경우
                if self.preference_matrix[n_idx, day, off_idx] >= 4:
                    total_preferences += 1
                    # 실제로 OFF를 받았는지 확인
                    if self.roster[n_idx, day, off_idx] == 1:
                        satisfied_preferences += 1
        
        if total_preferences == 0:
            return 100.0  # 선호 휴무일이 없으면 100% 만족
        
        return (satisfied_preferences / total_preferences) * 100.0
        
    def _calculate_shift_preference_satisfaction(self):
        """근무 유형(D, E, N) 선호도 만족도를 계산합니다."""
        total_preferences = 0
        satisfied_preferences = 0
        
        # 각 근무 유형에 대해 (OFF 제외)
        for shift in self.config.daily_shift_requirements.keys():
            s_idx = self.config.shift_types.index(shift)
            weight = self.config.shift_preference_weights.get(shift, 1.0) if hasattr(self.config, 'shift_preference_weights') else 1.0
            
            # 각 간호사와 날짜에 대해
            for n_idx in range(len(self.nurses)):
                for day in range(self.num_days):
                    # 해당 근무 유형의 가중치가 특정 값 이상이면 선호 근무로 간주
                    if self.preference_matrix[n_idx, day, s_idx] >= weight:
                        total_preferences += 1
                        # 실제로 해당 근무 유형이 배정된 경우
                        if self.roster[n_idx, day, s_idx] == 1:
                            satisfied_preferences += 1
        
        if total_preferences == 0:
            return 100.0  # 선호 근무 유형이 없으면 100% 만족
        
        return (satisfied_preferences / total_preferences) * 100.0
        
    def _calculate_pair_preference_satisfaction(self):
        """간호사 페어링 선호도 만족도를 계산합니다."""
        if not hasattr(self, 'pair_matrix'):
            return {"together": 100.0, "apart": 100.0, "overall": 100.0}
        
        # 요청자 기준 계산이 가능하면 해당 방식 사용 (방향성, '-'는 모수 제외)
        if hasattr(self, 'pair_requests') and isinstance(self.pair_requests, dict):
            together_reqs = self.pair_requests.get("together", set())
            apart_reqs = self.pair_requests.get("apart", set())
            
            total_together_prefs = 0
            satisfied_together_prefs = 0
            total_apart_prefs = 0
            satisfied_apart_prefs = 0
            
            # together 요청자 기준 집계
            for n1, n2 in together_reqs:
                for day in range(self.num_days):
                    total_together_prefs += 1
                    if self._are_nurses_working_together(n1, n2, day):
                        satisfied_together_prefs += 1
            
            # apart 요청자 기준 집계
            for n1, n2 in apart_reqs:
                for day in range(self.num_days):
                    total_apart_prefs += 1
                    if not self._are_nurses_working_together(n1, n2, day):
                        satisfied_apart_prefs += 1
            
            together_satisfaction = 100.0 if total_together_prefs == 0 else (satisfied_together_prefs / total_together_prefs) * 100.0
            apart_satisfaction = 100.0 if total_apart_prefs == 0 else (satisfied_apart_prefs / total_apart_prefs) * 100.0
            
            total_prefs = total_together_prefs + total_apart_prefs
            satisfied_prefs = satisfied_together_prefs + satisfied_apart_prefs
            overall_satisfaction = 100.0 if total_prefs == 0 else (satisfied_prefs / total_prefs) * 100.0
            
            return {
                "together": together_satisfaction,
                "apart": apart_satisfaction,
                "overall": overall_satisfaction
            }
        
        # 후방 호환: 기존 대칭 행렬 방식
        # 함께 일하기 선호도 만족도
        total_together_prefs = 0
        satisfied_together_prefs = 0
        
        # 따로 일하기 선호도 만족도
        total_apart_prefs = 0
        satisfied_apart_prefs = 0
        
        # 각 날짜에 대해
        for day in range(self.num_days):
            # 각 근무 유형에 대해
            for shift in self.config.daily_shift_requirements.keys():
                shift_idx = self.config.shift_types.index(shift)
                
                # 이 근무 유형에 배정된 간호사 찾기
                assigned_nurses = [i for i in range(len(self.nurses)) 
                                 if self.roster[i, day, shift_idx] == 1]
                
                # 함께 일하는 선호도 계산
                for i in range(len(assigned_nurses)):
                    for j in range(i+1, len(assigned_nurses)):
                        n1 = assigned_nurses[i]
                        n2 = assigned_nurses[j]
                        
                        # 함께 일하기 원하는 쌍인 경우
                        if self.pair_matrix["together"][n1, n2] > 0:
                            total_together_prefs += 1
                            satisfied_together_prefs += 1
                
                # 다른 교대에 배정된 간호사들과의 관계 확인
                for other_shift in self.config.daily_shift_requirements.keys():
                    if shift == other_shift:
                        continue
                    other_shift_idx = self.config.shift_types.index(other_shift)
                    other_assigned = [i for i in range(len(self.nurses)) 
                                     if self.roster[i, day, other_shift_idx] == 1]
                    
                    # 두 교대 간의 간호사 쌍 확인
                    for n1 in assigned_nurses:
                        for n2 in other_assigned:
                            # 따로 일하기 원하는 쌍인 경우
                            if self.pair_matrix["apart"][n1, n2] > 0:
                                total_apart_prefs += 1
                                satisfied_apart_prefs += 1
        
        # 불만족 케이스 확인
        for n1 in range(len(self.nurses)):
            for n2 in range(n1+1, len(self.nurses)):
                # 함께 일하기 원하는 쌍인 경우
                if self.pair_matrix["together"][n1, n2] > 0:
                    # 각 날짜에 대해 함께 근무했는지 확인
                    for day in range(self.num_days):
                        together_today = False
                        # 각 근무 유형에 대해
                        for shift in self.config.daily_shift_requirements.keys():
                            shift_idx = self.config.shift_types.index(shift)
                            # 둘 다 같은 교대에 배정된 경우
                            if (self.roster[n1, day, shift_idx] == 1 and 
                                self.roster[n2, day, shift_idx] == 1):
                                together_today = True
                                break
                        # 이날 함께 근무하지 않았으면 총 선호도 카운트만 추가
                        if not together_today:
                            total_together_prefs += 1
                
                # 따로 일하기 원하는 쌍인 경우
                if self.pair_matrix["apart"][n1, n2] > 0:
                    # 각 날짜에 대해 같은 근무에 배정되었는지 확인
                    for day in range(self.num_days):
                        for shift in self.config.daily_shift_requirements.keys():
                            shift_idx = self.config.shift_types.index(shift)
                            # 둘 다 같은 교대에 배정된 경우 (선호도 불만족)
                            if (self.roster[n1, day, shift_idx] == 1 and 
                                self.roster[n2, day, shift_idx] == 1):
                                total_apart_prefs += 1
        
        # 종합 만족도 계산
        together_satisfaction = 100.0 if total_together_prefs == 0 else (satisfied_together_prefs / total_together_prefs) * 100.0
        apart_satisfaction = 100.0 if total_apart_prefs == 0 else (satisfied_apart_prefs / total_apart_prefs) * 100.0
        
        # 종합 선호도 점수
        total_prefs = total_together_prefs + total_apart_prefs
        satisfied_prefs = satisfied_together_prefs + satisfied_apart_prefs
        overall_satisfaction = 100.0 if total_prefs == 0 else (satisfied_prefs / total_prefs) * 100.0
        
        return {
            "together": together_satisfaction,
            "apart": apart_satisfaction,
            "overall": overall_satisfaction
        }

    def calculate_individual_satisfaction(self) -> Dict[str, Dict]:
        """개개인의 만족도를 계산합니다."""
        individual_satisfaction = {}
        
        for n_idx, nurse in enumerate(self.nurses):
            nurse_id = nurse.db_id
            satisfaction = {
                "nurse_id": nurse_id,
                "name": nurse.name,
                "off_satisfaction": 0.0,
                "shift_satisfaction": 0.0,
                "pair_satisfaction": 0.0,
                "total_requests": 0,
                "satisfied_requests": 0,
                "overall_satisfaction": 0.0
            }
            
            # 휴무 선호도 만족도 계산
            off_idx = self.config.shift_types.index('O')
            total_off_requests = 0
            satisfied_off_requests = 0
            
            for day in range(self.num_days):
                if self.preference_matrix[n_idx, day, off_idx] >= 4:
                    total_off_requests += 1
                    if self.roster[n_idx, day, off_idx] == 1:
                        satisfied_off_requests += 1
            
            satisfaction["off_satisfaction"] = (satisfied_off_requests / total_off_requests * 100) if total_off_requests > 0 else 100.0
            satisfaction["off_request_count"] = total_off_requests
            
            # 근무 유형 선호도 만족도 계산
            total_shift_requests = 0
            satisfied_shift_requests = 0
            
            for day in range(self.num_days):
                for shift_idx, shift_type in enumerate(self.config.shift_types):
                    if shift_type != 'O' and self.preference_matrix[n_idx, day, shift_idx] >= 4:
                        total_shift_requests += 1
                        if self.roster[n_idx, day, shift_idx] == 1:
                            satisfied_shift_requests += 1
            
            satisfaction["shift_satisfaction"] = (satisfied_shift_requests / total_shift_requests * 100) if total_shift_requests > 0 else 100.0
            satisfaction["shift_request_count"] = total_shift_requests
            
            # 페어링 선호도 만족도 계산
            total_pair_requests = 0
            satisfied_pair_requests = 0
            
            if hasattr(self, 'pair_matrix') and self.pair_matrix is not None:
                # 요청자(방향성) 기준 계산만 사용: 사용자 입력 요청만 카운트
                has_directional = hasattr(self, 'pair_requests') and isinstance(self.pair_requests, dict)
                if has_directional:
                    together_reqs = self.pair_requests.get("together", set())
                    apart_reqs = self.pair_requests.get("apart", set())
                    for other_n_idx in range(len(self.nurses)):
                        if other_n_idx == n_idx:
                            continue
                        req_together = (n_idx, other_n_idx) in together_reqs
                        req_apart = (n_idx, other_n_idx) in apart_reqs
                        if not (req_together or req_apart):
                            continue
                        for day in range(self.num_days):
                            total_pair_requests += 1
                            if req_together:
                                if self._are_nurses_working_together(n_idx, other_n_idx, day):
                                    satisfied_pair_requests += 1
                            elif req_apart:
                                if not self._are_nurses_working_together(n_idx, other_n_idx, day):
                                    satisfied_pair_requests += 1
                else:
                    # 방향성 정보가 전혀 없는 경우에만 후방 호환(행렬 기반) 사용
                    together_mat = self.pair_matrix.get("together") if isinstance(self.pair_matrix, dict) else None
                    apart_mat = self.pair_matrix.get("apart") if isinstance(self.pair_matrix, dict) else None
                    def _has_pref(mat, i, j):
                        if mat is None:
                            return False
                        if isinstance(mat, np.ndarray):
                            try:
                                return mat[i, j] > 0
                            except Exception:
                                return False
                        if isinstance(mat, dict):
                            return mat.get((i, j), 0) > 0
                        return False
                    for other_n_idx in range(len(self.nurses)):
                        if other_n_idx != n_idx:
                            for day in range(self.num_days):
                                has_together = _has_pref(together_mat, n_idx, other_n_idx)
                                has_apart = _has_pref(apart_mat, n_idx, other_n_idx)
                                if has_together or has_apart:
                                    total_pair_requests += 1
                                    if has_together:
                                        if self._are_nurses_working_together(n_idx, other_n_idx, day):
                                            satisfied_pair_requests += 1
                                    elif has_apart:
                                        if not self._are_nurses_working_together(n_idx, other_n_idx, day):
                                            satisfied_pair_requests += 1
            satisfaction["pair_satisfaction"] = (satisfied_pair_requests / total_pair_requests * 100) if total_pair_requests > 0 else 100.0
            satisfaction["pair_request_count"] = total_pair_requests
            
            # 전체 요청 수와 만족한 요청 수 계산
            satisfaction["total_requests"] = total_off_requests + total_shift_requests + total_pair_requests
            satisfaction["satisfied_requests"] = satisfied_off_requests + satisfied_shift_requests + satisfied_pair_requests
            
            # 전체 만족도 계산
            if satisfaction["total_requests"] > 0:
                satisfaction["overall_satisfaction"] = (satisfaction["satisfied_requests"] / satisfaction["total_requests"]) * 100
            else:
                satisfaction["overall_satisfaction"] = 100.0
            
            individual_satisfaction[nurse_id] = satisfaction
        
        return individual_satisfaction

    def calculate_detailed_request_analysis(self) -> Dict:
        """요청별 상세 분석을 계산합니다."""
        analysis = {
            "total_requests": {
                "off": 0,
                "shift": 0,
                "pair": 0
            },
            "satisfied_requests": {
                "off": 0,
                "shift": 0,
                "pair": 0
            },
            "satisfaction_rate": {
                "off": 0.0,
                "shift": 0.0,
                "pair": 0.0,
                "overall": 0.0
            },
            "request_details": []
        }
        
        # 휴무 요청 분석
        off_idx = self.config.shift_types.index('O')
        for n_idx in range(len(self.nurses)):
            for day in range(self.num_days):
                if self.preference_matrix[n_idx, day, off_idx] >= 4:
                    analysis["total_requests"]["off"] += 1
                    if self.roster[n_idx, day, off_idx] == 1:
                        analysis["satisfied_requests"]["off"] += 1
                        analysis["request_details"].append({
                            "nurse_id": self.nurses[n_idx].db_id,
                            "nurse_name": self.nurses[n_idx].name,
                            "day": day + 1,
                            "request_type": "off",
                            "satisfied": True,
                            "preference_score": self.preference_matrix[n_idx, day, off_idx]
                        })
                    else:
                        analysis["request_details"].append({
                            "nurse_id": self.nurses[n_idx].db_id,
                            "nurse_name": self.nurses[n_idx].name,
                            "day": day + 1,
                            "request_type": "off",
                            "satisfied": False,
                            "preference_score": self.preference_matrix[n_idx, day, off_idx]
                        })
        
        # 근무 유형 요청 분석
        for n_idx in range(len(self.nurses)):
            for day in range(self.num_days):
                for shift_idx, shift_type in enumerate(self.config.shift_types):
                    if shift_type != 'O' and self.preference_matrix[n_idx, day, shift_idx] >= 4:
                        analysis["total_requests"]["shift"] += 1
                        if self.roster[n_idx, day, shift_idx] == 1:
                            analysis["satisfied_requests"]["shift"] += 1
                            analysis["request_details"].append({
                                "nurse_id": self.nurses[n_idx].db_id,
                                "nurse_name": self.nurses[n_idx].name,
                                "day": day + 1,
                                "request_type": "shift",
                                "shift_type": shift_type,
                                "satisfied": True,
                                "preference_score": self.preference_matrix[n_idx, day, shift_idx]
                            })
                        else:
                            analysis["request_details"].append({
                                "nurse_id": self.nurses[n_idx].db_id,
                                "nurse_name": self.nurses[n_idx].name,
                                "day": day + 1,
                                "request_type": "shift",
                                "shift_type": shift_type,
                                "satisfied": False,
                                "preference_score": self.preference_matrix[n_idx, day, shift_idx]
                            })
        
        # 페어링 요청 분석
        if hasattr(self, 'pair_matrix') and self.pair_matrix is not None:
            together_mat = self.pair_matrix.get("together") if isinstance(self.pair_matrix, dict) else None
            apart_mat = self.pair_matrix.get("apart") if isinstance(self.pair_matrix, dict) else None
            
            def _get_weight(mat, i, j):
                if mat is None:
                    return 0
                if isinstance(mat, np.ndarray):
                    try:
                        return mat[i, j]
                    except Exception:
                        return 0
                if isinstance(mat, dict):
                    return mat.get((i, j), 0)
                return 0
            
            for n1 in range(len(self.nurses)):
                for n2 in range(n1 + 1, len(self.nurses)):
                    for day in range(self.num_days):
                        together_pref = _get_weight(together_mat, n1, n2)
                        apart_pref = _get_weight(apart_mat, n1, n2)
                        
                        if together_pref > 0 or apart_pref > 0:
                            analysis["total_requests"]["pair"] += 1
                            request_type = "work_together" if together_pref > 0 else "work_apart"
                            satisfied = False
                            
                            if together_pref > 0:
                                satisfied = self._are_nurses_working_together(n1, n2, day)
                            else:
                                satisfied = not self._are_nurses_working_together(n1, n2, day)
                            
                            if satisfied:
                                analysis["satisfied_requests"]["pair"] += 1
                            
                            analysis["request_details"].append({
                                "nurse_1_id": self.nurses[n1].db_id,
                                "nurse_1_name": self.nurses[n1].name,
                                "nurse_2_id": self.nurses[n2].db_id if n2 < len(self.nurses) else None,
                                "nurse_2_name": self.nurses[n2].name if n2 < len(self.nurses) else None,
                                "day": day + 1,
                                "request_type": "pair",
                                "pair_type": request_type,
                                "satisfied": satisfied,
                                "preference_score": max(together_pref, apart_pref)
                            })
        
        # 만족도 계산
        for request_type in ["off", "shift", "pair"]:
            total = analysis["total_requests"][request_type]
            satisfied = analysis["satisfied_requests"][request_type]
            analysis["satisfaction_rate"][request_type] = (satisfied / total * 100) if total > 0 else 100.0
        
        total_requests = sum(analysis["total_requests"].values())
        total_satisfied = sum(analysis["satisfied_requests"].values())
        analysis["satisfaction_rate"]["overall"] = (total_satisfied / total_requests * 100) if total_requests > 0 else 100.0
        
        return analysis

    def _are_nurses_working_together(self, n1: int, n2: int, day: int) -> bool:
        """두 간호사가 같은 날 같은 근무에 배정되었는지 확인합니다."""
        for shift_idx in range(len(self.config.shift_types)):
            if (self.roster[n1, day, shift_idx] == 1 and 
                self.roster[n2, day, shift_idx] == 1):
                return True
        return False
        
#### 평가용 로직 
    def _analyze_consecutive_shifts(self) -> Dict:
        """Analyze consecutive shift patterns."""
        consecutive_counts = {
            'day': [],     # For 'D' shift
            'evening': [], # For 'E' shift
            'night': [],   # For 'N' shift
            'off': []      # For 'OFF' shift
        }
        
        # Map shift types to dictionary keys
        shift_map = {
            'D': 'day',
            'E': 'evening',
            'N': 'night',
            'O': 'off'
        }
        
        for n_idx in range(len(self.nurses)):
            for shift in self.config.shift_types:
                shift_idx = self.config.shift_types.index(shift)
                assignments = self.roster[n_idx, :, shift_idx]
                
                # Count consecutive assignments
                count = 0
                max_consecutive = 0
                for day in range(self.num_days):
                    if assignments[day]:
                        count += 1
                        max_consecutive = max(max_consecutive, count)
                    else:
                        count = 0
                
                # Use the mapping to get the correct key
                key = shift_map.get(shift, 'other')
                consecutive_counts[key].append(max_consecutive)
                
        return {
            shift: {
                'max': max(counts) if counts else 0,
                'avg': np.mean(counts) if counts else 0,
                'std': np.std(counts) if counts else 0
            }
            for shift, counts in consecutive_counts.items()
        }
        
    def _analyze_weekend_distribution(self) -> Dict:
        """Analyze the distribution of weekend shifts."""
        weekend_stats = {
            'per_nurse': {},
            'overall': {'total_weekends': 0, 'nurses_per_weekend': []}
        }
        
        try:
            for n_idx, nurse in enumerate(self.nurses):
                weekend_count = 0
                for day in range(self.num_days):
                    if self._is_weekend(day) and np.any(self.roster[n_idx, day, :-1]):
                        weekend_count += 1
                weekend_stats['per_nurse'][nurse.name] = weekend_count
                
            # Calculate nurses per weekend
            for day in range(self.num_days):
                if self._is_weekend(day):
                    weekend_stats['overall']['total_weekends'] += 1
                    nurses_working = sum(
                        1 for n_idx in range(len(self.nurses))
                        if np.any(self.roster[n_idx, day, :-1])
                    )
                    weekend_stats['overall']['nurses_per_weekend'].append(nurses_working)
        except Exception as e:
            print(f"Warning: Error calculating weekend distribution: {e}")
            # Return empty stats if there's an error
            return {
                'per_nurse': {},
                'overall': {'total_weekends': 0, 'nurses_per_weekend': []}
            }
                
        return weekend_stats
        
    def _analyze_shift_transitions(self) -> Dict:
        """Analyze transitions between different shifts."""
        transitions = {
            f"{s1}->{s2}": 0
            for s1 in self.config.shift_types
            for s2 in self.config.shift_types
        }
        
        for n_idx in range(len(self.nurses)):
            for day in range(self.num_days - 1):
                try:
                    # Find which shift is assigned for current day
                    current_shifts = np.where(self.roster[n_idx, day] == 1)[0]
                    next_shifts = np.where(self.roster[n_idx, day + 1] == 1)[0]
                    
                    if len(current_shifts) > 0 and len(next_shifts) > 0:
                        current = current_shifts[0]
                        next_day = next_shifts[0]
                        
                        transition = f"{self.config.shift_types[current]}->{self.config.shift_types[next_day]}"
                        transitions[transition] += 1
                except IndexError:
                    # Skip if there's any missing assignment
                    continue
                    
        return transitions
        
    def _estimate_nurse_satisfaction(self) -> Dict:
        """Estimate nurse satisfaction based on preferences and assignments."""
        satisfaction = {}
        
        for n_idx, nurse in enumerate(self.nurses):
            matches = 0
            total = 0
            
            for day in range(self.num_days):
                assigned_shift = np.where(self.roster[n_idx, day] == 1)[0][0]
                pref_score = self.preference_matrix[n_idx, day, assigned_shift]
                matches += pref_score
                total += 1
                
            satisfaction[nurse.name] = {
                'score': matches / total if total > 0 else 0,
                'preferred_shifts_ratio': matches / total if total > 0 else 0
            }
            
        return {
            'per_nurse': satisfaction,
            'average': np.mean([s['score'] for s in satisfaction.values()])
        }
        
    def _analyze_coverage(self) -> Dict:
        """Analyze shift coverage and staffing levels."""
        coverage = {
            'daily': {},
            'overall': {}
        }
        
        for day in range(self.num_days):
            coverage['daily'][day] = {}
            for shift in self.config.shift_types[:-1]:  # Exclude OFF
                shift_idx = self.config.shift_types.index(shift)
                required = self.config.daily_shift_requirements[shift]
                actual = np.sum(self.roster[:, day, shift_idx])
                coverage['daily'][day][shift] = {
                    'required': required,
                    'actual': actual,
                    'difference': actual - required
                }
                
        # Calculate overall statistics
        for shift in self.config.shift_types[:-1]:
            shift_idx = self.config.shift_types.index(shift)
            required_total = self.config.daily_shift_requirements[shift] * self.num_days
            actual_total = np.sum(self.roster[:, :, shift_idx])
            coverage['overall'][shift] = {
                'required_total': required_total,
                'actual_total': actual_total,
                'coverage_ratio': actual_total / required_total if required_total > 0 else 1.0
            }
            
        return coverage
        
    def _analyze_fairness(self) -> Dict:
        """Analyze fairness in shift distribution."""
        fairness = {
            'shift_distribution': {},
            'weekend_fairness': {},
            'workload_balance': {}
        }
        
        # Analyze shift type distribution
        for shift in self.config.shift_types[:-1]:
            shift_idx = self.config.shift_types.index(shift)
            assignments = [
                np.sum(self.roster[n_idx, :, shift_idx])
                for n_idx in range(len(self.nurses))
            ]
            fairness['shift_distribution'][shift] = {
                'gini_coefficient': self._calculate_gini(assignments),
                'coefficient_of_variation': np.std(assignments) / np.mean(assignments) if np.mean(assignments) > 0 else 0
            }
            
        return fairness
        
    def _calculate_gini(self, array: List[float]) -> float:
        """Calculate Gini coefficient as a measure of inequality."""
        array = np.array(array)
        if np.all(array == 0):
            return 0
        array = array.flatten()
        if np.amin(array) < 0:
            array -= np.amin(array)
        array += 0.0000001
        array = np.sort(array)
        index = np.arange(1, array.shape[0] + 1)
        n = array.shape[0]
        return ((np.sum((2 * index - n - 1) * array)) / (n * np.sum(array)))



    def apply_fixed_cells(self, fixed_cells: List[Dict]):
        """
        고정된 셀을 근무표에 적용합니다.
        
        Args:
            fixed_cells: 고정된 셀 정보 리스트
                [{'nurse_index': int, 'day_index': int, 'shift': str}, ...]
        """
        if not fixed_cells:
            return
            
        print(f"고정된 셀 {len(fixed_cells)}개 적용 중...")
        
        for fixed_cell in fixed_cells:
            nurse_idx = fixed_cell['nurse_index']
            day_idx = fixed_cell['day_index']
            shift = fixed_cell['shift']
            
            # 인덱스 범위 확인
            if (nurse_idx < 0 or nurse_idx >= len(self.nurses) or 
                day_idx < 0 or day_idx >= self.num_days):
                print(f"경고: 잘못된 인덱스 - 간호사 {nurse_idx}, 날짜 {day_idx}")
                continue
                
            # 근무 타입 인덱스 찾기
            try:
                shift_idx = self.config.shift_types.index(shift)
            except ValueError:
                print(f"경고: 잘못된 근무 타입 - {shift}")
                continue
                
            # 고정된 셀 적용
            self.roster[nurse_idx, day_idx, :] = 0  # 모든 근무 타입 초기화
            self.roster[nurse_idx, day_idx, shift_idx] = 1  # 지정된 근무 타입 설정
            
            print(f"고정 셀 적용: 간호사 {nurse_idx}, 날짜 {day_idx+1}, 근무 {shift}")
            
        print("고정된 셀 적용 완료")
