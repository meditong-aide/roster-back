from datetime import date, datetime, timedelta
import logging
import math
import time
import numpy as np
from typing import List, Dict, Optional, Tuple
from db.roster_config import NurseRosterConfig
from db.nurse_config import Nurse
from services.roster_system import RosterSystem
import numpy as np
from collections import defaultdict
from services.day_windows import iter_nurse_days, build_active_days
import random
from services.constraints.grade_constraints import add_grade_constraints
from services.objectives.team_objective import add_team_balance_objective_terms
from services.cp_sat.shift_normalizer import (
    build_shift_normalizer as build_shift_normalizer_impl,
    normalize_shift_code as normalize_shift_code_impl,
)
from services.cp_sat.m_coverage import compute_main_bucket_indices
from services.cp_sat.result_mapping import convert_result_to_db_format as _convert_result_to_db_format_impl
from services.cp_sat.work_shift_overrides import (
    apply_work_shift_overrides as _apply_work_shift_overrides_impl,
)
from services.cp_sat.postprocess_off import (
    postprocess_rebalance_off as _postprocess_rebalance_off_impl,
    postprocess_trim_extra_offs as _postprocess_trim_extra_offs_impl,
)
from services.cp_sat.hardcoded_weights import (
    EXPERIENCE_SHORT_PENALTY,
    FALLBACK_COVERAGE_SHORT_WEIGHT,
    FALLBACK_EXPERIENCE_SHORT_PENALTY,
    ISOLATED_OFF_PENALTY,
    NIGHT_DEVIATION_PENALTY,
    NOD_NOE_PENALTY,
    N_ONLY_NIGHT_BONUS,
    PREFERENCE_SCORE_SCALE,
    WEEK_OFF_SHORT_PENALTY,
)
from services.cp_sat.objective_terms import build_main_objective_terms, _n_forbid_n_set
from services.cp_sat.fallback_lex import optimize_fallback_lex_hard_first
from services.cp_sat.night_distribution_log import log_n_even_distribution
from services.cp_sat.lookahead_helpers import (
    get_D_ext,
    compute_leave_ext,
    month_total_day_range,
)
from services.cp_sat.lookahead_constraints import (
    add_lookahead_off_cap_constraints,
    add_lookahead_distribution_penalty_terms,
)
from services.cp_sat.off_policy import (
    build_off_partitions,
    compute_off_bounds,
    off_cap_semantics_label,
    resolve_effective_off_days,
)
from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes

logger = logging.getLogger(__name__)


def _allowed_shift_codes(raw) -> set[str]:
    return normalize_allowed_shift_codes(raw, use_mid=True)


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
    """(호환) shift_definitions 기반의 정규화 매핑을 생성한다."""
    return build_shift_normalizer_impl(shift_defs)


def _normalize_shift_code(raw_code: object, id_to_main: dict[str, str]) -> str | None:
    """(호환) 입력 근무코드를 메인 코드로 정규화한다."""
    return normalize_shift_code_impl(raw_code, id_to_main)


def _cp_sat_status_to_text(status: int) -> str:
    """CP-SAT 상태 코드를 사람이 읽을 수 있는 문자열로 변환한다."""
    from ortools.sat.python import cp_model

    mapping = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    return mapping.get(status, f"UNKNOWN({status})")


def _log_fixed_cells_off_trace(
    *,
    logger_prefix: str,
    nurses: list[Nurse],
    fixed_cells: list[dict],
    fixed_original_shift_map: dict[tuple[int, int], str],
    off_exception_cells: set[tuple[int, int]],
    off_exception_vacation_cells: set[tuple[int, int]],
    shift_id_to_type: dict[str, str],
    watch_db_ids: set[str],
) -> None:
    """고정 셀에서 휴무류가 어떻게 정규화/예외 처리되는지 추적 로그를 출력한다.

    이 프로젝트는 휴가/공가/주휴/주말강제OFF 등 '휴무류'를 모델 내부에서는 모두 `O`로 취급한다.
    다만 결과 변환 시에는 고정 셀의 원본 shift_id(`휴`, `생`, `법` 등)를 복원할 수 있어,
    화면에서 보이는 문자 `O` 개수와 "모델이 카운트한 OFF" 개수가 달라질 수 있다.

    Args:
        logger_prefix: 로그 접두사
        nurses: 간호사 리스트(인덱스 → db_id/name 매핑 용도)
        fixed_cells: 입력 고정 셀 리스트(정규화 이후 값이 들어있을 수 있음)
        fixed_original_shift_map: (nurse_index, day_index) → 원본 shift_id
        off_exception_cells: 휴무류 예외 셀(OFF 상한/특수 처리 제외용)
        off_exception_vacation_cells: 휴가/공가 셀(별도 추적용)
        shift_id_to_type: shift_id(대문자) → type(예: '휴가', '공가', '근무' 등)
        watch_db_ids: 추적 대상 nurse.db_id 집합
    """
    try:
        idx_to_dbid = {i: str(n.db_id) for i, n in enumerate(nurses)}
        idx_to_name = {i: str(getattr(n, "name", "?")) for i, n in enumerate(nurses)}
        rows: dict[str, list[dict]] = {}

        for c in fixed_cells or []:
            n_idx = c.get("nurse_index")
            d_idx = c.get("day_index")
            if n_idx is None or d_idx is None:
                continue
            db_id = idx_to_dbid.get(int(n_idx))
            if not db_id or db_id not in watch_db_ids:
                continue
            original = fixed_original_shift_map.get((int(n_idx), int(d_idx)))
            normalized = str(c.get("shift") or "").strip()
            shift_type = str(c.get("shift_type") or "").strip()
            if not shift_type:
                probe = original or normalized
                shift_type = shift_id_to_type.get(str(probe).strip().upper(), "")
            rows.setdefault(db_id, []).append(
                {
                    "day": int(d_idx) + 1,
                    "name": idx_to_name.get(int(n_idx), "?"),
                    "original_shift": original,
                    "normalized_shift": normalized,
                    "shift_type": shift_type,
                    "off_exception": (int(n_idx), int(d_idx)) in off_exception_cells,
                    "vac_exception": (int(n_idx), int(d_idx)) in off_exception_vacation_cells,
                }
            )

        for db_id, items in rows.items():
            items_sorted = sorted(items, key=lambda x: x["day"])
            print(f"{logger_prefix} [WatchFixedCells] nurse_id={db_id} rows={items_sorted}")
    except Exception as exc:
        print(f"{logger_prefix} [WatchFixedCells] 로그 실패: {exc}")


def _log_off_count_trace(
    *,
    logger_prefix: str,
    nurse: Nurse,
    schedule: list[str],
    shift_id_to_main: dict[str, str],
    shift_id_to_type: dict[str, str],
    weekly_off_days_0based: list[int] | None = None,
) -> None:
    """최종 결과에서 '보이는 O'와 '모델 기준 OFF' 차이를 추적 출력한다.

    - 보이는 O: 결과 배열에서 값이 'O'/'OFF'인 셀 개수
    - 모델 기준 OFF: 해당 shift_id를 정규화했을 때 메인 코드가 'O'로 판정되는 셀 개수
      (예: '휴'가 휴가로 정의되어 있으면 정규화 결과는 'O'가 되어 OFF로 카운트됨)

    Args:
        logger_prefix: 로그 접두사
        nurse: 대상 간호사
        schedule: DB 저장용 shift_id 리스트(최종 결과)
        shift_id_to_main: shift_id → 메인 코드 매핑(build_shift_normalizer 결과)
        shift_id_to_type: shift_id → type 매핑(예: '휴가', '공가')
        weekly_off_days_0based: 주휴(0-based day index) 리스트
    """
    weekly_set = set(int(x) for x in (weekly_off_days_0based or []))
    literal_o_cnt = 0
    off_like_cnt = 0
    vacation_cnt = 0
    off_like_but_not_o: list[dict] = []

    for day1, shift_id in enumerate(schedule, start=1):
        sid = str(shift_id or "").strip()
        sid_upper = sid.upper()
        is_literal_o = sid_upper in {"O", "OFF"}
        main = _normalize_shift_code(sid, shift_id_to_main)
        is_off_like = main == "O"
        stype = shift_id_to_type.get(sid_upper, "")
        is_vac = stype in {"휴가", "공가"}

        if is_literal_o:
            literal_o_cnt += 1
        if is_off_like:
            off_like_cnt += 1
        if is_vac:
            vacation_cnt += 1
        if is_off_like and not is_literal_o:
            off_like_but_not_o.append(
                {
                    "day": day1,
                    "shift": sid,
                    "type": stype,
                    "weekly_off": (day1 - 1) in weekly_set,
                }
            )

    print(
        f"{logger_prefix} [WatchOffCount] nurse_id={getattr(nurse, 'db_id', '?')}, "
        f"name={getattr(nurse, 'name', '?')}, "
        f"visible_O={literal_o_cnt}, off_like_total={off_like_cnt}, vacation={vacation_cnt}, "
        f"weekly_off={len(weekly_set)}"
    )
    if off_like_but_not_o:
        print(f"{logger_prefix} [WatchOffCount][OffLikeButNotO] {off_like_but_not_o}")


def _log_weekend_off_enforcement_once(
    rs: RosterSystem,
    join: list[int],
    leave: list[int],
    weekend_days: set[int],
    d_phys: int,
    fixed: dict[tuple[int, int], int],
    off_idx: int | None,
    logger_prefix: str,
) -> None:
    """주말 OFF 강제 제약 적용 내역을 1회만 출력한다."""
    if getattr(rs, "_weekend_off_enforcement_logged", False):
        return
    setattr(rs, "_weekend_off_enforcement_logged", True)
    if not getattr(rs.config, "weekend_off_only_enable", True):
        return
    if off_idx is None:
        return
    for n, nu in enumerate(rs.nurses):
        if not bool(getattr(nu, "is_weekend_off", False)):
            continue
        t0, t1 = join[n], leave[n]
        weekend_in_range_all = [d for d in sorted(weekend_days) if t0 <= d <= t1]
        weekend_in_range = [d for d in weekend_in_range_all if d < d_phys]
        weekend_days_1based = [d + 1 for d in weekend_in_range]
        lookahead_weekend_days_1based = [d + 1 for d in weekend_in_range_all if d >= d_phys]
        forced_days = []
        skipped_fixed_days = []
        for d in weekend_in_range:
            if (n, d) in fixed and fixed[(n, d)] != off_idx:
                skipped_fixed_days.append(d + 1)
            else:
                forced_days.append(d + 1)
        nurse_id = getattr(nu, "nurse_id", "?")
        nurse_name = getattr(nu, "name", "?")
        print(
            f"{logger_prefix} [WeekendOff][Enforce] nurse_idx={n}, "
            f"nurse_id={nurse_id}, name={nurse_name}, "
            f"weekend_days={weekend_days_1based}, "
            f"lookahead_weekend_days={lookahead_weekend_days_1based}, "
            f"forced_off_days={forced_days}, "
            f"skipped_fixed_days={skipped_fixed_days}"
        )


def _log_weekend_work_summary(rs: RosterSystem, logger_prefix: str) -> None:
    """간호사별 주말 근무 배정 여부를 요약 출력한다."""
    shift_types = rs.config.shift_types
    if "O" not in shift_types:
        off_code = None
    else:
        off_code = shift_types[shift_types.index("O")]
    first_day = rs.target_month
    weekend_days = {
        d for d in range(rs.num_days) if (first_day + timedelta(days=d)).weekday() >= 5
    }
    for n, nu in enumerate(rs.nurses):
        weekend_work = []
        for d in sorted(weekend_days):
            assigned_code = None
            for s_idx, code in enumerate(shift_types):
                if int(rs.roster[n, d, s_idx]) == 1:
                    assigned_code = code
                    break
            if assigned_code is None:
                continue
            if off_code is not None and assigned_code == off_code:
                continue
            weekend_work.append(f"{d + 1}:{assigned_code}")
        nurse_id = getattr(nu, "nurse_id", "?")
        nurse_name = getattr(nu, "name", "?")
        is_weekend_off = bool(getattr(nu, "is_weekend_off", False))
        print(
            f"{logger_prefix} [WeekendOff][Work] nurse_idx={n}, "
            f"nurse_id={nurse_id}, name={nurse_name}, "
            f"is_weekend_off={int(is_weekend_off)}, weekend_work={weekend_work}"
        )



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
        not_one_night = config_data.get('not_one_night', False)
        ban_night_before_fixed_off = bool(config_data.get('ban_night_before_fixed_off', True))
        max_nig_per_month = config_data.get('max_nig_per_month', 15)
        if max_nig_per_month != 15:
            max_nig_per_month = 17
        # 디버그: N 상한 보정 전/후 확인
        print(f"{self.logger_prefix} max_nig_per_month(raw)={max_nig_per_month}")
        
        # 병원 내규 (Soft Constraints)
        min_exp_per_shift = config_data.get('min_exp_per_shift', 3)
        req_exp_nurses = config_data.get('req_exp_nurses', 1)
        two_offs_per_week = config_data.get('two_offs_per_week', True)
        sequential_offs = config_data.get('sequential_offs', True)
        even_nights = config_data.get('even_nights', True)
        enforce_clustered_offs = bool(config_data.get("enforce_clustered_offs", False))
        isolated_off_slack_penalty = int(config_data.get("isolated_off_slack_penalty", 300000) or 0)
        # if int(config_data.get('off_placement_mode', 0) or 0) != 0:
        #     print(f"{self.logger_prefix} [OffPlacementMode] deprecated: forcing off_placement_mode=0")
        # off_placement_mode = 0
        
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
                'D','E','N'(필요 시 'M','W') 키만을 갖는 정수 요구치 맵
            """
            base_keys = {"D", "E", "N"}
            if bool(config_data.get("use_mid", False)):
                base_keys.add("M")
            if isinstance(req_map, dict):
                for k in req_map.keys():
                    key = str(k).strip().upper()
                    if key == "W":
                        base_keys.add("W")
                    elif key == "M":
                        base_keys.add("M")
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
            not_one_night=not_one_night,
            ban_night_before_fixed_off=ban_night_before_fixed_off,
            max_consecutive_work_days=max_conseq_work,
            # 추가된 새로운 제약사항들
            banned_day_after_eve=banned_day_after_eve,
            two_offs_after_three_nig=two_offs_after_three_nig,
            two_offs_after_two_nig=two_offs_after_two_nig,
            sequential_offs=sequential_offs,
            even_nights=even_nights,
            nod_noe=config_data.get('nod_noe', True),
            enforce_clustered_offs=enforce_clustered_offs,
            isolated_off_slack_penalty=isolated_off_slack_penalty,
            global_monthly_off_days=0,
            standard_personal_off_days=config_data.get('off_days', 8),
            # standard_personal_off_days=,
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
            preceptee_on=bool(config_data.get('preceptee_on', False)),
            preceptee_shift_count=bool(config_data.get('preceptee_shift_count', True)),
            use_mid=bool(config_data.get('use_mid', False)),
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
            # use_max_coverage 폐기 → min/max 범위 모델로 전환 (daily_shift_requirements_max_by_day)
            # off_placement_mode=0,
        )
        off_days_eff, off_days_src = resolve_effective_off_days(config_data)
        print(
            f"[OffPolicy][Config] effective_off_days={off_days_eff}, source={off_days_src}, "
            f"raw_off_days={config_data.get('off_days')}, "
            f"raw_standard={config_data.get('standard_personal_off_days')}, "
            f"raw_global={config_data.get('global_monthly_off_days')}"
        )
        # 월말 자연스러운 종료를 유도하기 위한 그림자 커버리지(소프트) 기본값
        setattr(cfg, "shadow_coverage_lookback_days", int(config_data.get("shadow_coverage_lookback_days", 6) or 0))
        setattr(cfg, "shadow_coverage_need_ratio", float(config_data.get("shadow_coverage_need_ratio", 0.6) or 0.0))
        setattr(cfg, "shadow_coverage_penalty_weight", int(config_data.get("shadow_coverage_penalty_weight", 6) or 0))
        print("=== DEBUG: 최종 cfg 값 확인 ===")
        print("oversupply_equalize_weight:", cfg.oversupply_equalize_weight)
        print("extra_off_penalty_weight:", cfg.extra_off_penalty_weight)
        # 전이 금지 하드 제약 설정 (기본: 모두 금지)
        ban_e_to_d = bool(config_data.get("ban_e_to_d", True))
        ban_n_to_e = bool(config_data.get("ban_n_to_e", True))
        ban_d_to_n = bool(config_data.get("ban_d_to_n", True))
        ban_n_to_d = bool(config_data.get("ban_n_to_d", True))
        if not banned_day_after_eve:
            ban_e_to_d = False
            ban_n_to_e = False
            ban_d_to_n = False
            ban_n_to_d = False
        setattr(cfg, "ban_e_to_d", ban_e_to_d)
        setattr(cfg, "ban_n_to_e", ban_n_to_e)
        setattr(cfg, "ban_d_to_n", ban_d_to_n)
        setattr(cfg, "ban_n_to_d", ban_n_to_d)
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
                    for kk in daily_req.keys():
                        if kk not in m:
                            m[kk] = daily_req.get(kk, 0)
                    norm_list.append(m)
                setattr(cfg, "daily_shift_requirements_by_day", norm_list)
        except Exception:
            pass
        try:
            ds_max_by_day = config_data.get('daily_shift_requirements_max_by_day')
            if isinstance(ds_max_by_day, list) and len(ds_max_by_day) > 0:
                norm_max_list = []
                for day_map in ds_max_by_day:
                    if not isinstance(day_map, dict):
                        norm_max_list.append({k: 0 for k in daily_req})
                        continue
                    m = {}
                    for k, v in day_map.items():
                        key = str(k).strip().upper()
                        if key in daily_req:
                            m[key] = int(v or 0)
                    for kk in daily_req.keys():
                        if kk not in m:
                            m[kk] = 0
                    norm_max_list.append(m)
                setattr(cfg, "daily_shift_requirements_max_by_day", norm_max_list)
        except Exception:
            pass
        setattr(cfg, "lookahead_days", int(config_data.get("lookahead_days") or 0))
        setattr(
            cfg,
            "next_month_head_requirements",
            config_data.get("next_month_head_requirements") or [],
        )
        setattr(cfg, "shift_definitions", config_data.get("shift_definitions") or [])
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
        """DB에서 가져온 간호사 데이터를 Nurse 객체 리스트로 변환한다.

        Notes:
            - active=0(비활성) 인력은 엔진 대상에서 제외한다.
        """
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
        sorted_rows = sorted(
            nurses_data,
            key=lambda r: (
                r.get('sequence', 0),
                -int(r.get('experience', 0) or 0),
                str(r.get('nurse_id')),
            ),
        )
        nurses: list[Nurse] = []
        for nurse_data in sorted_rows:
            active_raw = nurse_data.get("active", 1)
            try:
                active_flag = int(active_raw)
            except Exception:
                active_flag = 1 if active_raw else 0
            if active_flag == 0:
                print(
                    f"{self.logger_prefix} 비활성 간호사 제외: "
                    f"{nurse_data.get('name', '?')}({nurse_data.get('nurse_id', '?')}) active={active_raw}"
                )
                continue
            nurse_idx = len(nurses)
            # DB 모델을 Nurse 객체로 변환
            nurse_dict = {
                'id': nurse_idx,  # 엔진에서 사용할 인덱스 ID
                'db_id': nurse_data['nurse_id'],  # DB ID
                'name': nurse_data['name'],
                'experience_years': nurse_data.get('experience') or 0,
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
                'preceptor_id': nurse_data.get('preceptor_id'),
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

        # # 디버깅 대상 간호사 ID
        # watch_ids = {
        #     "442921",
        #     "442931",
        #     "442934",
        #     "442926",
        #     "442919",
        #     "442924",
        #     "442918",
        #     "442920",
        # }

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
        # ── 디버그: 특정 간호사 선호 요약 ──
        def _count_dates(obj: dict | list | None) -> int:
            if obj is None:
                return 0
            if isinstance(obj, dict):
                total = 0
                for v in obj.values():
                    if isinstance(v, dict):
                        total += len(v)
                    elif isinstance(v, list):
                        total += len(v)
                    else:
                        total += 1
                return total
            if isinstance(obj, list):
                return len(obj)
            return 0

        debug_rows = []
        for wid in {pref['nurse_id'] for pref in prefs_data}:
            shift_pref = shift_preferences.get(wid)
            off_req = off_requests.get(wid)
            together = [p for p in pair_preferences["work_together"] if p.get("nurse_1") == wid or p.get("nurse_2") == wid]
            apart = [p for p in pair_preferences["work_apart"] if p.get("nurse_1") == wid or p.get("nurse_2") == wid]
            debug_rows.append(
                {
                    "nurse_id": wid,
                    "shift_pref_types": list(shift_pref.keys()) if shift_pref else [],
                    "shift_pref_cnt": _count_dates(shift_pref),
                    "off_cnt": _count_dates(off_req),
                    "pair_together": len(together),
                    "pair_apart": len(apart),
                }
            )
        try:
            print(f"[PrefDebug] watch_ids summary: {debug_rows}")
        except Exception:
            pass

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
            try:
                setattr(config, "off_exception_cells", config_data.get("off_exception_cells", []) if isinstance(config_data, dict) else [])
            except Exception:
                setattr(config, "off_exception_cells", [])
        shift_defs = config_data.get("shift_definitions") if isinstance(config_data, dict) else None
        shift_id_to_main, main_to_shift_id = _build_shift_normalizer(shift_defs)
        canonical_to_shift_id = main_to_shift_id or {}
        shift_id_to_type: dict[str, str] = {}
        for row in shift_defs or []:
            try:
                sid = str(row.get("shift_id") or "").strip().upper()
                stype = str(row.get("type") or "").strip()
            except Exception:
                continue
            if sid and stype:
                shift_id_to_type[sid] = stype
        # type='근무' + shift_gb가 DEN 계열인 하위코드 집합 (프리셉티 동기화에서 근무로 취급)
        _WORK_GB = {"D", "E", "N", "데이", "이브닝", "나이트"}
        if bool(config_data.get("use_mid", False)):
            _WORK_GB |= {"M", "미드"}
        _work_sub_ids: set[str] = set()
        for row in shift_defs or []:
            try:
                _sid = str(row.get("shift_id") or "").strip().upper()
                _sgb = str(row.get("shift_gb") or "").strip()
                _stype = str(row.get("type") or "").strip()
            except Exception:
                continue
            if _stype == "근무" and (_sgb in _WORK_GB or _sgb.upper() in _WORK_GB):
                _work_sub_ids.add(_sid)
        if _work_sub_ids:
            print(f"{self.logger_prefix} [ShiftDef] 근무 하위코드: {_work_sub_ids}")
        fixed_original_shift_map: dict[tuple[int, int], str] = {}
        watch_db_ids: set[str] = {"442934"}  # 김지우 기본 추적(필요 시 config_data로 확장)
        try:
            extra_watch = (
                config_data.get("debug_watch_nurse_ids", [])
                if isinstance(config_data, dict)
                else []
            )
            if isinstance(extra_watch, list):
                watch_db_ids |= {str(x) for x in extra_watch if str(x).strip()}
        except Exception:
            pass
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
            # blocked_by_nurse: nurse_assignment 기반 불연속 근무일 처리
            # config_data에서 nurse_id(문자열) 키로 받은 것을 솔버 내부 인덱스로 변환
            _blocked_by_id = config_data.get("blocked_by_nurse_id") if isinstance(config_data, dict) else None
            if _blocked_by_id:
                _id_to_idx = {nu.db_id: i for i, nu in enumerate(nurses)}
                _blocked_by_idx: dict[int, set[int]] = {}
                for nid, days in _blocked_by_id.items():
                    idx = _id_to_idx.get(str(nid))
                    if idx is not None:
                        _blocked_by_idx[idx] = days
                        print(f"[Assignment][Solver] nurse_id={nid}, solver_idx={idx}, blocked={sorted(days)}")
                if _blocked_by_idx:
                    setattr(roster_system, "blocked_by_nurse", _blocked_by_idx)
            # coverage_exclude: 파견 기간 커버리지 제외 (nurse_id → solver_idx 변환)
            _cov_excl_id = config_data.get("coverage_exclude_nurse_days") if isinstance(config_data, dict) else None
            if _cov_excl_id:
                _id_to_idx = getattr(roster_system, '_id_to_idx', None) or {nu.db_id: i for i, nu in enumerate(nurses)}
                _cov_excl_cells: set[tuple[int, int]] = set()
                for nid, days in _cov_excl_id.items():
                    idx = _id_to_idx.get(str(nid))
                    if idx is not None:
                        for d in days:
                            _cov_excl_cells.add((idx, d))
                if _cov_excl_cells:
                    setattr(roster_system, "coverage_exclude_cells", _cov_excl_cells)
                    print(f"[Assignment][Solver] coverage_exclude_cells: {len(_cov_excl_cells)}건")
            # preceptee_period: 프리셉티 기간 (nurse_id → solver_idx 변환)
            _pperiod_id = config_data.get("preceptee_period_by_nurse_id") if isinstance(config_data, dict) else None
            if _pperiod_id:
                _id_to_idx = getattr(roster_system, '_id_to_idx', None) or {nu.db_id: i for i, nu in enumerate(nurses)}
                _pperiod_idx: dict[int, set[int]] = {}
                for nid, days in _pperiod_id.items():
                    idx = _id_to_idx.get(str(nid))
                    if idx is not None:
                        _pperiod_idx[idx] = days
                        print(f"[Assignment][Solver] preceptee_period: nurse_id={nid}, solver_idx={idx}, days={sorted(days)}")
                if _pperiod_idx:
                    setattr(roster_system, "preceptee_follow_days", _pperiod_idx)
            setattr(roster_system, "shift_id_to_main", dict(shift_id_to_main or {}))
            # cross-group OFF cap 조정용
            _other_group_offs = config_data.get("other_group_offs") if isinstance(config_data, dict) else None
            if _other_group_offs:
                setattr(roster_system, "other_group_offs", _other_group_offs)
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
            # 고정 O/주/휴가/공가를 예외 셀로 확장하되, 휴가/공가는 별도 표기로 보관
            try:
                off_ex = set(getattr(config, "off_exception_cells", []) or [])
                off_ex_vac = set(getattr(config, "off_exception_vacation_cells", []) or [])
                for c in fixed_cells:
                    try:
                        n_idx = c.get("nurse_index")
                        d_idx = c.get("day_index")
                        raw_shift = str(c.get("shift") or "").strip().upper()
                        raw_type = str(c.get("shift_type") or "").strip()
                        # print(f"{self.logger_prefix} 고정 셀: 간호사 {n_idx}, 날짜 {d_idx+1}, 근무 {raw_shift}, 타입 {raw_type}")
                    except Exception as e:
                        print('error!', e)
                        continue
                    if n_idx is None or d_idx is None:
                        continue
                    if not raw_type:
                        inferred = shift_id_to_type.get(raw_shift)
                        if inferred:
                            raw_type = inferred
                    if raw_shift in {"O", "OFF", "주"} or raw_type in {"휴가", "공가"}:
                        off_ex.add((n_idx, d_idx))
                    if raw_type in {"휴가", "공가"}:
                        off_ex_vac.add((n_idx, d_idx))
                setattr(config, "off_exception_cells", sorted(list(off_ex)))
                setattr(config, "off_exception_vacation_cells", sorted(list(off_ex_vac)))
                # print('off_ex!!!!!!', off_ex)
                # print('off_ex_vac!!!!!!', off_ex_vac)
            except Exception as e:
                print('[line 634] error!!!!!!!', e)
                pass
            # 주휴 등 휴무류 코드는 엔진에서 'O'로만 취급한다. (shift_types=['D','E','N','O'])
            for c in fixed_cells:
                try:
                    original_shift = str(c.get('shift') or '').strip()
                except Exception as e:
                    print('[line 641] error!!!!!!!', e)
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
                    except Exception as e:
                        print('[line 655] error!!!!!!!', e)
                        pass
                c['shift'] = normalized_shift
            _preceptee_fixed_wanted_map: dict[tuple[int, int], str] = {}
            _preceptee_fixed_wanted_map_raw = config_data.get('preceptee_fixed_wanted_map') or {}
            if isinstance(_preceptee_fixed_wanted_map_raw, dict):
                for _key, _raw_code in _preceptee_fixed_wanted_map_raw.items():
                    try:
                        _n_idx, _d_idx = _key
                        _n_i = int(_n_idx)
                        _d_i = int(_d_idx)
                    except Exception:
                        continue
                    _norm = _normalize_shift_code(str(_raw_code or '').strip(), shift_id_to_main)
                    if _norm and _norm in config.shift_types:
                        _preceptee_fixed_wanted_map[(_n_i, _d_i)] = _norm
            else:
                _preceptee_fixed_wanted_map_raw = {}
            roster_system._preceptee_fixed_wanted_map = _preceptee_fixed_wanted_map
            # 디버그: 고정 셀(휴가/공가/주휴/기타 휴무류) 정규화/예외 처리 확인
            try:
                off_ex = set(getattr(config, "off_exception_cells", []) or [])
                off_ex_vac = set(getattr(config, "off_exception_vacation_cells", []) or [])
                if watch_db_ids:
                    _log_fixed_cells_off_trace(
                        logger_prefix=self.logger_prefix,
                        nurses=nurses,
                        fixed_cells=fixed_cells,
                        fixed_original_shift_map=fixed_original_shift_map,
                        off_exception_cells=off_ex,
                        off_exception_vacation_cells=off_ex_vac,
                        shift_id_to_type=shift_id_to_type,
                        watch_db_ids=watch_db_ids,
                    )
            except Exception:
                pass
            # # 디버그: 특정 간호사 고정 셀/휴가 인식 확인
            # try:
            #     watch_ids = {"442918", "442924"}  # 박지은, 임윤아
            #     watch_days = {15, 16, 17, 18}  # 16~19일(0-based)
            #     id_to_idx = {n.db_id: n.id for n in nurses}
            #     idx_to_id = {n.id: n.db_id for n in nurses}
            #     watch_indices = {id_to_idx.get(nid) for nid in watch_ids if id_to_idx.get(nid) is not None}
            #     if watch_indices:
            #         off_ex = set(getattr(config, "off_exception_cells", []) or [])
            #         off_ex_vac = set(getattr(config, "off_exception_vacation_cells", []) or [])
            #         watch_rows = []
            #         for c in fixed_cells:
            #             n_idx = c.get("nurse_index")
            #             d_idx = c.get("day_index")
            #             if n_idx not in watch_indices:
            #                 continue
            #             if d_idx not in watch_days:
            #                 continue
            #             watch_rows.append(
            #                 {
            #                     "nurse_id": idx_to_id.get(n_idx),
            #                     "day": d_idx + 1,
            #                     "shift": c.get("shift"),
            #                     "original_shift": fixed_original_shift_map.get((n_idx, d_idx)),
            #                     "shift_type": c.get("shift_type"),
            #                     "off_exception": (n_idx, d_idx) in off_ex,
            #                     "off_exception_vac": (n_idx, d_idx) in off_ex_vac,
            #                 }
            #             )
            #         if watch_rows:
            #             print(f"{self.logger_prefix} [WatchFixedCells] {watch_rows}")
            # except Exception:
            #     pass
            # ── 경계 제약(강제 OFF/금지) 병합 ──
            initial_constraints = config_data.get('initial_constraints') or {}
            allow_override_by_law = bool(config_data.get('allow_override_by_law', False))
            # ID 매핑을 문자열 기반으로 통일(입력 dict 키가 str이므로 불일치 방지)
            rs_dbid_to_idx = {str(n.db_id): n.id for n in nurses}
            # 보조: int 키로도 접근 가능하도록 추가
            rs_dbid_to_idx.update({n.db_id: n.id for n in nurses})
            def _get_nurse_idx(dbid):
                if dbid in rs_dbid_to_idx:
                    return rs_dbid_to_idx[dbid]
                try:
                    return rs_dbid_to_idx.get(str(dbid))
                except Exception:
                    return None
            weekly_off_map_raw = config_data.get("weekly_off_map") or {}
            # weekly_off_settings의 activate가 0이면 weekly_off_map이 비어있음
            # 이 경우 off_placement_mode도 효력이 없도록 설정
            weekly_off_settings_activate = bool(config_data.get("weekly_off_settings_activate", False))
            if not weekly_off_settings_activate or not weekly_off_map_raw:
                # activate가 0이거나 weekly_off_map이 비어있으면 주휴 관련 옵션 비활성화
                # config.off_placement_mode = 0
                weekly_off_by_idx: dict[int, list[int]] = {}
                print('weekly_off_by_idx 없는걸로', weekly_off_by_idx)
            else:
                # 각 nurse 별 주휴 담기
                # if int(config_data.get("off_placement_mode", 0) or 0) != 0:
                #     print("[OffPlacementMode] deprecated: forcing off_placement_mode=0")
                # config.off_placement_mode = 0
                weekly_off_by_idx: dict[int, list[int]] = {}
                for dbid, day_list in (weekly_off_map_raw or {}).items():
                    n_idx = rs_dbid_to_idx.get(str(dbid))
                    if n_idx is None:
                        continue
                    if bool(getattr(nurses[n_idx], "is_weekend_off", False)):
                        continue
                    try:
                        weekly_off_by_idx[n_idx] = sorted({int(d) for d in day_list})
                    except Exception as e:
                        print('[line 715] error!!!!!!!', e)
                        continue
            roster_system.weekly_off_by_idx = weekly_off_by_idx
            # 룩어헤드 구간 주휴 고정 셀(다음 달 요일 기준 별도 계산 결과)
            raw_lookahead = config_data.get("lookahead_weekly_off_cells")
            if isinstance(raw_lookahead, (set, list)):
                roster_system.lookahead_weekly_off_cells = set(
                    (int(n), int(d)) for (n, d) in raw_lookahead
                )
            else:
                roster_system.lookahead_weekly_off_cells = set()
            # 전월 달에 따른 휴무 담기 
            prev_last_off_raw = config_data.get("prev_month_last_is_off") or {}
            prev_last_off_by_idx: dict[int, bool] = {}
            for dbid, flag in (prev_last_off_raw or {}).items():
                n_idx = _get_nurse_idx(dbid)
                if n_idx is None:
                    continue
                prev_last_off_by_idx[n_idx] = bool(flag)
            roster_system.prev_month_last_is_off = prev_last_off_by_idx
            prev_n_tail_raw = config_data.get("prev_month_n_tail") or {}
            prev_n_tail_by_idx: dict[int, int] = {}
            for dbid, tail_cnt in (prev_n_tail_raw or {}).items():
                n_idx = _get_nurse_idx(dbid)
                if n_idx is None:
                    continue
                try:
                    tcnt = int(tail_cnt or 0)
                except Exception:
                    tcnt = 0
                if tcnt > 0:
                    prev_n_tail_by_idx[n_idx] = tcnt
            roster_system.prev_month_n_tail_by_idx = prev_n_tail_by_idx
            # N tail 뒤 이미 소비된 OFF 수 (2N→2OFF 크로스먼스 중복 방지용)
            prev_n_offs_after_raw = config_data.get("prev_month_n_offs_after") or {}
            prev_n_offs_after_by_idx: dict[int, int] = {}
            for dbid, oa_cnt in (prev_n_offs_after_raw or {}).items():
                n_idx = _get_nurse_idx(dbid)
                if n_idx is None:
                    continue
                try:
                    oacnt = int(oa_cnt or 0)
                except Exception:
                    oacnt = 0
                if oacnt > 0:
                    prev_n_offs_after_by_idx[n_idx] = oacnt
            roster_system.prev_month_n_offs_after_by_idx = prev_n_offs_after_by_idx
            # 전월 꼬리 연속 OFF 카운트 (4O 월경계 제약용)
            prev_off_tail_raw = config_data.get("prev_month_off_tail") or {}
            prev_off_tail_by_idx: dict[int, int] = {}
            for dbid, off_cnt in (prev_off_tail_raw or {}).items():
                n_idx = _get_nurse_idx(dbid)
                if n_idx is None:
                    continue
                try:
                    ocnt = int(off_cnt or 0)
                except Exception:
                    ocnt = 0
                if ocnt > 0:
                    prev_off_tail_by_idx[n_idx] = ocnt
            roster_system.prev_month_off_tail_by_idx = prev_off_tail_by_idx
            # 꼬리 연속근무 보정용 OFF 윈도우 (월초 0..K-w 구간 OFF≥1)
            off_window_raw = config_data.get("off_window_constraints") or {}
            off_window_by_idx: dict[int, list[tuple[int, int]]] = {}
            for dbid, win_list in (off_window_raw or {}).items():
                n_idx = _get_nurse_idx(dbid)
                if n_idx is None:
                    continue
                normalized: list[tuple[int, int]] = []
                for win in (win_list or []):
                    if not isinstance(win, (list, tuple)) or len(win) < 2:
                        continue
                    try:
                        start = int(win[0]); end = int(win[1])
                    except Exception:
                        continue
                    if start < 0 or end < start:
                        continue
                    normalized.append((start, end))
                if normalized:
                    off_window_by_idx[n_idx] = normalized
            roster_system.off_window_constraints = off_window_by_idx
            # forced_off: { nurse_db_id: [day_idx,...] }
            forced_off = initial_constraints.get('forced_off') or {}
            # print('forced_off!!!!!', forced_off)
            if forced_off:
                for dbid, day_list in forced_off.items():
                    n_idx = rs_dbid_to_idx.get(dbid)
                    if n_idx is None:
                        continue
                    for d in day_list:
                        # 유저 고정 셀 우선: 유저가 비-O 고정을 넣은 날은 전월 꼬리 forced_off 무시
                        conflict = next((c for c in fixed_cells if c.get('nurse_index')==n_idx and c.get('day_index')==d and c.get('shift')!='O'), None)
                        if conflict:
                            print(f"{self.logger_prefix} 법규-유저 고정 충돌 (유저 우선): nurse={dbid}, day={d+1}, user={conflict.get('shift')}, law=O → forced_off 무시")
                            continue
                        fixed_cells.append({'nurse_index': n_idx, 'day_index': d, 'shift': 'O'})
            # print('fixed_cells!!!!!', fixed_cells)
            roster_system._work_sub_ids = _work_sub_ids  # fallback 등에서 참조
            roster_system._fixed_original_shift_map = fixed_original_shift_map  # fallback 동기화에서 참조
            if fixed_cells:
                print(f"{self.logger_prefix} 고정된 셀 {len(fixed_cells)}개 처리 중...")
                roster_system.fixed_cells = fixed_cells
                # for fixed_cell in fixed_cells:
                #     print(f"{self.logger_prefix} 고정 셀: 간호사 {fixed_cell['nurse_index']}, 날짜 {fixed_cell['day_index']+1}, 근무 {fixed_cell['shift']}")
            # forbidden: { nurse_db_id: { day_idx: [codes...] } }
            forbidden = initial_constraints.get('forbidden') or {}
            # print('forbidden!!!!!', forbidden)
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
                        for code in (codes or []): # codes: ['D', 'E', 'N']
                            raw_code = str(code or "").strip().upper()
                            if raw_code == "M":
                                norm_code = "M"
                            else:
                                norm_code = _normalize_shift_code(raw_code, shift_id_to_main)
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
            setattr(roster_system, "_used_fallback", False)
            success = self._optimize_with_enhanced_constraints(roster_system, time_limit_seconds, nurses, grouped, randomize=randomize, seed=seed)
            # fallback_success = False
            if not success:
                print(f"{self.logger_prefix} 개선된 제약사항으로 실패, 기본 알고리즘으로 폴백...")
                setattr(roster_system, "_used_fallback", True)
                self._optimize_fallback_lex_hard_first(
                    roster_system,
                    time_limit_seconds=time_limit_seconds,
                    grouped=grouped,
                    shift_type_map=shift_id_to_type,
                )
            # if not success and not fallback_success:
            #     raise RuntimeError("HARD_INFEASIBLE: stage1/fallback 모두 해 없음")
        # 9-1. 불필요 OFF 정리 (N-only 제외)
        # try:
        #     with Timer("불필요 OFF 정리"):
        #         trimmed = self._postprocess_trim_extra_offs(roster_system, max_changes=80, prefer_shortage=True)
        #         print(f"{self.logger_prefix} 불필요 OFF 교체 {trimmed}건")
        # except Exception as exc:
        #     print(f"{self.logger_prefix} 불필요 OFF 후처리 실패: {exc}")

        # ── 후처리 완료 후 프리셉티 roster를 프리셉터와 동기화 ──
        # 규칙: 프리셉터의 DEN/O → 프리셉티 동일 복사
        #       프리셉터의 특수코드(법,생,휴 등 원티드) → 프리셉티는 OFF
        preceptee_follow = bool(getattr(roster_system.config, 'preceptee_on', False))
        if preceptee_follow and hasattr(roster_system, 'nurses'):
            _shift_types = roster_system.config.shift_types
            _off_s_idx = _shift_types.index('O') if 'O' in _shift_types else None
            _standard = {'D', 'E', 'N', 'O'}
            if bool(getattr(roster_system.config, 'use_mid', False)):
                _standard.add('M')
            id_to_idx = {nu.db_id: n for n, nu in enumerate(roster_system.nurses)}
            synced = 0
            special_converted = 0
            _pte_fw_map = getattr(roster_system, '_preceptee_fixed_wanted_map', {})
            _fw_restored = 0
            _pp_follow_days = getattr(roster_system, 'preceptee_follow_days', {}) or {}
            for n, nu in enumerate(roster_system.nurses):
                pid = getattr(nu, 'preceptor_id', None)
                if not pid or pid not in id_to_idx:
                    continue
                ptr_idx = id_to_idx[pid]
                # 1단계: 프리셉터 roster 복사 (기간 제한 적용)
                _follow_set = _pp_follow_days.get(n)
                print(f"[PrecepteeSync] n={n}, nurse={nu.name}({nu.db_id}), _follow_set={'set('+str(sorted(_follow_set))+')' if _follow_set is not None else 'None'}, _pp_follow_days_keys={list(_pp_follow_days.keys())}")
                if _follow_set is not None:
                    # 기간 내 day만 프리셉터 복사
                    for d in _follow_set:
                        if d < roster_system.num_days:
                            roster_system.roster[n, d, :] = roster_system.roster[ptr_idx, d, :]
                else:
                    # 기간 미설정 → 전체 월 복사 (기존 동작)
                    roster_system.roster[n] = roster_system.roster[ptr_idx].copy()
                # 2단계: 특수코드 일자는 프리셉티를 OFF로 전환
                if _off_s_idx is not None:
                    for d in range(roster_system.num_days):
                        # 기간 외 day는 skip (독립 배정 유지)
                        if _follow_set is not None and d not in _follow_set:
                            continue
                        if (n, d) in _pte_fw_map:
                            _fw_code = _pte_fw_map[(n, d)]
                            if _fw_code in _shift_types:
                                roster_system.roster[n, d, :] = 0
                                roster_system.roster[n, d, _shift_types.index(_fw_code)] = 1
                                _fw_restored += 1
                                continue
                        need_off = False
                        _original = fixed_original_shift_map.get((ptr_idx, d))
                        if _original:
                            # 원본 코드 존재 → 원본 기준으로만 판정 (엔진 정규화 코드 무시)
                            _ou = _original.upper()
                            if _ou not in _standard and _ou not in _work_sub_ids:
                                need_off = True
                        else:
                            # 원본 없으면 roster 코드 기준
                            _idx_arr = np.where(roster_system.roster[ptr_idx, d] == 1)[0]
                            if len(_idx_arr) > 0:
                                _s_code = _shift_types[int(_idx_arr[0])]
                                if _s_code not in _standard and _s_code.upper() not in _work_sub_ids:
                                    need_off = True
                        if need_off:
                            roster_system.roster[n, d, :] = 0
                            roster_system.roster[n, d, _off_s_idx] = 1
                            special_converted += 1
                            
                            # 디버깅용
                            # preceptor_code = _shift_types[int(np.where(roster_system.roster[ptr_idx, d] == 1)[0][0])]
                            # original_code = fixed_original_shift_map.get((ptr_idx, d), '없음')
                            # print(f"  → [{nu.name} <- {roster_system.nurses[ptr_idx].name}] {d+1}일차 : "
                            #       f"preceptor={preceptor_code}, original={original_code} → OFF")
                synced += 1
            if synced:
                msg = f"{self.logger_prefix} [PrecepteeSync] 후처리 후 프리셉티 roster 동기화: {synced}명"
                if special_converted:
                    msg += f" (특수코드→OFF 전환: {special_converted}건)"
                if _fw_restored:
                    msg += f", fixed_wanted 재적용: {_fw_restored}건"
                print(msg)
            # if bool(getattr(roster_system.config, 'ban_e_to_d', True)) and _off_s_idx is not None:
            #     _eve_s_idx = _shift_types.index('E') if 'E' in _shift_types else None
            #     _day_s_idx = _shift_types.index('D') if 'D' in _shift_types else None
            #     _repair_cnt = 0
            #     _blocked_cnt = 0
            #     if _eve_s_idx is not None and _day_s_idx is not None:
            #         for n, nu in enumerate(roster_system.nurses):
            #             pid = getattr(nu, 'preceptor_id', None)
            #             if not pid or pid not in id_to_idx:
            #                 continue
            #             for d in range(1, roster_system.num_days):
            #                 if int(roster_system.roster[n, d - 1, _eve_s_idx]) != 1:
            #                     continue
            #                 if int(roster_system.roster[n, d, _day_s_idx]) != 1:
            #                     continue
            #                 cur_fixed = (n, d) in _pte_fw_map
            #                 prev_fixed = (n, d - 1) in _pte_fw_map
            #                 if not cur_fixed:
            #                     roster_system.roster[n, d, :] = 0
            #                     roster_system.roster[n, d, _off_s_idx] = 1
            #                     _repair_cnt += 1
            #                 elif not prev_fixed:
            #                     roster_system.roster[n, d - 1, :] = 0
            #                     roster_system.roster[n, d - 1, _off_s_idx] = 1
            #                     _repair_cnt += 1
            #                 else:
            #                     _blocked_cnt += 1
            #     if _repair_cnt or _blocked_cnt:
            #         print(
            #             f"{self.logger_prefix} [PrecepteeSync][Repair-E->D] repaired={_repair_cnt}, blocked_fixed={_blocked_cnt}"
            #         )
                
            # 추가: 프리셉티의 fixed_cells를 프리셉터 값으로 강제 덮어쓰기
            if preceptee_follow and synced > 0:
                fixed_cells = getattr(roster_system, "fixed_cells", []) or []
                new_fixed = []
                preceptor_fixed = {}  # preceptor별 fixed 캐시

                for cell in fixed_cells:
                    n_idx = cell.get("nurse_index")
                    if n_idx in id_to_idx:
                        nu = roster_system.nurses[n_idx]
                        if getattr(nu, 'preceptor_id', None):
                            # 프리셉티 fixed는 스킵 (아래에서 새로 만듦)
                            continue
                        key = (n_idx, cell.get("day_index"))
                        preceptor_fixed[key] = cell

                for n, nu in enumerate(roster_system.nurses):
                    pid = getattr(nu, 'preceptor_id', None)
                    if not pid or pid not in id_to_idx:
                        # 프리셉티 아닌 경우 기존 유지
                        for cell in fixed_cells:
                            if cell.get("nurse_index") == n:
                                new_fixed.append(cell)
                        continue

                    ptr_idx = id_to_idx[pid]
                    for d in range(roster_system.num_days):
                        key = (ptr_idx, d)
                        if key in preceptor_fixed:
                            copied = preceptor_fixed[key].copy()
                            copied["nurse_index"] = n
                            new_fixed.append(copied)

                _new_fixed_by_key = {}
                for _cell in new_fixed:
                    _k = (_cell.get('nurse_index'), _cell.get('day_index'))
                    _new_fixed_by_key[_k] = _cell
                _pte_fw_raw = _preceptee_fixed_wanted_map_raw
                for (_n_idx, _d_idx), _fw_code in _pte_fw_map.items():
                    _raw_shift = _pte_fw_raw.get((_n_idx, _d_idx), _fw_code) if isinstance(_pte_fw_raw, dict) else _fw_code
                    _new_fixed_by_key[(_n_idx, _d_idx)] = {
                        'nurse_index': _n_idx,
                        'day_index': _d_idx,
                        'shift': _raw_shift,
                    }
                _final_fixed = list(_new_fixed_by_key.values())
                roster_system.fixed_cells = _final_fixed
                print(f"[PrecepteeSync-Fixed] fixed_cells 프리셉터 동기화 완료: {len(_final_fixed)}개")

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

        # Grade 배치 요약 출력/CSV 저장 및 로그 (GRADE 전략일 때만)
        try:
            grade_strategy_norm = str(grade_strategy or "BASE").upper()
            if grade_strategy_norm == "GRADE" and grade_config:
                _dump_grade_summary(roster_system, nurses, grade_config, self.logger_prefix)
                _log_grade_result(
                    roster_system, nurses, grade_config, self.logger_prefix, label="solve 직후"
                )
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
                # Grade Repair 이후 프리셉티 재동기화
                _pf = bool(getattr(roster_system.config, 'preceptee_on', False))
                if _pf and hasattr(roster_system, 'nurses'):
                    _st = roster_system.config.shift_types
                    _oi = _st.index('O') if 'O' in _st else None
                    _std = {'D', 'E', 'N', 'O'}
                    if bool(getattr(roster_system.config, 'use_mid', False)):
                        _std.add('M')
                    _id2i = {nu.db_id: n for n, nu in enumerate(roster_system.nurses)}
                    _sc = 0
                    _gr_pte_fw = getattr(roster_system, '_preceptee_fixed_wanted_map', {})
                    _gr_fw_restored = 0
                    for _n, _nu in enumerate(roster_system.nurses):
                        _pid = getattr(_nu, 'preceptor_id', None)
                        if not _pid or _pid not in _id2i:
                            continue
                        _pi = _id2i[_pid]
                        _gr_follow_set = _pp_follow_days.get(_n) if _pp_follow_days else None
                        if _gr_follow_set is not None:
                            for _fd in _gr_follow_set:
                                if _fd < roster_system.num_days:
                                    roster_system.roster[_n, _fd, :] = roster_system.roster[_pi, _fd, :]
                        else:
                            roster_system.roster[_n] = roster_system.roster[_pi].copy()
                        if _oi is not None:
                            for _d in range(roster_system.num_days):
                                if _gr_follow_set is not None and _d not in _gr_follow_set:
                                    continue
                                if (_n, _d) in _gr_pte_fw:
                                    _fw_code = _gr_pte_fw[(_n, _d)]
                                    if _fw_code in _st:
                                        roster_system.roster[_n, _d, :] = 0
                                        roster_system.roster[_n, _d, _st.index(_fw_code)] = 1
                                        _gr_fw_restored += 1
                                        continue
                                _need = False
                                _orig = fixed_original_shift_map.get((_pi, _d))
                                if _orig:
                                    _ou2 = _orig.upper()
                                    if _ou2 not in _std and _ou2 not in _work_sub_ids:
                                        _need = True
                                else:
                                    _ia = np.where(roster_system.roster[_pi, _d] == 1)[0]
                                    if len(_ia) > 0:
                                        _sc2 = _st[int(_ia[0])]
                                        if _sc2 not in _std and _sc2.upper() not in _work_sub_ids:
                                            _need = True
                                if _need:
                                    roster_system.roster[_n, _d, :] = 0
                                    roster_system.roster[_n, _d, _oi] = 1
                        _sc += 1
                    if _sc:
                        _msg = f"{self.logger_prefix} [PrecepteeSync] Grade Repair 후 프리셉티 재동기화: {_sc}명"
                        if _gr_fw_restored:
                            _msg += f", fixed_wanted 재적용: {_gr_fw_restored}건"
                        print(_msg)
                _log_grade_result(
                    roster_system, nurses, grade_config, self.logger_prefix, label="최종(Repair 후)"
                )
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

        # nurse별 work_shifts에 맞춰 최종 근무 코드를 대체한다.
        result = self._apply_work_shift_overrides(
            roster_map=result,
            nurses_data=nurses_data,
            shift_definitions=shift_defs,
        )
        # 프리셉티 최종 결과를 프리셉터 결과로 직접 동기화
        # (roster 레벨은 엔진 정규화 코드(W 등)를 사용하므로, 최종 result 문자열 기준으로 덮어쓴다)
        if bool(getattr(roster_system.config, 'preceptee_on', False)):
            _off_code = canonical_to_shift_id.get('O', 'O')
            _id2i_final = {nu.db_id: n for n, nu in enumerate(nurses)}
            _synced_final = 0
            _synced_final_fw = 0
            _pte_fw_raw_final = _preceptee_fixed_wanted_map_raw
            _pte_fw_norm_final = getattr(roster_system, '_preceptee_fixed_wanted_map', {})
            _pp_fw_final = getattr(roster_system, 'preceptee_follow_days', {}) or {}
            for nu in nurses:
                _pid_f = getattr(nu, 'preceptor_id', None)
                if not _pid_f or _pid_f not in _id2i_final:
                    continue
                ptr_sched = result.get(_pid_f, [])
                pte_sched = result.get(nu.db_id, [])
                if not ptr_sched or not pte_sched:
                    continue
                _n_final = _id2i_final.get(nu.db_id)
                _final_follow_set = _pp_fw_final.get(_n_final) if _n_final is not None else None
                changed = False
                for d_i in range(min(len(ptr_sched), len(pte_sched))):
                    # 기간 제한: follow 기간 외 day는 skip
                    if _final_follow_set is not None and d_i not in _final_follow_set:
                        continue
                    _n_idx_final = _id2i_final.get(nu.db_id)
                    _fw_code_final = None
                    if _n_idx_final is not None:
                        if isinstance(_pte_fw_raw_final, dict):
                            _fw_code_final = _pte_fw_raw_final.get((_n_idx_final, d_i))
                        if not _fw_code_final:
                            _fw_norm = _pte_fw_norm_final.get((_n_idx_final, d_i)) if isinstance(_pte_fw_norm_final, dict) else None
                            if _fw_norm:
                                _fw_code_final = canonical_to_shift_id.get(_fw_norm, _fw_norm)
                    if _fw_code_final:
                        _fw_code_final = str(_fw_code_final).strip()
                        if _fw_code_final and pte_sched[d_i] != _fw_code_final:
                            pte_sched[d_i] = _fw_code_final
                            changed = True
                            _synced_final_fw += 1
                        continue
                    ptr_code = str(ptr_sched[d_i]).strip()
                    ptr_u = ptr_code.upper()
                    if ptr_u in {'D', 'E', 'N', 'O'} or ptr_u in _work_sub_ids:
                        # DEN/O 또는 근무 하위코드 → 프리셉터와 동일
                        if pte_sched[d_i] != ptr_code:
                            pte_sched[d_i] = ptr_code
                            changed = True
                    else:
                        # 특수코드(휴가/공가 등) → 프리셉티는 OFF
                        if str(pte_sched[d_i]).upper() != _off_code.upper():
                            pte_sched[d_i] = _off_code
                            changed = True
                if changed:
                    result[nu.db_id] = pte_sched
                    _synced_final += 1
            if _synced_final:
                _msg = f"{self.logger_prefix} [PrecepteeSync] 최종 result 동기화: {_synced_final}명"
                if _synced_final_fw:
                    _msg += f", fixed_wanted 재적용: {_synced_final_fw}건"
                print(_msg)
        # 디버그: 최종 결과에서 O/휴가/주휴가 OFF로 어떻게 카운트되는지 확인
        try:
            weekly_off_by_idx = (
                getattr(roster_system, "weekly_off_by_idx", {})
                if isinstance(getattr(roster_system, "weekly_off_by_idx", {}), dict)
                else {}
            )
            for nu in nurses:
                if str(getattr(nu, "db_id", "")) not in watch_db_ids:
                    continue
                sched = result.get(str(nu.db_id), [])
                n_idx = getattr(nu, "id", None)
                weekly = weekly_off_by_idx.get(int(n_idx), []) if n_idx is not None else []
                _log_off_count_trace(
                    logger_prefix=self.logger_prefix,
                    nurse=nu,
                    schedule=sched,
                    shift_id_to_main=shift_id_to_main,
                    shift_id_to_type=shift_id_to_type,
                    weekly_off_days_0based=weekly,
                )
        except Exception as exc:
            print(f"{self.logger_prefix} [WatchOffCount] 로그 실패: {exc}")

        # 13. 최종 근무표 로그 출력
        self._log_final_roster(nurses, result)
        # _log_weekend_work_summary(roster_system, self.logger_prefix)

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
        print(
            f"{self.logger_prefix} [Progress] base_tl={base_tl}s, "
            f"remaining={time_limit_seconds - base_tl}s"
        )
        roster_system.is_quick_phase = True
        feasible = self._quick_initial_solve(
            roster_system, base_tl, grouped, run_seed)
        roster_system.is_quick_phase = False
        print(f"{self.logger_prefix} [Progress] 초기해={int(bool(feasible))}")
        if not feasible:
            print(
                f"{self.logger_prefix} [Progress] 초기해 실패 -> 이웃 탐색 생략, 폴백으로 전환"
            )
            return False
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

        if max_iter == 0:
            print(
                f"{self.logger_prefix} [Progress] 반복 없음: "
                f"best_viol={best_viol}, remaining={remaining}s"
            )
            return best_viol == 0
        for it in range(max_iter):
            try:
                n_sel, d_sel = policy.select()
                print(
                    f"{self.logger_prefix} [Progress] iter={it + 1}/{max_iter}, "
                    f"n_sel={len(n_sel)}, d_sel={len(d_sel)}"
                )
                ok, status_text = _solve_neighbourhood(
                    roster_system, n_sel, d_sel, per_iter, grouped, run_seed, it=it
                )
            except Exception as e:
                print(f"{self.logger_prefix} 근무표 생성 중 오류: {e}")
                raise e
            if not ok:
                print(
                    f"{self.logger_prefix} [Progress] iter={it + 1} 실패: "
                    f"status={status_text}"
                )
                policy.update(False, n_sel, d_sel)
                continue
            curr_viol = hard_violation_cnt()
            improved  = curr_viol < best_viol
            if improved:
                best_viol = curr_viol;  best_roster = roster_system.roster.copy()
            else:  # rollback
                roster_system.roster = best_roster.copy()
            policy.update(improved, n_sel, d_sel)
            print(
                f"{self.logger_prefix} [Progress] iter={it + 1} "
                f"status={status_text}, curr_viol={curr_viol}, "
                f"best_viol={best_viol}, improved={int(improved)}"
            )
            if best_viol == 0:
                print(f"{self.logger_prefix} [Progress] 하드 위반 0 달성, 종료")
                break
        roster_system.roster = best_roster
        log_n_even_distribution(roster_system, self.logger_prefix)
        if best_viol > 0:
            try:
                violations = [
                    v for v in roster_system._find_violations()
                    if v.get('type') in HARD_TYPES
                ]
                by_type: dict[str, int] = {}
                for v in violations:
                    t = str(v.get('type') or 'unknown')
                    by_type[t] = by_type.get(t, 0) + 1
                print(
                    f"{self.logger_prefix} [HardViolations] total={len(violations)}, by_type={by_type}"
                )
                if by_type.get('shift_requirement', 0) > 0:
                    _log_shift_requirement_gaps(roster_system)
                for v in violations[:12]:
                    nurse_name = v.get('nurse_name') or v.get('name') or '?'
                    nurse_id = v.get('nurse_id') or '?'
                    day = v.get('day')
                    detail = v.get('detail') or v.get('message') or ''
                    print(
                        f"{self.logger_prefix} [HardViolations] "
                        f"type={v.get('type')}, nurse={nurse_name}({nurse_id}), day={day}, detail={detail}"
                    )
            except Exception as e:
                print(f"{self.logger_prefix} [HardViolations] logging failed: {e}")
            print(
                f"{self.logger_prefix} [Progress] 종료: best_viol={best_viol}, "
                f"max_iter={max_iter}, per_iter={per_iter}s"
            )
        return best_viol == 0


    # ────────────────────────────────────────────────────────────────────
    #                    ※ 아래는 helper 들 – 모두 완전판               │
    # ────────────────────────────────────────────────────────────────────
    def _optimize_fallback_lex_hard_first(
        self,
        roster_system: RosterSystem,
        time_limit_seconds: int,
        grouped=None,
        shift_type_map: dict[str, str] | None = None,
    ) -> bool:
        """(호환) 폴백(서열) 최적화는 별도 모듈로 분리되었다."""
        return optimize_fallback_lex_hard_first(
            roster_system=roster_system,
            time_limit_seconds=time_limit_seconds,
            grouped=grouped,
            shift_type_map=shift_type_map,
            logger_prefix=self.logger_prefix,
            timer_cls=Timer,
            add_preceptor_terms_fn=_add_preceptor_objective_terms,
            add_team_balance_terms_fn=add_team_balance_objective_terms,
            add_grade_constraints_fn=add_grade_constraints,
            postprocess_rebalance_off_fn=self._postprocess_rebalance_off,
            blocked_by_nurse=getattr(roster_system, 'blocked_by_nurse', None),
        )

    def _quick_initial_solve(self, rs: RosterSystem,
                             tl:int, grouped, run_seed: int | None = None):
        from ortools.sat.python import cp_model
        try:
            model,X,j,l,fixed = _build_full_model(rs,grouped)
            # print('model', model)
            # print('X', X)
            # print('j', j)
            # print('l', l)
            # print('fixed', fixed)
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
            leave_phys = [min(int(x), D - 1) for x in l]
            for n in range(N):
                for d in range(j[n], leave_phys[n] + 1):
                    for s in range(S):
                        if solver.Value(X(n,d,s)): rs.roster[n,d,s]=1
            log_n_even_distribution(rs, self.logger_prefix, join=j, leave=leave_phys)
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
        """(호환) RosterSystem 결과를 DB 형식으로 변환한다."""
        return _convert_result_to_db_format_impl(
            roster_system,
            nurses,
            canonical_to_shift_id=canonical_to_shift_id,
            fixed_original_shift_map=fixed_original_shift_map,
        )

    def _apply_work_shift_overrides(
        self,
        roster_map: Dict[str, List[str]],
        nurses_data: List[dict],
        shift_definitions: list[dict] | None,
    ) -> Dict[str, List[str]]:
        """(호환) work_shifts 설정을 사용해 간호사별 근무 코드를 맞춤 대체한다."""
        return _apply_work_shift_overrides_impl(
            roster_map=roster_map,
            nurses_data=nurses_data,
            shift_definitions=shift_definitions,
        )

    def _postprocess_rebalance_off(
        self,
        roster_system: RosterSystem,
        max_attempts: int = 30,
    ) -> None:
        """(호환) 후처리로 불필요한 O를 당겨와 연속근무 위반을 완화합니다."""
        _postprocess_rebalance_off_impl(
            roster_system=roster_system,
            logger_prefix=self.logger_prefix,
            max_attempts=max_attempts,
        )

    def _postprocess_trim_extra_offs(
        self,
        roster_system: RosterSystem,
        max_changes: int = 80,
        prefer_shortage: bool = True,
    ) -> int:
        """(호환) 필수/강제 OFF를 보존하면서 불필요한 OFF를 근무로 교체한다."""
        return _postprocess_trim_extra_offs_impl(
            roster_system=roster_system,
            logger_prefix=self.logger_prefix,
            max_changes=max_changes,
            prefer_shortage=prefer_shortage,
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

def _build_full_model(rs: RosterSystem, grouped, include_pair_objective: bool = True, blocked_by_nurse: Optional[dict[int, set[int]]] = None):
    # rs 객체에 blocked_by_nurse가 있으면 우선 사용
    if blocked_by_nurse is None:
        blocked_by_nurse = getattr(rs, 'blocked_by_nurse', None)
    from ortools.sat.python import cp_model
    m = cp_model.CpModel()
    D_phys = rs.num_days
    # K_lookahead = int(getattr(rs.config, "lookahead_days", 5) or 0)
    K_lookahead = 5
    D = get_D_ext(D_phys, K_lookahead)
    logger.info(
        "[Lookahead] CP-SAT 적용: K=%s, D_phys=%s, D_ext=%s",
        K_lookahead,
        D_phys,
        D,
    )
    N, S = len(rs.nurses), rs.config.num_shifts
    # join / leave index (leave는 당월 물리 마지막일만 사용; 룩어헤드 변수 범위는 leave)
    join, leave = [], []
    has_w = "W" in rs.config.shift_types
    w_idx = rs.config.shift_types.index("W") if has_w else None
    off_exception_cells = set(getattr(rs.config, "off_exception_cells", []) or [])
    off_exception_vacation_cells = set(
        getattr(rs.config, "off_exception_vacation_cells", []) or []
    )
    initial_forbidden = (
        getattr(rs, "initial_forbidden", {})
        if isinstance(getattr(rs, "initial_forbidden", {}), dict)
        else {}
    )
    off_idx_full = rs.config.shift_types.index("O") if "O" in rs.config.shift_types else None
    weekly_off_by_idx = getattr(rs, "weekly_off_by_idx", {}) if isinstance(getattr(rs, "weekly_off_by_idx", {}), dict) else {}
    first_day = rs.target_month
    last_day_phys = first_day + timedelta(days=D_phys - 1)

    for nu in rs.nurses:
        # join
        if nu.joining_date:
            j = (nu.joining_date - first_day).days
        else:
            j = 0

        # leave: 당월 물리 마지막일(inclusive) 기준
        if nu.resignation_date:
            if nu.resignation_date < first_day:
                join.append(1)
                leave.append(0)
                continue
            elif nu.resignation_date > last_day_phys:
                l = D_phys - 1
            else:
                l = (nu.resignation_date - first_day).days
        else:
            l = D_phys - 1

        j = max(j, 0)
        l = min(l, D_phys - 1)

        join.append(j)
        leave.append(l)
        off_exception_cells = set(getattr(rs.config, "off_exception_cells", []) or [])
    leave = compute_leave_ext(leave, D_phys, D) if K_lookahead > 0 else list(leave)
    if K_lookahead > 0:
        logger.info(
            "[Lookahead] leave 적용 (당월 퇴사자 제외, 룩어헤드 구간 d=%s~%s 포함)",
            D_phys,
            D - 1,
        )
    # 고정 셀 (수간호사 등)
    code2main = {
        str(c).strip().upper(): str(r["main_code"]).strip().upper()
        for r in (grouped or [])
        for c in r["codes"]
    }
    code2type = {
        str(c).strip().upper(): r.get("type")
        for r in (grouped or [])
        for c in r["codes"]
    }
    shift_id_to_main_map = {
        str(k).strip().upper(): str(v).strip().upper()
        for k, v in (getattr(rs, "shift_id_to_main", {}) or {}).items()
        if str(k or "").strip() and str(v or "").strip()
    }

    def _normalize_fixed_to_main(raw_code: object) -> str:
        code = str(raw_code or "").strip().upper()
        if not code:
            return ""
        mapped = code2main.get(code) or shift_id_to_main_map.get(code) or code
        if mapped in {"OFF", "주"}:
            return "O"
        return mapped

    fixed, fixed_cnt = {}, [[0]*S for _ in range(D)]
    fixed_type_by_cell: dict[tuple[int, int], Optional[str]] = {}
    fixed_wanted_cells: set[tuple[int, int]] = set()
    for c in getattr(rs,'fixed_cells',[]) or []:
        n,d = c['nurse_index'], c['day_index']
        s_main = _normalize_fixed_to_main(c.get("shift"))
        if s_main not in rs.config.shift_types:
            print(
                f"[CP-SAT-Basic] 고정 셀 코드 스킵(미지원 메인코드): "
                f"n={n}, d={d + 1}, raw={c.get('shift')}, main={s_main}"
            )
            continue
        s_idx  = rs.config.shift_types.index(s_main)
        fixed[(n,d)] = s_idx; fixed_cnt[d][s_idx]+=1
        # 코드에 타입 매핑이 없으면 메인 코드 기준으로 재시도
        raw_code = str(c.get("shift") or "").strip().upper()
        fixed_type_by_cell[(n, d)] = code2type.get(raw_code) or code2type.get(s_main)
        if str(c.get("fixed_source") or "").strip().lower() == "fixed_wanted":
            fixed_wanted_cells.add((n, d))
        # print('이미 있음 cpsat- fixed_type_by_cell', fixed_type_by_cell)
    fixed_off_cells: set[tuple[int, int]] = set()
    fixed_vacation_off_cells: set[tuple[int, int]] = set()
    if off_idx_full is not None:
        fixed_off_cells = {(n, d) for (n, d), s_idx in fixed.items() if s_idx == off_idx_full}
        vacation_types = {"휴가", "공가"}
        fixed_vacation_off_cells = {
            (n, d)
            for (n, d), s_idx in fixed.items()
            if s_idx == off_idx_full and fixed_type_by_cell.get((n, d)) in vacation_types
        }
    fixed_non_off_cells = {
        (n, d)
        for (n, d), s_idx in fixed.items()
        if off_idx_full is not None and s_idx != off_idx_full
    }
    partition = build_off_partitions(
        nurses=rs.nurses,
        num_days=D,
        first_day=first_day,
        fixed_off_cells=fixed_off_cells,
        fixed_vacation_off_cells=fixed_vacation_off_cells,
        off_exception_cells=off_exception_cells,
        off_exception_vacation_cells=off_exception_vacation_cells,
        weekly_off_by_idx=weekly_off_by_idx,
        weekend_off_only_enable=bool(getattr(rs.config, "weekend_off_only_enable", True)),
        include_off_exception_cells=True,
        include_weekly_off_cells=True,
        include_weekend_off_cells=True,
        weekend_within_active_range=False,
        fixed_non_off_cells=fixed_non_off_cells,
    )
    vacation_off_cells = set(partition["vacation_off_cells"])
    structural_off_cells = set(partition["structural_off_cells"])
    weekend_days = set(partition["weekend_days"])
    off_cap_semantics = off_cap_semantics_label()
    print(
        f"[OffPolicy][Partition] cap_semantics={off_cap_semantics}, "
        f"vacation_cells={len(vacation_off_cells)}, structural_cells={len(structural_off_cells)}"
    )
    forced_off_cells: set[tuple[int, int]] = set(fixed_off_cells)
    # 룩어헤드 구간 주휴: 당월 weekly_off_by_idx가 아닌 별도 계산 셀만 사용
    lookahead_weekly_off = getattr(rs, "lookahead_weekly_off_cells", set()) or set()
    if lookahead_weekly_off:
        structural_off_cells.update(lookahead_weekly_off)
    forced_off_cap_excluded: set[tuple[int, int]] = set(vacation_off_cells)

    # N 금지 간호사 판별(모든 근무일에 N이 금지된 경우)
    n_forbid_n: set[int] = _n_forbid_n_set(rs, join, leave)
    if n_forbid_n:
        try:
            n_forbid_list = [
                f"{getattr(rs.nurses[n], 'name', '?')}({getattr(rs.nurses[n], 'nurse_id', '?')})"
                for n in sorted(n_forbid_n)
                if n < len(rs.nurses)
            ]
            print(
                f"[N-Forbid] N 전일 금지 간호사 수={len(n_forbid_n)} "
                f"list={n_forbid_list}"
            )
        except Exception as e:
            print(f"[N-Forbid] 로깅 실패: {e}")


    # 변수 (룩어헤드 시 leave까지 생성)
    Xv = {}
    for n in range(N):
        for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
            for s in range(S):
                Xv[n, d, s] = m.NewBoolVar(f"x_{n}_{d}_{s}")
    _false_var = m.NewConstant(0)
    def X(n, d, s):
        return Xv.get((n, d, s), _false_var)

    def countable_off(n: int, d: int) -> int:
        """vacation_off_cells를 제외한 O 변수만 반환한다."""
        if off_idx_full is None:
            return 0
        if (n, d) in vacation_off_cells:
            return 0
        return X(n, d, off_idx_full)
    active_days = build_active_days(N, join, leave, blocked_by_nurse)
    isolated_off_slacks: list = []

    # ── 프리셉티 인덱스 사전 계산 (preceptee_on 무관하게 항상 빌드 — 커버리지 제외에 필요) ──
    preceptee_follow = bool(getattr(rs.config, 'preceptee_on', False))
    preceptee_indices: set[int] = set()
    _id_to_idx_pre = {nu.db_id: n for n, nu in enumerate(rs.nurses)}
    for n, nu in enumerate(rs.nurses):
        pid = getattr(nu, 'preceptor_id', None)
        if pid:
            preceptee_indices.add(n)
    # 프리셉티 기간 제한
    preceptee_follow_days: dict[int, set[int]] = getattr(rs, "preceptee_follow_days", {}) or {}
    _has_preceptee_period = bool(preceptee_follow_days)
    # dispatch(assignment) 기반 프리셉티도 인덱스에 포함
    if _has_preceptee_period:
        for n in preceptee_follow_days:
            if n not in preceptee_indices:
                preceptee_indices.add(n)
    preceptee_indices = set(preceptee_indices)
    if preceptee_indices:
        print(f"[FIX] 프리셉티 인덱스: {len(preceptee_indices)}명 (follow={preceptee_follow})")
    # 기간이 빈 set인 프리셉티 = 해당 월에서 프리셉티 아님 → preceptee_indices에서 제거
    if _has_preceptee_period:
        _empty_period = {n for n, days in preceptee_follow_days.items() if len(days) == 0}
        if _empty_period:
            preceptee_indices -= _empty_period
            print(f"[FIX] 프리셉티 기간 종료 → 인덱스 제거: {_empty_period}")

    def _is_preceptee_at(n: int, d: int = -1) -> bool:
        if not preceptee_follow or n not in preceptee_indices:
            return False
        if not _has_preceptee_period:
            return True  # 기간 미설정 → 전체 월 follow
        if n not in preceptee_follow_days:
            return True  # 이 간호사에 대한 기간 미설정 → 전체 월 follow
        if d < 0:
            return False  # nurse-level: 기간 설정됨 → 제약 skip 안 함 (day별 판별 필요)
        return d in preceptee_follow_days[n]

    # ───────────── 2-A. 고정 셀  ─────────────
    for (n,d),s_idx in fixed.items():
        if (n, d) not in active_days:
            print(f"[CP-SAT-Basic] 고정 셀 무시: n={n}, d={d+1} (퇴사/입사 범위 밖)")
            continue
        # 프리셉티는 고정 셀 스킵 (프리셉터의 스케줄을 따라감, 기간 내만)
        if _is_preceptee_at(n, d):
            continue
        m.Add(X(n,d,s_idx)==1)
        for s in range(S):
            if s!=s_idx: m.Add(X(n,d,s)==0)
    # W(특별 근무)는 고정 셀 외에는 전부 금지
    if has_w and w_idx is not None:
        for n in range(N):
            if _is_preceptee_at(n):
                continue
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                if (n, d) in fixed and fixed[(n, d)] == w_idx:
                    continue
                m.Add(X(n, d, w_idx) == 0)

    # 순수 O/주 4연속 금지 (예외/강제 포함 시 스킵, fixed로 이미 4O/주면 경고만)
    # config.skip_4o_hard_first_days: 월초 N일 구간에서는 4O Hard 미적용 (기본 3 → 1~3일 시작 윈도우는 4연속 O 허용)
    if off_idx_full is not None:
        vac_cells = set(vacation_off_cells)
        off_or_weekly = {cell for cell in structural_off_cells if cell not in vac_cells}
        skip_4o_hard_first_days = int(getattr(rs.config, "skip_4o_hard_first_days", 3) or 0)
        for n in range(N):
            if _is_preceptee_at(n):
                continue
            for d in range(join[n], leave[n] - 2):
                if d + 3 > leave[n]:
                    continue
                # blocked day가 윈도우에 포함되면 스킵
                if any((n, d + k) not in active_days for k in range(4)):
                    continue
                # if skip_4o_hard_first_days > 0 and d < skip_4o_hard_first_days:
                #     continue
                fixed_o_cnt = sum(
                    1
                    for (fn, fd), fs_idx in fixed.items()
                    if fn == n
                    and fd in {d, d + 1, d + 2, d + 3}
                    and fs_idx == off_idx_full
                    and (fn, fd) not in vac_cells
                )
                if fixed_o_cnt >= 4:
                    print(
                        f"[CP-SAT-Basic] [4O-skip-fixed] nurse_idx={n}, days={d+1},{d+2},{d+3},{d+4} (fixed O x{fixed_o_cnt})"
                    )
                    continue
                # off_or_weekly(주휴·주말휴무 등)와 countable_off(X) 중복 카운팅 방지:
                # structural OFF는 이미 X=1로 고정되므로 countable_off만 사용
                off_like = [
                    1 if (n, d + k) in off_or_weekly else countable_off(n, d + k)
                    for k in range(4)
                ]
                m.Add(sum(off_like) <= 3)

    # ── 4O 월경계 제약: 전월 꼬리 연속 OFF + 현월 초 연속 OFF 합산 4 이상 금지 ──
    _4o_cross_affected: set[int] = set()  # OffCap 보정 대상
    if off_idx_full is not None:
        prev_off_tail = getattr(rs, "prev_month_off_tail_by_idx", {}) or {}
        print(f"[CP-SAT-Basic] [4O-cross-month-debug] prev_off_tail keys={list(prev_off_tail.keys())}, "
              f"values={dict(prev_off_tail)}, N={N}")
        for n in range(N):
            if _is_preceptee_at(n):
                continue
            t = prev_off_tail.get(n, 0)
            if t <= 0 or t >= 4:
                continue
            if join[n] > 0:
                continue
            need = 4 - t
            window_days = list(range(0, min(need, leave[n] + 1)))
            if len(window_days) < need:
                continue
            free_vars = []
            effective_t = t
            _detail_per_day = []
            for wd in window_days:
                in_off_weekly = (n, wd) in off_or_weekly
                in_fixed_off = (n, wd) in fixed and fixed[(n, wd)] == off_idx_full
                is_fixed_off = in_off_weekly or in_fixed_off
                if is_fixed_off:
                    effective_t += 1
                    _detail_per_day.append(f"day{wd}=고정OFF(off_weekly={in_off_weekly},fixed={in_fixed_off})")
                else:
                    free_vars.append(countable_off(n, wd))
                    _detail_per_day.append(f"day{wd}=free")
            if effective_t >= 4:
                nu = rs.nurses[n] if n < len(rs.nurses) else None
                print(f"[CP-SAT-Basic] [4O-cross-month-SKIP] nurse_idx={n}, "
                      f"name={getattr(nu, 'name', '?')}, prev_tail={t}, "
                      f"effective_t={effective_t}>=4 → 제약 스킵, detail={_detail_per_day}")
                continue
            if not free_vars:
                continue
            remaining = 3 - effective_t
            m.Add(sum(free_vars) <= remaining)
            _4o_cross_affected.add(n)
            nu = rs.nurses[n] if n < len(rs.nurses) else None
            print(
                f"[CP-SAT-Basic] [4O-cross-month] nurse_idx={n}, "
                f"name={getattr(nu, 'name', '?')}, prev_tail={t}, "
                f"고정OFF={effective_t - t}, free={len(free_vars)}, OFF<={remaining}, "
                f"detail={_detail_per_day}"
            )

    # ───────────── 2-A2. 고립 OFF 금지(슬랙 허용) ─────────────
    enforce_clustered_offs = bool(getattr(rs.config, "enforce_clustered_offs", False))
    if enforce_clustered_offs and off_idx_full is not None:
        slack_penalty = int(getattr(rs.config, "isolated_off_slack_penalty", 300000) or 0)
        for n in range(N):
            if _is_preceptee_at(n):
                continue
            t0, t1 = join[n], leave[n]
            for d in range(t0, t1 + 1):
                neighbours = []
                if d - 1 >= t0:
                    neighbours.append(countable_off(n, d - 1))
                if d + 1 <= t1:
                    neighbours.append(countable_off(n, d + 1))
                slack_var = m.NewBoolVar(f"iso_off_slack_{n}_{d}")
                isolated_off_slacks.append((slack_var, slack_penalty))
                if neighbours:
                    m.Add(countable_off(n, d) <= sum(neighbours) + slack_var)
                else:
                    m.Add(countable_off(n, d) <= slack_var)
    
    # # ───────────── 2-A3. 주휴 근처 OFF 배치 제약 (off_placement_mode) ─────────────
    # off_placement_mode = int(getattr(rs.config, "off_placement_mode", 0) or 0)
    # prev_month_last_is_off = getattr(rs, "prev_month_last_is_off", {}) if isinstance(getattr(rs, "prev_month_last_is_off", {}), dict) else {}
    
    # if off_placement_mode > 0 and weekly_off_by_idx:
    #     # 커버리지 부족일 계산 (fallback_lex.py와 동일한 로직)
    #     shortage_days: set[int] = set()
    #     try:
    #         for d in range(D):
    #             if (
    #                 hasattr(rs.config, "daily_shift_requirements_by_day")
    #                 and isinstance(rs.config.daily_shift_requirements_by_day, list)
    #                 and d < len(rs.config.daily_shift_requirements_by_day)
    #             ):
    #                 need_map = rs.config.daily_shift_requirements_by_day[d]
    #             else:
    #                 need_map = rs.config.daily_shift_requirements
    #             total_need = sum(int(v) for v in (need_map or {}).values())
    #             active_cnt = sum(1 for n in range(N) if join[n] <= d <= leave[n])
    #             fixed_off_cnt = (
    #                 sum(
    #                     1
    #                     for (fn, fd), s_idx in fixed.items()
    #                     if s_idx == off_idx_full
    #                     and fd == d
    #                     and (fn, fd) not in vacation_off_cells
    #                 )
    #                 if off_idx_full is not None
    #                 else 0
    #             )
    #             avail_eff = max(0, active_cnt - fixed_off_cnt)
    #             if avail_eff < total_need:
    #                 shortage_days.add(d)
    #     except Exception:
    #         shortage_days = set()
        
    #     for n, day_list in weekly_off_by_idx.items():
    #         if n >= len(join):
    #             continue
    #         if _is_preceptee_at(n):
    #             continue
    #         # 주말 휴무 대상자는 주휴 인접 OFF 배치를 적용하지 않는다.
    #         if bool(getattr(rs.nurses[n], "is_weekend_off", False)):
    #             continue
    #         T0, T1 = join[n], leave[n]
    #         for d_raw in day_list or []:
    #             try:
    #                 d = int(d_raw)
    #             except Exception:
    #                 continue
    #             if d < T0 or d > T1:
    #                 continue
    #             if d == D - 1:
    #                 continue
    #             if d == 0:
    #                 if bool(prev_month_last_is_off.get(n, False)):
    #                     continue
    #                 if d + 1 <= T1:
    #                     m.Add(countable_off(n, d + 1) == 1)
    #                 continue
                
    #             if off_placement_mode == 1:
    #                 # 모드 1: 앞/뒤 중 하나에 O 배치
    #                 neighbours = []
    #                 left_pos = d - 1
    #                 right_pos = d + 1
    #                 if left_pos >= T0 and left_pos not in shortage_days:
    #                     neighbours.append(countable_off(n, left_pos))
    #                 if right_pos <= T1 and right_pos not in shortage_days:
    #                     neighbours.append(countable_off(n, right_pos))
    #                 if not neighbours:
    #                     continue
    #                 if len(neighbours) == 1:
    #                     m.Add(neighbours[0] == 1)
    #                 else:
    #                     m.Add(sum(neighbours) >= 1)
    #             elif off_placement_mode == 2:
    #                 # 모드 2: 앞에만 O 배치
    #                 left_pos = d - 1
    #                 if left_pos >= T0 and left_pos not in shortage_days:
    #                     m.Add(countable_off(n, left_pos) == 1)
    
    # ───────────── 2-A2. 초기 금지 셀(경계 제약) ─────────────
    try:
        if hasattr(rs, 'initial_forbidden') and isinstance(rs.initial_forbidden, dict):
            # main code → shift_types 내 해당 shift_id 인덱스 매핑 (D → [Dx, D2, D3, ...])
            _sid_to_main = getattr(rs, "shift_id_to_main", {}) or {}
            _main_to_sidx: dict[str, list[int]] = {}
            for s_idx, sid in enumerate(rs.config.shift_types):
                main = _sid_to_main.get(sid, sid)
                _main_to_sidx.setdefault(main, []).append(s_idx)
            for (n, d), code_list in rs.initial_forbidden.items():
                if _is_preceptee_at(n):
                    continue
                for code in (code_list or []):
                    # main code로 매핑된 모든 shift_id를 금지
                    target_indices = _main_to_sidx.get(code, [])
                    if not target_indices:
                        # 직접 shift_types에 있으면 그것만
                        if code in rs.config.shift_types:
                            target_indices = [rs.config.shift_types.index(code)]
                        else:
                            continue
                    if (n, d) not in active_days:
                        print(f"[CP-SAT-Basic] 초기 금지 무시: n={n}, d={d+1}, code={code} (퇴사/입사 범위 밖)")
                        continue
                    if (n, d) in fixed:
                        print(f"[CP-SAT-Basic] 초기 금지 무시 (유저 고정 우선): n={n}, d={d+1}, code={code}, fixed={rs.config.shift_types[fixed[(n,d)]]}")
                        continue
                    for s_idx in target_indices:
                        m.Add(X(n,d,s_idx)==0)
    except Exception as e:
        print(f"[CP-SAT-Basic] 초기 금지 셀 적용 중 오류: {e}")

    # ───────────── 2-B. Exactly-one ──────────
    for n in range(N):
        for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
            # 프리셉티는 고정 셀 스킵되었으므로 ExactlyOne 필요
            if (n,d) in fixed and not (_is_preceptee_at(n, d)):
                continue
            m.AddExactlyOne(X(n,d,s) for s in range(S))

    # ───────────── 2-B-2. 프리셉티 팔로우 제약 추가 (preceptee_on=True 일 때) ──────────
    # preceptee_follow, preceptee_indices 는 위(2-A1 직전)에서 이미 계산됨
    print(f"[CP-SAT-Basic] preceptee_on={getattr(rs.config, 'preceptee_on', 'MISSING')}, "
          f"preceptee_shift_count={getattr(rs.config, 'preceptee_shift_count', 'MISSING')}, "
          f"preceptee_follow={preceptee_follow}, preceptee_indices={len(preceptee_indices)}명")
    if preceptee_follow and preceptee_indices:
        id_to_idx = {nu.db_id: n for n, nu in enumerate(rs.nurses)}
        constraint_count = 0
        for n in sorted(preceptee_indices):
            nu = rs.nurses[n]
            pid = getattr(nu, 'preceptor_id', None)
            if not pid or pid not in id_to_idx:
                continue
            p = id_to_idx[pid]
            preceptor_nurse = rs.nurses[p]
            d_start = max(join[n], join[p])
            d_end = min(leave[n], leave[p])
            # print(f"[CP-SAT-Basic] 프리셉티 팔로우: {nu.name}(idx={n}) → {preceptor_nurse.name}(idx={p}), "
            #       f"days={d_start}~{d_end}")
            # 하드 제약: 프리셉티(n)의 근무 = 프리셉터(p)의 근무 (기간 내에만)
            for d in range(d_start, d_end + 1):
                if not _is_preceptee_at(n, d):
                    continue
                for s in range(S):
                    xn = X(n, d, s)
                    xp = X(p, d, s)
                    if isinstance(xn, int) or isinstance(xp, int):
                        continue
                    m.Add(xn == xp)
                    constraint_count += 1
        print(f"[CP-SAT-Basic] 프리셉티 팔로우 모드: {len(preceptee_indices)}명, "
              f"제약 {constraint_count}개 추가, 개별 하드제약 면제 적용")

    # preceptee_shift_count=False 이면 DEN 하드제약에서 프리셉티 제외 (preceptee_on 무관)
    _pte_shift_count = getattr(rs.config, 'preceptee_shift_count', True)
    exclude_preceptee_from_den = (not _pte_shift_count) and bool(preceptee_indices)
    _pte_cnt = len(preceptee_indices)
    if exclude_preceptee_from_den:
        print(f"[CP-SAT-Basic] [DEN커버리지] preceptee_shift_count=False → 프리셉티 {_pte_cnt}명 제외, DEN 대상: {N - _pte_cnt}명/{N}명")
    elif preceptee_indices:
        print(f"[CP-SAT-Basic] [DEN커버리지] preceptee_shift_count=True → 프리셉티 {_pte_cnt}명 포함, DEN 대상: {N}명/{N}명")
    # 프리셉티 제외 시 fixed_cnt도 재계산
    if exclude_preceptee_from_den:
        fixed_cnt_adj = [[0] * S for _ in range(D)]
        for (n2, d2), s_idx in fixed.items():
            if n2 not in preceptee_indices:
                fixed_cnt_adj[d2][s_idx] += 1
    else:
        fixed_cnt_adj = fixed_cnt

    m_bucket_indices = compute_main_bucket_indices(
        rs.config.shift_types,
        target_main="M",
        code2main=code2main,
        shift_id_to_main_map=shift_id_to_main_map,
    )

    # ───────────── 2-C. Shift requirements (per-day, slack 허용) ───
    coverage_shortage_vars = []
    over_vars_by_day = {}
    zero_demand_block_codes = {"D", "E", "N", "M"}
    coverage_exclude_cells: set[tuple[int, int]] = getattr(rs, "coverage_exclude_cells", set()) or set()
    cfg = rs.config
    max_by_day = getattr(cfg, "daily_shift_requirements_max_by_day", None)
    _has_any_max = isinstance(max_by_day, list) and any(
        any(int(v or 0) > 0 for v in day_map.values())
        for day_map in max_by_day if isinstance(day_map, dict)
    )
    print(f"[MinMaxCoverage] max_by_day type={type(max_by_day).__name__}, len={len(max_by_day) if isinstance(max_by_day, list) else 'N/A'}, sample={max_by_day[0] if isinstance(max_by_day, list) and max_by_day else 'None'}, has_any_max={_has_any_max}")
    m_coverage_shortage_vars = []  # M soft min shortage (max coverage 없을 때)
    max_coverage_excess_vars = []  # max coverage 초과 soft 패널티
    daily_assigned_by_code: dict[str, list] = {}  # 일자별 커버리지 균등화용 {code: [(day, assigned_var, need, need_max)]}
    next_month_head_req = getattr(cfg, "next_month_head_requirements", None) or []
    for d in range(D):
        # 일자별 요구치(min): 룩어헤드 일자는 next_month_head_requirements 또는 기본값
        if d >= D_phys and isinstance(next_month_head_req, list) and (d - D_phys) < len(next_month_head_req):
            need_map = next_month_head_req[d - D_phys] if isinstance(next_month_head_req[d - D_phys], dict) else cfg.daily_shift_requirements
        elif hasattr(cfg, "daily_shift_requirements_by_day") and isinstance(cfg.daily_shift_requirements_by_day, list) and d < len(cfg.daily_shift_requirements_by_day):
            need_map = cfg.daily_shift_requirements_by_day[d]
        else:
            need_map = cfg.daily_shift_requirements
        # 일자별 요구치(max)
        need_max_map = max_by_day[d] if isinstance(max_by_day, list) and d < len(max_by_day) else None
        for code, req in need_map.items():
            if code not in rs.config.shift_types:
                continue
            s = rs.config.shift_types.index(code)
            req_raw = max(0, int(req or 0))
            need = req_raw - fixed_cnt_adj[d][s]
            # max 요구치 계산 (0이면 상한 없음)
            req_max_raw = int((need_max_map or {}).get(code, 0) or 0)
            need_max = max(0, req_max_raw - fixed_cnt_adj[d][s]) if req_max_raw > 0 else 0
            assigned = sum(
                X(n, d, s)
                for n in range(N)
                if join[n] <= d <= leave[n] and (n, d) not in fixed
                and (not exclude_preceptee_from_den or not _is_preceptee_at(n, d))
                and (n, d) not in coverage_exclude_cells
            )
            if code == "M":
                if m_bucket_indices:
                    assigned_m_bucket = sum(
                        X(n, d, s2)
                        for n in range(N)
                        if join[n] <= d <= leave[n]
                        and (n, d) not in fixed
                        and (not exclude_preceptee_from_den or not _is_preceptee_at(n, d))
                        and (n, d) not in coverage_exclude_cells
                        for s2 in m_bucket_indices
                    )
                else:
                    assigned_m_bucket = assigned
                fixed_m_bucket = (
                    sum(int(fixed_cnt_adj[d][s2] or 0) for s2 in m_bucket_indices)
                    if m_bucket_indices
                    else int(fixed_cnt_adj[d][s] or 0)
                )
                if req_raw == 0:
                    m.Add(assigned_m_bucket == 0)
                    ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                    over_vars_by_day.setdefault(d, {})[code] = ov
                    continue
                m_cap_max = max(0, int(req_max_raw - fixed_m_bucket)) if req_max_raw > 0 else 0
                # M 상한: max coverage 있으면 hard, 없으면 min으로 hard cap
                if m_cap_max > 0:
                    m.Add(assigned_m_bucket <= m_cap_max)
                else:
                    m_cap_non_fixed = max(0, int(req_raw - fixed_m_bucket))
                    m.Add(assigned_m_bucket <= m_cap_non_fixed)
                # M min: max coverage 있으면 hard(D/E/N과 동일), 없으면 soft(shortage 허용)
                if not _has_any_max:
                    m_need = max(0, int(req_raw - fixed_m_bucket))
                    if m_need > 0:
                        m_sh = m.NewIntVar(0, m_need, f"m_short_{d}")
                        m.Add(assigned_m_bucket + m_sh >= m_need)
                        m_coverage_shortage_vars.append(m_sh)
                    ov = m.NewIntVar(0, N, f"over_{d}_{code}")
                    if need > 0:
                        m.Add(ov >= assigned - need)
                    over_vars_by_day.setdefault(d, {})[code] = ov
                    continue
                # max coverage 있으면 아래 일반 하드 로직으로 처리
            if code in zero_demand_block_codes and req_raw == 0:
                m.Add(assigned == 0)
                ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                over_vars_by_day.setdefault(d, {})[code] = ov
                continue
            # min 제약: assigned >= need (하드)
            if need > 0:
                m.Add(assigned >= need)
            # max 제약: hard (상한 초과 불가)
            if need_max > 0:
                m.Add(assigned <= need_max)
                ov = m.NewIntVar(0, 0, f"over_{d}_{code}")
                over_vars_by_day.setdefault(d, {})[code] = ov
                # 일자별 커버리지 균등화용 수집 (물리일만)
                if d < D_phys:
                    daily_assigned_by_code.setdefault(code, []).append((d, assigned, need, need_max))
            elif need > 0:
                ov = m.NewIntVar(0, N, f"over_{d}_{code}")
                m.Add(ov >= assigned - need)
                over_vars_by_day.setdefault(d, {})[code] = ov

    # ───────────── 2-D. Max coverage Off 균등 분배 ───
    max_cov_off_equalize_terms = []
    if _has_any_max:
        off_idx = rs.config.shift_types.index('O')
        nurse_off_vars = []
        for n in range(N):
            # 프리셉티는 프리셉터 스케줄을 따라가므로 균등화 대상에서 제외
            if n in preceptee_indices:
                continue
            T0, T1 = join[n], leave[n]
            phys_days = [d for d in range(T0, min(T1 + 1, D_phys))]
            if not phys_days:
                continue
            total_off_n = m.NewIntVar(0, len(phys_days), f"mc_off_{n}")
            m.Add(
                total_off_n == sum(X(n, d, off_idx) for d in phys_days if (n, d) not in fixed)
                + sum(1 for d in phys_days if (n, d) in fixed and fixed[(n, d)] == off_idx)
            )
            nurse_off_vars.append(total_off_n)
        if len(nurse_off_vars) >= 2:
            off_global_max = m.NewIntVar(0, D_phys, "mc_off_max")
            off_global_min = m.NewIntVar(0, D_phys, "mc_off_min")
            m.AddMaxEquality(off_global_max, nurse_off_vars)
            m.AddMinEquality(off_global_min, nurse_off_vars)
            off_range = m.NewIntVar(0, D_phys, "mc_off_range")
            m.Add(off_range == off_global_max - off_global_min)
            # Off 분산 패널티 (가중치 높게 설정하여 균등 분배 유도)
            max_cov_off_equalize_terms.append(-200 * off_range)
            print(f"[MaxCoverage] Off 균등 분배 제약 추가: 간호사 {len(nurse_off_vars)}명")

    # ───────────── 2-E. 일자별 커버리지 균등화 (min~max 범위 내 고른 배정) ───
    daily_cov_equalize_terms = []
    if _has_any_max and daily_assigned_by_code:
        for code, day_entries in daily_assigned_by_code.items():
            if len(day_entries) < 2:
                continue
            assigned_vars = []
            for d, assigned_expr, _need, _need_max in day_entries:
                av = m.NewIntVar(0, N, f"dcov_{code}_{d}")
                m.Add(av == assigned_expr)
                assigned_vars.append(av)
                # min 초과분 패널티: min에 가깝게 유도
                if _need > 0:
                    excess = m.NewIntVar(0, N, f"dcov_excess_{code}_{d}")
                    m.Add(excess >= av - _need)
                    daily_cov_equalize_terms.append(-80 * excess)
            # 글로벌 range: 월 전체에서 최대-최소 편차 최소화 (블록 쏠림 방지)
            cov_max = m.NewIntVar(0, N, f"dcov_max_{code}")
            cov_min = m.NewIntVar(0, N, f"dcov_min_{code}")
            m.AddMaxEquality(cov_max, assigned_vars)
            m.AddMinEquality(cov_min, assigned_vars)
            cov_range = m.NewIntVar(0, N, f"dcov_range_{code}")
            m.Add(cov_range == cov_max - cov_min)
            daily_cov_equalize_terms.append(-150 * cov_range)
            # 인접일 평활화: 연속된 날 급변 방지
            for i in range(1, len(assigned_vars)):
                diff = m.NewIntVar(0, N, f"dcov_adj_{code}_{i}")
                m.Add(diff >= assigned_vars[i] - assigned_vars[i - 1])
                m.Add(diff >= assigned_vars[i - 1] - assigned_vars[i])
                daily_cov_equalize_terms.append(-60 * diff)
        if daily_cov_equalize_terms:
            print(f"[MaxCoverage] 일자별 커버리지 균등화 제약 추가: {list(daily_assigned_by_code.keys())}")

    # shorthand indices
    idx = {c: rs.config.shift_types.index(c) for c in ('D', 'E', 'N', 'O')}
    day,eve,night,off = idx['D'],idx['E'],idx['N'],idx['O']
    mid = rs.config.shift_types.index('M') if 'M' in rs.config.shift_types else None
    # 주말(토/일) day_idx 집합(0-based)
    weekend_days = {d for d in range(D) if (rs.target_month + timedelta(days=d)).weekday() >= 5}
    try:
        weekend_off_list = [
            f"{getattr(nu, 'name', '?')}({getattr(nu, 'nurse_id', '?')})"
            for nu in rs.nurses
            if bool(getattr(nu, "is_weekend_off", False))
        ]
        print(
            f"[WeekendOff][CP-SAT] 대상 간호사 수={len(weekend_off_list)} "
            f"weekend_days={sorted(weekend_days)} "
            f"list={weekend_off_list}"
        )
    except Exception as e:
        print(f"[WeekendOff][CP-SAT] 로깅 실패: {e}")

    # ───────────── 3. Hard 법규 ───────────────
    cfg = rs.config
    K   = cfg.max_consecutive_work_days
    L   = cfg.max_consecutive_nights

    _log_weekend_off_enforcement_once(
        rs=rs,
        join=join,
        leave=leave,
        weekend_days=weekend_days,
        d_phys=D_phys,
        fixed=fixed,
        off_idx=off,
        logger_prefix="[CP-SAT-Basic]",
    )
    for n,nu in enumerate(rs.nurses):
        T0,T1 = join[n], leave[n]
        # 프리셉티는 프리셉터의 일정을 그대로 따르므로 개별 하드제약 면제
        if _is_preceptee_at(n):
            continue
        # 주말 휴무 제약: is_weekend_off=True인 간호사는 주말(토/일)은 기본적으로 OFF를 강제하고,
        # 평일(월~금)에는 OFF를 금지한다.
        #
        # 예외:
        # - 특정 날짜가 '고정 셀(fixed_cells)'로 이미 근무(D/E/N/W 등)로 지정된 경우,
        #   기존 고정이 우선이며 주말 OFF 강제를 덮어쓰지 않는다.
        if bool(getattr(nu, "is_weekend_off", False)) and getattr(cfg, "weekend_off_only_enable", True):
            try:
                weekend_in_range_all = [d for d in weekend_days if T0 <= d <= T1]
                weekend_in_range = [d for d in weekend_in_range_all if d < D_phys]
                weekend_cnt = len(weekend_in_range)
                vac_cnt_in_range = sum(1 for d in weekend_in_range if (n, d) in vacation_off_cells)
                off_exception_days = sorted(
                    d + 1
                    for (n_idx, d) in off_exception_cells
                    if n_idx == n and T0 <= d <= T1
                )
                fixed_off_days = sorted(
                    d + 1
                    for (n_idx, d) in forced_off_cells
                    if n_idx == n and T0 <= d <= T1 and d < D_phys
                )
                cap_excluded_days = sorted(
                    d + 1
                    for (n_idx, d) in forced_off_cap_excluded
                    if n_idx == n and T0 <= d <= T1
                )
                weekly_off_days = sorted(
                    d + 1 for d in (weekly_off_by_idx.get(n, []) or []) if T0 <= d <= T1
                )
                is_weekend_only = bool(getattr(nu, "is_weekend_off", False))
                weekend_cnt = len(weekend_in_range)
                weekend_nonvac_cnt = max(0, weekend_cnt - vac_cnt_in_range)
                _n_blocked_r = len(blocked_by_nurse.get(n, set())) if blocked_by_nurse else 0
                off_bounds_in_range = compute_off_bounds(
                    source=cfg,
                    avail_days=(T1 - T0 + 1 - _n_blocked_r),
                    vacation_cnt=vac_cnt_in_range,
                    reference_days=D_phys,
                    weekend_only=is_weekend_only,
                    weekend_slots_nonvac=weekend_nonvac_cnt,
                )
                min_off_required = int(off_bounds_in_range["min_off_required"])
                max_off_allowed = int(off_bounds_in_range["max_off_allowed"])
                weekday_off_cap = max(0, max_off_allowed - weekend_cnt)
                print(
                    f"[WeekendOff][HardCheck] nurse_idx={n}, "
                    f"nurse_id={getattr(nu, 'nurse_id', '?')}, name={getattr(nu, 'name', '?')}, "
                    f"range={T0+1}~{T1+1}, weekend_cnt={weekend_cnt}, vac_in_range={vac_cnt_in_range}, "
                    f"min_off_required(excl_vac)={min_off_required}, max_off_allowed(excl_vac)={max_off_allowed}, "
                    f"weekday_off_cap={weekday_off_cap}, "
                    f"K={K}, forbid_n={int(n in n_forbid_n)}, "
                    f"two_offs_after_two_nig={int(bool(getattr(cfg, 'two_offs_after_two_nig', False)))}, "
                    f"two_offs_after_three_nig={int(bool(getattr(cfg, 'two_offs_after_three_nig', False)))}"
                )
                print(
                    f"[WeekendOff][HardCheck][OFF-DETAIL] "
                    f"weekend_days={[(d + 1) for d in weekend_in_range]}, "
                    f"lookahead_weekend_days={[(d + 1) for d in weekend_in_range_all if d >= D_phys]}, "
                    f"weekly_off_days={weekly_off_days}, "
                    f"off_exception_days={off_exception_days}, "
                    f"fixed_off_days={fixed_off_days}, "
                    f"cap_excluded_days={cap_excluded_days}"
                )
                if weekend_cnt < min_off_required:
                    print(
                        f"[WeekendOff][HardCheck][WARN] weekend_cnt({weekend_cnt}) < "
                        f"min_off_required({min_off_required}) → 평일 OFF 필요"
                    )
                if not is_weekend_only and weekend_cnt > max_off_allowed:
                    print(
                        f"[WeekendOff][HardCheck][WARN] weekend_cnt({weekend_cnt}) > "
                        f"max_off_allowed({max_off_allowed}) → OFF 상한 충돌"
                    )
            except Exception as e:
                print(f"[WeekendOff][HardCheck] 로깅 실패: {e}")
            # print('nu!!!!!', nu.__dict__)
            for d in range(T0, T1 + 1):
                if bool(getattr(nu, "is_weekend_off", False)) and d >= D_phys:
                    continue
                if d in weekend_days:
                    # 주말(토/일): 기본 OFF 강제
                    # 단, 고정 셀이 근무로 지정되어 있으면(예: 특수 근무/교육 등) 고정이 우선이다.
                    if (n, d) in fixed and fixed[(n, d)] != off:
                        try:
                            fixed_code = rs.config.shift_types[fixed[(n, d)]]
                        except Exception:
                            fixed_code = str(fixed.get((n, d)))
                        print(
                            f"[WeekendOff][CP-SAT] 주말 OFF 강제 스킵(고정 우선): "
                            f"nurse_index={n}, day={d+1}, fixed_shift={fixed_code}"
                        )
                        continue
                    wd = (rs.target_month + timedelta(days=d)).weekday()
                    
                    print(
                        f"[WeekendOff][HardDebug] X(n,d,off)==1 추가: "
                        f"n={n}, nurse_id={getattr(nu, 'nurse_id', '?')}, name={getattr(nu, 'name', '?')}, "
                        f"d={d+1}, weekday={wd}(토5/일6)"
                    )
                    m.Add(X(n, d, off) == 1)
                else:
                    # 평일(월~금): OFF 금지(D/E/N만 가능)
                    # 사용자 고정 OFF는 예외로 허용
                    if (n, d) in fixed and fixed[(n, d)] == off:
                        print(
                            f"[WeekendOff][HardDebug] 평일 OFF 금지 스킵(고정 OFF): "
                            f"n={n}, nurse_id={getattr(nu, 'nurse_id', '?')}, name={getattr(nu, 'name', '?')}, d={d+1}"
                        )
                        continue
                    if d <= 1 and getattr(rs, "prev_month_n_tail_by_idx", {}).get(n, 0) >= 2:
                        print(
                            f"[WeekendOff][HardDebug] 평일 OFF 금지 스킵(n_tail 월초 복구): "
                            f"n={n}, nurse_id={getattr(nu, 'nurse_id', '?')}, name={getattr(nu, 'name', '?')}, d={d+1}"
                        )
                        continue
                    # off_window 범위 내 평일: 전월 꼬리 연속근무 보정을 위해 OFF 허용 필요
                    _ow_ranges = (getattr(rs, "off_window_constraints", {}) or {}).get(n, []) or []
                    if any(ws <= d <= we for (ws, we) in _ow_ranges):
                        print(
                            f"[WeekendOff][HardDebug] 평일 OFF 금지 스킵(off_window 월경계 보정): "
                            f"n={n}, nurse_id={getattr(nu, 'nurse_id', '?')}, name={getattr(nu, 'name', '?')}, d={d+1}"
                        )
                        continue
                    wd = (rs.target_month + timedelta(days=d)).weekday()
                    print(
                        f"[WeekendOff][HardDebug] X(n,d,off)==0 추가: "
                        f"n={n}, nurse_id={getattr(nu, 'nurse_id', '?')}, name={getattr(nu, 'name', '?')}, "
                        f"d={d+1}, weekday={wd}(월0~금4)"
                    )
                    m.Add(X(n, d, off) == 0)
        # 월초 OFF 윈도우 (전월 꼬리 연속근무 보정): 지정 구간에 OFF ≥ 1
        # 주말 휴무자도 월경계 연속근무 초과 가능 → 동일 적용
        try:
            off_windows = getattr(rs, "off_window_constraints", {}) or {}
            if off_idx_full is not None:
                _blocked_ow = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                for (w_start, w_end) in off_windows.get(n, []) or []:
                    left = max(T0, w_start)
                    right = min(T1, w_end)
                    if left > right:
                        continue
                    # 유저 고정 우선: 윈도우 내 고정 비-OFF 셀은 제외하고 적용
                    # blocked day도 제외 (X 변수 없음 → sum=0 → INFEASIBLE 방지)
                    free_days = [d for d in range(left, right + 1) if d not in _blocked_ow and not ((n, d) in fixed and fixed[(n, d)] != off_idx_full)]
                    if not free_days:
                        print(f"[CP-SAT-Basic] off_window 무시 (유저 고정 우선): n={n}, window=[{left+1},{right+1}] 전체 고정")
                        continue
                    m.Add(sum(X(n, d, off_idx_full) for d in free_days) >= 1)
        except Exception as e:
            print(f"[CP-SAT-Basic] 월초 OFF 윈도우 적용 실패: n={n}, err={e}")
        # 연속 근무 K+1 중 OFF ≥1 (주말 휴무자 포함: 월경계 연속근무 초과 방지)
        # 고정 셀 우선: D/E/N/O 불문하고 fixed_cells에 포함된 날은 자유 일수에서 제외
        # → 유저가 K+1일 이상 연속 근무를 고정한 경우, 해당 윈도우는 제약 적용하지 않음
        _blocked = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
        for d0 in range(T0, T1-K+1):
            window = [d0 + t for t in range(K + 1)]
            # blocked day가 있으면 해당 일은 근무 불가 → 연속근무 자동 중단 → 스킵
            if any(d in _blocked for d in window):
                continue
            # 고정 OFF가 하나라도 있으면 이미 만족 → 스킵
            if any((n, d) in fixed and fixed[(n, d)] == off for d in window):
                continue
            # 고정 셀(근무/OFF 불문)을 제외한 자유 일수만 대상으로 합산
            free_days_w = [d for d in window if (n, d) not in fixed]
            if not free_days_w:
                # 윈도우 전체가 고정(D/E/N 연속 포함) → 유저 고정 우선, 제약 무시
                continue
            m.Add(sum(X(n, d, off) for d in free_days_w) >= 1)

        # E→D, N→D, N→E
        for d in range(T0+1, T1+1):
            if getattr(cfg, "ban_n_to_d", True):
                # fixed_cells로 N→D가 명시적으로 고정된 경우 제약 면제
                if not (fixed.get((n, d-1)) == night and fixed.get((n, d)) == day):
                    m.Add(X(n,d,day)+X(n,d-1,night)<=1)  # N→D 금지
            if getattr(cfg, "ban_e_to_d", True):
                # fixed_cells로 E→D가 명시적으로 고정된 경우 제약 면제
                if not (fixed.get((n, d-1)) == eve and fixed.get((n, d)) == day):
                    m.Add(X(n,d,day)+X(n,d-1,eve)<=1)   # E→D 금지
            if getattr(cfg, "ban_n_to_e", True):
                # fixed_cells로 N→E가 명시적으로 고정된 경우 제약 면제
                if not (fixed.get((n, d-1)) == night and fixed.get((n, d)) == eve):
                    m.Add(X(n,d,eve)+X(n,d-1,night)<=1) # N→E 금지
            if mid is not None:
                m.Add(X(n, d, mid) <= X(n, d - 1, day) + X(n, d - 1, off))
            # if getattr(cfg, "ban_d_to_n", True):
            #     m.Add(X(n,d,night)+X(n,d-1,day)<=1)

        # Night-전담 (레거시 + 새로운 방식 모두 고려)
        is_n_only = False
        raw = getattr(nu, "is_night_nurse", None)
        allowed = _allowed_shift_codes(raw)
        if allowed:
            is_n_only = allowed == {"N"}
            for d in range(T0, T1 + 1):
                if "D" not in allowed:
                    m.Add(X(n, d, day) == 0)
                if "E" not in allowed:
                    m.Add(X(n, d, eve) == 0)
                if "N" not in allowed:
                    m.Add(X(n, d, night) == 0)
                if mid is not None and "M" not in allowed:
                    m.Add(X(n, d, mid) == 0)

        if n not in n_forbid_n:
            # 1N 금지: N 배정 시 인접일 중 최소 1일은 N 이어야 한다.
            # print(f"[NotOneNight] not_one_night: {bool(getattr(cfg, 'not_one_night', False))}")
            if bool(getattr(cfg, "not_one_night", False)):
                for d in range(T0, T1 + 1):
                    if d == T1:
                        continue
                    if d == 0 and (n, 0) in fixed and fixed[(n, 0)] == night:
                        continue  # 1N day0 N 고정(경계) 시 해당일 1N 제약 스킵
                    if d == 0 and getattr(rs, "prev_month_n_tail_by_idx", {}).get(n, 0) > 0:
                        continue
                    neighbors = []
                    if d - 1 >= T0:
                        neighbors.append(X(n, d - 1, night))
                    if d + 1 <= T1:
                        neighbors.append(X(n, d + 1, night))
                    if not neighbors:
                        continue
                    m.Add(X(n, d, night) <= sum(neighbors))

            # fixed 비근무 직전일 N 금지 (하드 제약)
            if bool(getattr(cfg, "ban_night_before_fixed_off", False)):
                _ban_n_cnt = 0
                for d in range(T0 + 1, T1 + 1):
                    if (n, d) not in fixed:
                        continue
                    if fixed[(n, d)] != off_idx_full:
                        continue  # OFF가 아닌 고정 셀은 대상 아님
                    if (n, d) in fixed_wanted_cells:
                        continue  # fixed_wanted OFF는 BanN 대상 제외
                    _fw_type = fixed_type_by_cell.get((n, d))
                    if _fw_type == "근무":
                        continue
                    prev_d = d - 1
                    if prev_d < T0:
                        continue
                    if blocked_by_nurse and prev_d in blocked_by_nurse.get(n, set()):
                        continue
                    if (n, prev_d) in fixed:
                        continue  # 이미 고정된 셀은 변경 불가
                    m.Add(X(n, prev_d, night) == 0)
                    _ban_n_cnt += 1
                if _ban_n_cnt > 0:
                    print(f"[CP-SAT-Basic] [BanNBeforeFixedOff] nurse_idx={n}: {_ban_n_cnt}건 N 금지")

            # # 주말 휴무자 N 요일 제한: forbid_n 아니고 2N/3N 2O 켜진 경우 2O가 주말에 자연 달성되도록 제한
            # # 2N→2O만: 목금만 N 허용. 3N→2O도 켜진 경우: 수목금 N 허용 (3N 블록이 금요일 끝나면 2O=토일)
            # is_weekend_off = bool(getattr(nu, "is_weekend_off", False))
            # two_n = bool(getattr(cfg, "two_offs_after_two_nig", False))
            # three_n = bool(getattr(cfg, "two_offs_after_three_nig", False))
            # if is_weekend_off and n not in n_forbid_n and (two_n or three_n):
            #     allowed_wd = {2, 3, 4} if three_n else {3, 4}  # 수목금 vs 목금
            #     for d in range(T0, T1 + 1):
            #         if (n, d) in fixed and fixed[(n, d)] == night:
            #             continue
            #         wd = (rs.target_month + timedelta(days=d)).weekday()
            #         if wd not in allowed_wd:
            #             m.Add(X(n, d, night) == 0)

            # 연속 Night
            n_tail = getattr(rs, "prev_month_n_tail_by_idx", {}).get(n, 0)
            _n_offs_after_cnight = (getattr(rs, "prev_month_n_offs_after_by_idx", {}) or {}).get(n, 0)
            # offs_after >= 1 이면 야간 연속이 이미 끊긴 상태 → 월경계 연속N 제약 스킵
            if n_tail > 0 and _n_offs_after_cnight == 0:
                for w in range(1, n_tail + 1):
                    april_window_end = L - w
                    cap = L - w
                    if april_window_end < 0 or cap < 0:
                        continue
                    days_in_window = list(range(T0, min(T0 + april_window_end + 1, T1 + 1)))
                    if days_in_window:
                        m.Add(sum(X(n, d, night) for d in days_in_window) <= cap)
            for d0 in range(T0, T1 - L + 1):
                m.Add(sum(X(n, d0 + t, night) for t in range(L + 1)) <= L)

            # 월 Night 상한 (당월 D_phys만 합산)
            phys_range_night = month_total_day_range(T0, T1, D_phys)
            if phys_range_night:
                m.Add(
                    sum(X(n, d, night) for d in phys_range_night)
                    <= cfg.max_night_shifts_per_month
                )

        # 월 최소/최대 OFF (당월 D_phys만 합산)
        # max coverage가 설정된 경우: min/max coverage 기반으로 OFF cap 자동 조정
        try:
            _off_cap_skip = False
            _auto_min_off = None  # max coverage 기반 자동 최소 OFF
            _auto_max_off = None  # min coverage 기반 자동 최대 OFF
            if _has_any_max:
                _blocked_set = set(blocked_by_nurse.keys()) if blocked_by_nurse else set()
                _total_off_capacity = 0  # min coverage 기준 최대 OFF 가용량
                _total_off_required = 0  # max coverage 기준 최소 OFF 필요량
                max_by_day = getattr(cfg, "daily_shift_requirements_max_by_day", None)
                for _dd in range(D_phys):
                    _day_min_sum = 0
                    _day_max_sum = 0
                    if hasattr(cfg, "daily_shift_requirements_by_day") and isinstance(cfg.daily_shift_requirements_by_day, list) and _dd < len(cfg.daily_shift_requirements_by_day):
                        _day_min_sum = sum(int(v or 0) for v in cfg.daily_shift_requirements_by_day[_dd].values())
                    elif hasattr(cfg, "daily_shift_requirements") and isinstance(cfg.daily_shift_requirements, dict):
                        _day_min_sum = sum(int(v or 0) for v in cfg.daily_shift_requirements.values())
                    if isinstance(max_by_day, list) and _dd < len(max_by_day) and isinstance(max_by_day[_dd], dict):
                        # shift type별: max >= min이면 max 사용, 아니면 min으로 폴백
                        _day_min_map = {}
                        if hasattr(cfg, "daily_shift_requirements_by_day") and isinstance(cfg.daily_shift_requirements_by_day, list) and _dd < len(cfg.daily_shift_requirements_by_day):
                            _day_min_map = cfg.daily_shift_requirements_by_day[_dd]
                        elif hasattr(cfg, "daily_shift_requirements") and isinstance(cfg.daily_shift_requirements, dict):
                            _day_min_map = cfg.daily_shift_requirements
                        _all_codes = set(list(max_by_day[_dd].keys()) + (list(_day_min_map.keys()) if isinstance(_day_min_map, dict) else []))
                        for _code in _all_codes:
                            if _code == 'O':
                                continue
                            _mv = int((max_by_day[_dd].get(_code) or 0))
                            _minv = int((_day_min_map.get(_code) or 0) if isinstance(_day_min_map, dict) else 0)
                            if _mv > 0 and _mv >= _minv:
                                _day_max_sum += _mv
                            else:
                                _day_max_sum += _minv
                    _day_active = sum(
                        1 for nn in range(N)
                        if join[nn] <= _dd <= leave[nn]
                        and _dd not in (blocked_by_nurse.get(nn, set()) if blocked_by_nurse else set())
                    )
                    _total_off_capacity += max(0, _day_active - _day_min_sum)
                    if _day_max_sum > 0:
                        _total_off_required += max(0, _day_active - _day_max_sum)
                _n_full = max(1, sum(1 for nn in range(N) if nn not in _blocked_set))
                _auto_min_off = max(1, int(math.ceil(_total_off_required / _n_full)))
                _auto_max_off = max(_auto_min_off, int(_total_off_capacity / _n_full))
                # 2N2O/3N2O 하드 제약 활성 시 회복 OFF 여유 확보
                if getattr(cfg, "two_offs_after_two_nig", False) or getattr(cfg, "two_offs_after_three_nig", False):
                    _auto_max_off += 2
                # 안전장치: required > capacity이면 auto 값이 비현실적 → 비활성화
                if _total_off_required > _total_off_capacity:
                    print(
                        f"[OffCap][MaxCov] required({_total_off_required}) > capacity({_total_off_capacity})"
                        f" → auto 조정 비활성화 (max coverage 설정 오류 또는 인원 부족)"
                    )
                    _auto_min_off = None
                    _auto_max_off = None
                _configured_off = int(getattr(cfg, "off_days", 9) or 9)
                if n == 0:
                    print(
                        f"[OffCap][MaxCov] 자동 조정: off_capacity={_total_off_capacity}, "
                        f"off_required={_total_off_required}, N={_n_full}, "
                        f"auto_min={_auto_min_off}, auto_max={_auto_max_off}, configured={_configured_off}"
                    )
            if not _off_cap_skip and not bool(getattr(nu, "is_weekend_off", False)):
                phys_range_off = month_total_day_range(T0, T1, D_phys)
                _n_blocked_set = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
                if _n_blocked_set:
                    phys_range_off = [d for d in phys_range_off if d not in _n_blocked_set]
                avail_days = len(phys_range_off) if phys_range_off else 0
                vacation_cnt = sum(1 for d in phys_range_off if (n, d) in vacation_off_cells)
                off_bounds = compute_off_bounds(
                    source=cfg,
                    avail_days=avail_days,
                    vacation_cnt=vacation_cnt,
                    reference_days=D_phys,
                )
                structural_cnt = sum(
                    1
                    for d in phys_range_off
                    if (n, d) in structural_off_cells and (n, d) not in vacation_off_cells
                )
                nonvac_active_days = max(0, avail_days - vacation_cnt)
                min_off_required = int(off_bounds["min_off_required"])
                # max coverage 기반 자동 조정: max_off만 제한, min_off는 기존 비례축소 유지
                # (min_off를 auto_max로 올리면 커버리지 부족 시 INFEASIBLE 발생)
                # 상대 그룹 OFF 차감: 합산 기준 off cap 조정
                _other_offs_map = getattr(rs, "other_group_offs", None) or {}
                _nurse_db_id = str(getattr(nu, "nurse_id", getattr(nu, "db_id", "")))
                _other_offs = (_other_offs_map or {}).get(_nurse_db_id, 0)
                if _other_offs > 0:
                    _full_month_off = int(off_bounds.get("effective_off_days", min_off_required))
                    _adjusted_min = max(0, _full_month_off - _other_offs)
                    _adjusted_min = min(_adjusted_min, nonvac_active_days)
                    print(
                        f"[OffCap][CrossGroup] nurse_idx={n}, id={_nurse_db_id}: "
                        f"full_month_off={_full_month_off}, other_offs={_other_offs}, "
                        f"min_off {min_off_required}→{_adjusted_min}"
                    )
                    min_off_required = _adjusted_min
                if min_off_required > 0 and phys_range_off:
                    m.Add(
                        sum(
                            X(n, d, off)
                            for d in phys_range_off
                            if (n, d) not in vacation_off_cells
                        )
                        >= min_off_required
                    )
                extra_allowed = int(off_bounds["max_extra_off_days"])
                # 상대 그룹 OFF 차감: max_off도 조정
                if _other_offs > 0:
                    _adjusted_max = max(min_off_required, min_off_required + extra_allowed)
                    _adjusted_max = min(_adjusted_max, nonvac_active_days)
                    extra_allowed = max(0, _adjusted_max - min_off_required)
                    print(
                        f"[OffCap][CrossGroup] nurse_idx={n}: max_off adjusted → "
                        f"min={min_off_required}, max={_adjusted_max}, extra={extra_allowed}"
                    )
                if extra_allowed >= 0 and phys_range_off:
                    if is_n_only:
                        max_off_allowed_n_only = min(
                            max(0, avail_days - 15),
                            nonvac_active_days,
                        )
                        m.Add(
                            sum(
                                X(n, d, off)
                                for d in phys_range_off
                                if (n, d) not in vacation_off_cells
                            )
                            <= max_off_allowed_n_only
                        )
                        print(
                            f"[OffCap][Init] nurse_idx={n}, id={getattr(nu, 'nurse_id', '?')}, "
                            f"cap_semantics={off_cap_semantics}, is_n_only=1, vac_cnt={vacation_cnt}, "
                            f"structural_nonvac={structural_cnt}, nonvac_active_days={nonvac_active_days}, "
                            f"min_off={min_off_required}, max_off={max_off_allowed_n_only}"
                        )
                    else:
                        _base_max = int(off_bounds["max_off_allowed"])
                        # 2N2O/3N2O 하드 제약으로 인한 추가 OFF를 OffCap에 반영 (미반영 시 INFEASIBLE)
                        _extra_off = 0
                        if n not in n_forbid_n and (
                            getattr(cfg, "two_offs_after_two_nig", False)
                            or getattr(cfg, "two_offs_after_three_nig", False)
                        ):
                            _extra_off += 2
                        if bool(getattr(nu, "is_weekend_off", False)):
                            _ow_data = (getattr(rs, "off_window_constraints", {}) or {}).get(n, []) or []
                            for (_ws, _we) in _ow_data:
                                _wl = max(T0, _ws)
                                _wr = min(T1, _we)
                                if _wl <= _wr and not any(_d in weekend_days for _d in range(_wl, _wr + 1)):
                                    _extra_off += 1
                        # 4O 월경계 제약으로 월초 OFF 배치 제한된 간호사는 max_off +1 보정
                        if n in _4o_cross_affected:
                            _extra_off += 1
                        max_off_allowed = min(
                            _base_max + _extra_off,
                            nonvac_active_days,
                        )
                        # max coverage 자동 조정: max_off도 auto 값으로 cap
                        if _auto_max_off is not None:
                            _ratio = nonvac_active_days / max(1, D_phys)
                            _scaled_auto_max = max(min_off_required, int(_auto_max_off * _ratio))
                            max_off_allowed = min(max_off_allowed, _scaled_auto_max)
                            max_off_allowed = max(max_off_allowed, min_off_required)
                        m.Add(
                            sum(
                                X(n, d, off)
                                for d in phys_range_off
                                if (n, d) not in vacation_off_cells
                            )
                            <= max_off_allowed
                        )
                        print(
                            f"[OffCap][Init] nurse_idx={n}, id={getattr(nu, 'nurse_id', '?')}, "
                            f"cap_semantics={off_cap_semantics}, vac_cnt={vacation_cnt}, "
                            f"structural_nonvac={structural_cnt}, nonvac_active_days={nonvac_active_days}, "
                            f"min_off={min_off_required}, max_off={max_off_allowed}"
                            + (f", extra_off={_extra_off}" if _extra_off > 0 else "")
                            + (f", auto_max={_auto_max_off}" if _auto_max_off is not None else "")
                        )
        except Exception as exc:
            print(
                f"[OffCap][Init] 적용 실패: nurse_idx={n}, id={getattr(nu, 'nurse_id', '?')}, err={exc}"
            )

        # N2/3→2OFF
        # 주의: "N 2회/3회 후 OFF 2회"는 다음 2일이 모두 OFF여야 한다.
        # 기존 구현은 (sum_n - 1 <= off1 + off2) 형태여서 연속 N일 때 OFF 1개만 허용되는 버그가 있었다.
        if cfg.two_offs_after_three_nig and n not in n_forbid_n:
            n_tail = getattr(rs, "prev_month_n_tail_by_idx", {}).get(n, 0)
            n_offs_after_3n = getattr(rs, "prev_month_n_offs_after_by_idx", {}).get(n, 0)
            _blocked_3n = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
            _3n_rem = max(0, 2 - n_offs_after_3n) if n_tail >= 3 else 2
            if n_tail >= 3 and _3n_rem > 0 and (T0 + 1) <= T1 and T0 not in _blocked_3n and (T0 + 1) not in _blocked_3n:
                end_prev_block = m.NewBoolVar(f"end_3n_prev_{n}")
                m.Add(end_prev_block == X(n, T0, night).Not())
                if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) != off_idx_full for d2 in (T0, T0 + 1)):
                    if _3n_rem >= 2:
                        m.Add(
                            countable_off(n, T0) + countable_off(n, T0 + 1) == 2
                        ).OnlyEnforceIf([end_prev_block])
                    else:
                        m.Add(
                            countable_off(n, T0) + countable_off(n, T0 + 1) >= 1
                        ).OnlyEnforceIf([end_prev_block])
                print(f"[CP-SAT-Basic] [3N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                      f"offs_after={n_offs_after_3n}, rem={_3n_rem}")
            elif n_tail >= 3 and _3n_rem == 0:
                print(f"[CP-SAT-Basic] [3N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                      f"offs_after={n_offs_after_3n} → 전월 내 2OFF 충족, 현월 강제 OFF 스킵")
            if n_tail >= 2 and n_offs_after_3n < 2 and (T0 + 2) <= T1:
                if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) != off_idx_full for d2 in (T0 + 1, T0 + 2)):
                    m.Add(
                        countable_off(n, T0 + 1) + countable_off(n, T0 + 2) == 2
                    ).OnlyEnforceIf([X(n, T0, night)])
            if n_tail == 1 and n_offs_after_3n < 2 and (T0 + 3) <= T1:
                if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) != off_idx_full for d2 in (T0 + 2, T0 + 3)):
                    m.Add(
                        countable_off(n, T0 + 2) + countable_off(n, T0 + 3) == 2
                    ).OnlyEnforceIf([X(n, T0, night), X(n, T0 + 1, night)])
            for d in range(T0 + 2, T1 - 1):
                # (N_d-2 ∧ N_d-1 ∧ N_d) → (O_d+1 + O_d+2 == 2)
                if any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) != off_idx_full for d2 in (d + 1, d + 2)):
                    # 회복 OFF 슬롯에 non-OFF fixed_wanted → 3N 블록 자체를 금지
                    m.Add(
                        X(n, d, night) + X(n, d - 1, night) + X(n, d - 2, night) <= 2
                    )
                    continue
                m.Add(
                    countable_off(n, d + 1) + countable_off(n, d + 2) == 2
                ).OnlyEnforceIf(
                    [X(n, d, night), X(n, d - 1, night), X(n, d - 2, night)]
                )
        if cfg.two_offs_after_two_nig and n not in n_forbid_n and not getattr(cfg, '_2n2off_pre_injected', False):
            n_tail = getattr(rs, "prev_month_n_tail_by_idx", {}).get(n, 0)
            n_offs_after = getattr(rs, "prev_month_n_offs_after_by_idx", {}).get(n, 0)
            _blocked_2n = blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
            # 전월 N tail 뒤 이미 소비된 OFF 수를 반영: req_offs(2) - offs_after 만큼만 현월에서 추가 필요
            _2n_rem = max(0, 2 - n_offs_after) if n_tail >= 2 else 2
            if n_tail >= 2 and _2n_rem > 0 and (T0 + 1) <= T1 and T0 not in _blocked_2n and (T0 + 1) not in _blocked_2n:
                end_prev_block = m.NewBoolVar(f"end_2n_prev_{n}")
                m.Add(end_prev_block == X(n, T0, night).Not())
                if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) != off_idx_full for d2 in (T0, T0 + 1)):
                    if _2n_rem >= 2:
                        m.Add(
                            countable_off(n, T0) + countable_off(n, T0 + 1) == 2
                        ).OnlyEnforceIf([end_prev_block])
                    else:
                        # _2n_rem == 1: 1개만 추가 필요
                        m.Add(
                            countable_off(n, T0) + countable_off(n, T0 + 1) >= 1
                        ).OnlyEnforceIf([end_prev_block])
                print(f"[CP-SAT-Basic] [2N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                      f"offs_after={n_offs_after}, rem={_2n_rem}")
            elif n_tail >= 2 and _2n_rem == 0:
                print(f"[CP-SAT-Basic] [2N2OFF-cross] nurse_idx={n}, n_tail={n_tail}, "
                      f"offs_after={n_offs_after} → 전월 내 2OFF 충족, 현월 강제 OFF 스킵")
            if n_tail >= 1 and n_offs_after < 2 and (T0 + 2) <= T1:
                end_block_b0 = m.NewBoolVar(f'end_2n_main_b0_{n}')
                m.Add(end_block_b0 == X(n, T0 + 1, night).Not())
                if not any((n, d2) in fixed_wanted_cells and fixed.get((n, d2)) != off_idx_full for d2 in (T0 + 1, T0 + 2)):
                    m.Add(
                        countable_off(n, T0 + 1) + countable_off(n, T0 + 2) == 2
                    ).OnlyEnforceIf([X(n, T0, night), end_block_b0])
            for d in range(T0 + 1, T1 - 1):
                # 블록이 2N 이상이고 d가 블록의 끝일 때만 2O 강제 (2N1O 금지, 3N 허용)
                xn_prev = X(n, d - 1, night)
                xn_curr = X(n, d, night)
                xn_next = X(n, d + 1, night)
                end_block = m.NewBoolVar(f'end_2n_main_{n}_{d}')
                m.Add(end_block == xn_next.Not())
                if any(
                    (n, d2) in fixed_wanted_cells
                    and fixed.get((n, d2)) not in (off_idx_full, night, None)
                    for d2 in (d + 1, d + 2)
                ):
                    # 회복 OFF 슬롯에 non-OFF/non-N fixed_wanted → 이 위치에서 2N 블록 종료 금지
                    m.Add(xn_prev + xn_curr + end_block <= 2)
                    continue
                m.Add(
                    countable_off(n, d + 1) + countable_off(n, d + 2) == 2
                ).OnlyEnforceIf(
                    [xn_prev, xn_curr, end_block]
                )

    # ───────────── 3-2. 룩어헤드 전용: 일별 OFF 상한(고정 vs 선택 분리) ───────
    if K_lookahead > 0 and off_idx_full is not None:
        logger.info(
            "[Lookahead] 일별 OFF 상한 제약 추가 (룩어헤드 %s일, d=%s~%s)",
            D - D_phys,
            D_phys,
            D - 1,
        )
        fixed_off_lookahead = {
            (n, d) for (n, d) in structural_off_cells if d >= D_phys
        }
        fixed_off_lookahead = {
            (n, d)
            for (n, d) in fixed_off_lookahead
            if not bool(getattr(rs.nurses[n], "is_weekend_off", False))
        }
        def _get_need_for_day(d_val):
            if d_val >= D_phys and isinstance(next_month_head_req, list) and (d_val - D_phys) < len(next_month_head_req):
                r = next_month_head_req[d_val - D_phys]
                return r if isinstance(r, dict) else (cfg.daily_shift_requirements or {})
            if hasattr(cfg, "daily_shift_requirements_by_day") and isinstance(cfg.daily_shift_requirements_by_day, list) and d_val < len(cfg.daily_shift_requirements_by_day):
                return cfg.daily_shift_requirements_by_day[d_val]
            return cfg.daily_shift_requirements or {}
        add_lookahead_off_cap_constraints(
            m=m,
            X=X,
            N=N,
            D_phys=D_phys,
            D_ext=D,
            join=join,
            leave_ext=leave,
            off_idx=off_idx_full,
            get_need_for_day=_get_need_for_day,
            fixed_off_cells_lookahead=fixed_off_lookahead,
            shift_codes=rs.config.shift_types,
            logger_prefix="[CP-SAT-Basic]",
        )

    # ───────────── 4. Soft (패널티 변수) ───────
    obj = build_main_objective_terms(
        m=m,
        rs=rs,
        X=X,
        join=join,
        leave=leave,
        over_vars_by_day=over_vars_by_day,
        coverage_shortage_vars=coverage_shortage_vars,
        include_pair_objective=include_pair_objective,
        preceptor_terms_fn=_add_preceptor_objective_terms,
        fixed_cnt=fixed_cnt,
        blocked_by_nurse=blocked_by_nurse,
    )
    # M soft min shortage 패널티 (max coverage 없을 때만 활성)
    for m_sh in m_coverage_shortage_vars:
        obj.append(-FALLBACK_COVERAGE_SHORT_WEIGHT * m_sh)
    # 고립 OFF 슬랙 패널티(강제 불가 시에만 허용)
    for slack_var, w in isolated_off_slacks:
        if w > 0:
            obj.append(-w * slack_var)
    # 룩어헤드 OFF 분산(작은 가중치로 당월 품질 우선)
    if K_lookahead > 0 and off_idx_full is not None:
        lookahead_dist_weight = int(getattr(rs.config, "lookahead_distribution_weight", 1) or 1)
        logger.info(
            "[Lookahead] OFF 분산 패널티 추가 weight=%s (룩어헤드 %s일)",
            lookahead_dist_weight,
            D - D_phys,
        )
        obj.extend(
            add_lookahead_distribution_penalty_terms(
                m, X, N, D_phys, D,
                join, leave, off_idx_full,
                {(n, d) for (n, d) in structural_off_cells if d >= D_phys},
                weight=lookahead_dist_weight,
            )
        )
    # max coverage Off 균등 분배 항 추가
    if max_cov_off_equalize_terms:
        obj.extend(max_cov_off_equalize_terms)
    # 일자별 커버리지 균등화 항 추가
    if daily_cov_equalize_terms:
        obj.extend(daily_cov_equalize_terms)
    m.Maximize(sum(obj))

    return m, X, join, leave, fixed


# ─────────────────────────────────────────────────────────────
#           Neighbourhood solver  (전역 변수·제약 그대로)     │
# ─────────────────────────────────────────────────────────────
def _solve_neighbourhood(
    rs,
    n_set,
    d_set,
    tl,
    grouped,
    run_seed: int | None = None,
    it: int = 0,
) -> tuple[bool, str]:
    """선택된 이웃(n_set, d_set)만 재탐색하여 해를 갱신한다."""
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
    st = solver.Solve(model)
    status_text = _cp_sat_status_to_text(st)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if not bool(getattr(rs, "_infeasible_n_diag_logged", False)):
            _log_infeasible_n_capacity(rs, j, l, fixed)
            rs._infeasible_n_diag_logged = True
        return False, status_text

    # 반영
    for n in n_set:
        for d in d_set:
            for s in range(S):
                rs.roster[n,d,s]=1 if solver.Value(X(n,d,s)) else 0
    return True, status_text


def _log_infeasible_n_capacity(rs, join: list[int], leave: list[int], fixed: dict[tuple[int, int], int]) -> None:
    try:
        cfg = rs.config
        if "N" not in cfg.shift_types:
            return
        night_idx = cfg.shift_types.index("N")
        d_phys = rs.num_days
        d_ext = max(leave) + 1 if leave else d_phys
        initial_forbidden = getattr(rs, "initial_forbidden", {})
        if not isinstance(initial_forbidden, dict):
            initial_forbidden = {}

        def need_n(d: int) -> int:
            if (
                d < d_phys
                and hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and d < len(cfg.daily_shift_requirements_by_day)
                and isinstance(cfg.daily_shift_requirements_by_day[d], dict)
            ):
                return int((cfg.daily_shift_requirements_by_day[d] or {}).get("N", 0) or 0)
            if d < d_phys:
                return int((cfg.daily_shift_requirements or {}).get("N", 0) or 0)
            head = getattr(cfg, "next_month_head_requirements", None)
            i = d - d_phys
            if isinstance(head, list) and i < len(head) and isinstance(head[i], dict):
                return int((head[i] or {}).get("N", 0) or 0)
            return int((cfg.daily_shift_requirements or {}).get("N", 0) or 0)

        fixed_n_by_day = [0] * max(0, d_ext)
        for (n, d), s_idx in (fixed or {}).items():
            if 0 <= d < d_ext and s_idx == night_idx:
                fixed_n_by_day[d] += 1

        deficits: list[tuple[int, int, int, int]] = []
        for d in range(d_ext):
            req = max(0, need_n(d) - fixed_n_by_day[d])
            if req <= 0:
                continue
            cap = 0
            for n in range(len(rs.nurses)):
                t0, t1 = join[n], leave[n]
                if d < t0 or d > t1:
                    continue
                fixed_shift = fixed.get((n, d))
                if fixed_shift is not None:
                    if fixed_shift == night_idx:
                        cap += 1
                    continue
                raw = getattr(rs.nurses[n], "is_night_nurse", None)
                if isinstance(raw, list):
                    allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
                    if allowed and "N" not in allowed:
                        continue
                if "N" in initial_forbidden.get((n, d), set()):
                    continue
                cap += 1
            if cap < req:
                deficits.append((d + 1, req, cap, fixed_n_by_day[d]))

        if deficits:
            print(
                "[CP-SAT-Basic][Diag][N-Capacity] 일자별 N 수요 대비 가능 인원 부족 감지: "
                f"count={len(deficits)}"
            )
            for day_1, req, cap, fixed_n in deficits[:12]:
                print(
                    "[CP-SAT-Basic][Diag][N-Capacity] "
                    f"day={day_1}, req_after_fixed={req}, cap={cap}, fixed_n={fixed_n}"
                )
        else:
            print("[CP-SAT-Basic][Diag][N-Capacity] 일자별 N 수요 대비 인원 부족은 없음")
    except Exception as exc:
        print(f"[CP-SAT-Basic][Diag][N-Capacity] 분석 실패: {exc}")


def _log_shift_requirement_gaps(rs) -> None:
    try:
        cfg = rs.config
        shift_types = list(getattr(cfg, "shift_types", []) or [])
        if not shift_types:
            return
        ds_by_day = getattr(cfg, "daily_shift_requirements_by_day", None)
        base_req = getattr(cfg, "daily_shift_requirements", {}) or {}
        gaps: list[tuple[int, str, int, int]] = []
        for d in range(rs.num_days):
            if isinstance(ds_by_day, list) and d < len(ds_by_day) and isinstance(ds_by_day[d], dict):
                need_map = ds_by_day[d]
            else:
                need_map = base_req
            for code, req_val in (need_map or {}).items():
                s_code = str(code or "").strip().upper()
                if s_code not in shift_types:
                    continue
                req = int(req_val or 0)
                if req <= 0:
                    continue
                s_idx = shift_types.index(s_code)
                assigned = int(sum(int(rs.roster[n, d, s_idx]) for n in range(len(rs.nurses))))
                if assigned < req:
                    gaps.append((d + 1, s_code, req, assigned))
        if gaps:
            print(f"[CP-SAT-Basic][Diag][ShiftGap] 부족 셀={len(gaps)}")
            for day_1, code, req, assigned in gaps[:20]:
                print(
                    f"[CP-SAT-Basic][Diag][ShiftGap] day={day_1}, shift={code}, req={req}, assigned={assigned}, shortage={req-assigned}"
                )
        else:
            print("[CP-SAT-Basic][Diag][ShiftGap] shift_requirement 부족 없음")
    except Exception as exc:
        print(f"[CP-SAT-Basic][Diag][ShiftGap] 분석 실패: {exc}")


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


def _log_grade_result(
    rs: RosterSystem,
    nurses,
    grade_config: dict,
    logger_prefix: str = "[CP-SAT-Basic]",
    label: str = "최종",
):
    """Grade 처리 적용 여부를 확인하고, 일자별 D/E/N 필요·확보, Grade별 배치·달성%를 가독성 있게 로그한다."""
    constraints_map = grade_config.get("constraints") or grade_config.get("constraints_json") or {}
    if not constraints_map:
        return

    shift_types = rs.config.shift_types
    cfg = rs.config
    ds_by_day = getattr(cfg, "daily_shift_requirements_by_day", None)
    apply_shifts = ("D", "E", "N")

    # grade 정의역 및 간호사별 grade 매핑 (_dump_grade_summary와 동일)
    _grades = set()
    for _, gmap in constraints_map.items():
        if not isinstance(gmap, dict):
            continue
        for k in gmap:
            try:
                _grades.add(int(k))
            except (TypeError, ValueError):
                continue
    grade_values = sorted(_grades) if _grades else [1, 2, 3]
    null_policy = str(grade_config.get("null_grade_policy") or "LOWEST").upper()
    valid_grades = [getattr(n, "grade", None) for n in nurses if getattr(n, "grade", None) is not None]
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
            h = hashlib.md5(str(getattr(nurse, "db_id", getattr(nurse, "name", ""))).encode()).hexdigest()
            return grade_values[int(h[:8], 16) % len(grade_values)]
        return max(grade_values)

    nurse_grades = [_resolve_grade(n) for n in nurses]

    def _need_for_day(day_idx: int) -> dict:
        if isinstance(ds_by_day, list) and day_idx < len(ds_by_day) and isinstance(ds_by_day[day_idx], dict):
            return ds_by_day[day_idx]
        return getattr(cfg, "daily_shift_requirements", {}) or {}

    # ---------- 1) Grade 처리 적용 확인 ----------
    print(f"{logger_prefix} ========== [Grade] Grade 처리 적용됨 ({label}) ==========")

    # ---------- 2) 일자별 D, E, N 필요 수 / 확보 수 ----------
    print(f"{logger_prefix} --- 일자별 교대(D/E/N) 필요 수 · 확보 수 ---")
    for d in range(rs.num_days):
        need_map = _need_for_day(d)
        parts = []
        for code in apply_shifts:
            if code not in shift_types:
                continue
            s_idx = shift_types.index(code)
            need = int(need_map.get(code, 0) or 0)
            secured = int(sum(1 for n in range(len(nurses)) if int(rs.roster[n, d, s_idx]) == 1))
            parts.append(f"{code} 필요 {need} 확보 {secured}")
        if parts:
            print(f"{logger_prefix}   일자 {d + 1:2d}:  {' | '.join(parts)}")

    # ---------- 3) Grade별 목표·배치·달성% ----------
    total_target_by_grade = {g: 0 for g in grade_values}
    total_assigned_by_grade = {g: 0 for g in grade_values}

    for d in range(rs.num_days):
        need_map = _need_for_day(d)
        for shift_code, base in (constraints_map or {}).items():
            s_code = str(shift_code or "").upper()
            if s_code not in apply_shifts or s_code not in shift_types:
                continue
            req = int(need_map.get(s_code, 0) or 0)
            if req <= 0:
                continue
            base_min = {g: 0 for g in grade_values}
            if isinstance(base, dict):
                for k, v in base.items():
                    try:
                        gi = int(k)
                        if gi in grade_values:
                            base_min[gi] = max(0, int(v or 0))
                    except Exception:
                        pass
            sum_base = sum(base_min.values())
            if sum_base <= 0:
                continue
            s_idx = shift_types.index(s_code)
            for g in grade_values:
                t = (req * base_min.get(g, 0) + sum_base - 1) // sum_base if sum_base else 0
                total_target_by_grade[g] += min(t, req)
            for n_idx in range(len(nurses)):
                if int(rs.roster[n_idx, d, s_idx]) == 1:
                    g = nurse_grades[n_idx]
                    total_assigned_by_grade[g] = total_assigned_by_grade.get(g, 0) + 1

    print(f"{logger_prefix} --- Grade별 목표 대비 배치 현황 ---")
    for g in sorted(grade_values):
        target = total_target_by_grade.get(g, 0)
        assigned = total_assigned_by_grade.get(g, 0)
        pct = (100.0 * assigned / target) if target > 0 else (100.0 if assigned == 0 else 0)
        print(f"{logger_prefix}   Grade {g}: 목표 {target}명, 배치 {assigned}명 → 달성 {pct:.1f}%")
    print(f"{logger_prefix} ========================================")


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
