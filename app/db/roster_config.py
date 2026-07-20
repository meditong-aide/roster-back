from dataclasses import dataclass, field
from typing import Dict, List, Optional
import numpy as np

@dataclass
class NurseRosterConfig:
    """간호사 근무표 시스템 설정."""
    # 교대 근무 요구사항
    daily_shift_requirements: Dict[str, int] = None  # {'D': 3, 'E': 3, 'N': 2}
    
    # 경력 제약 조건
    min_experience_per_shift: int = 3  # 교대당 필요한 최소 경력 연수
    required_experienced_nurses: int = 1  # 교대당 필요한 경력 간호사 수
    
    # 야간 근무 제약 조건
    max_night_shifts_per_month: int = 15  # 월별 최대 야간 근무 수
    max_consecutive_nights: int = 3  # 최대 연속 야간 근무 수
    not_one_night: bool = False  # 야간 단발성(1N) 금지 여부
    two_offs_after_three_nig: bool = False  # N 3회 후 OFF 2회 연속 필수
    two_offs_after_two_nig: bool = False  # N 2회 후 OFF 2회 연속 필수
    
    # 근무 패턴 제약 조건
    max_consecutive_work_days: int = 6  # 최대 연속 근무일 수
    enforce_two_offs_per_week: bool = False  # 주당 2일 휴무 적용 여부
    banned_day_after_eve: bool = True  # E → D 근무 금지 (법규)
    # 야간 전이 금지(기본 True). cp_sat_basic/fallback_lex 가 config_data/getattr 로 읽음.
    # dataclass 필드로 선언해 apply-resolution config_override 화이트리스트에 포함(비-DB-컬럼).
    ban_n_to_d: bool = True  # N → D 전이 금지
    ban_n_to_e: bool = True  # N → E 전이 금지
    
    # 병원 내규 (소프트 제약)
    sequential_offs: bool = True  # OFF 연속 배정
    even_nights: bool = True  # N 개수 균등 배정
    nod_noe: bool = True  # N-O-D/E 패턴 최소화 적용 여부
    
    # 휴무일 관리
    global_monthly_off_days: int = 3  # 모든 간호사에게 적용되는 전체 휴무일(공휴일, 특별 휴무일)
    standard_personal_off_days: int = 8  # 간호사별 표준 개인 휴무일 수
    max_extra_off_days: int = 3  # 월 최소 휴무 기준 대비 허용되는 추가 OFF 상한(n)
    extra_off_penalty_weight: int = 80  # 추가 OFF(여유 OFF)를 기피하는 목적함수 패널티 가중치
    # GRADE/coverage 충족을 위해 per-nurse OFF cap을 추가로 풀어주는 여유 일수.
    # 솔버는 extra_off_penalty_weight 때문에 *필요할 때만* 이 여유를 사용함.
    # 후처리(OffSwap, off_swap_enabled=True)가 baseline=off_days 초과분을 연차로 라벨링하므로
    # 사실상 "연차로 흡수 가능한 추가 OFF" 역할. 기본 0(기존 동작 유지).
    off_cap_relax_extra: int = 0
    # 월 합계 GRADE hard constraint (soft + 10× weight로 사실상 hard).
    # per-day는 allow_soft_fallback=True의 soft penalty 유지(KLD 자유도 보존),
    # 월 누계는 grade demand×D를 거의 강제 → grade 비율 보장.
    # 시화병원처럼 인원 vs demand가 빡센 케이스에서 GRADE 트레이드오프 곡선 확장.
    grade_monthly_hard: bool = True
    # Layer 2 total work target에 grade demand 비례 bias.
    # nurse target_work = baseline + α × (grade_natural_work - baseline)
    # α=0: 기존 동작 (모든 nurse가 baseline_work_target 동일)
    # α=0.5: 잉여 grade는 약하게 work 낮춤, 부족 grade는 약하게 work 높임
    # 폐기된 grade-aware target과 달리 α로 강도 조절 → OFF 양극화 완화
    grade_target_bias_alpha: float = 0.0
    # α 자동 산출 (auto=True): shortage/surplus 비율로 매 solve마다 자동 결정.
    # 인원 vs demand가 빡센 케이스: α↑ (잉여 grade가 양보), 여유 케이스: α=0 (bias 비활성).
    # pre-solve feasibility 진단도 함께 수행 (demand > capacity 시 경고 log).
    # 시화병원 검증 결과 monthly_hard와 결합 시 N/D-E 양극화 부작용 발생.
    # 기본 비활성. 필요 시 케이스별로 ON 가능.
    grade_target_bias_alpha_auto: bool = False
    # Layer 1.5: per-nurse |D-E|, |E-N|, |D-N| balance 직접 minimize (X축 균등).
    # 기존 KLD는 shift별 따로 균등화 → 한 nurse의 D=10/E=0 같은 양극화 약하게만 페널티.
    # 이 항은 같은 사람 내 시프트 비대칭을 직접 0에 끌어당김. 전담자(allowed shift 1개)는 제외.
    # 0: 비활성, 30000~90000: 적정. default는 KLD per-shift weight 수준.
    kld_balance_weight: int = 0  # X-ratio cap이 X축을 직접 모델링하므로 보조항 비활성
    # Per-nurse 시프트 상한 soft cap (자동 산출, hardcode 없음):
    # Y축: N count ≤ N_target × ratio (예: 6.43 × 1.5 = 9.6 → floor 9)
    # X축: max(D,E) ≤ ratio × min(D,E)
    # 0이면 비활성. 1.5이면 사용자 목표.
    shift_ratio_cap: float = 1.5
    shift_cap_weight: int = 500000
    soft_max_consecutive_work_days: Optional[int] = None  # 소프트 연속근무 상한(없으면 hard와 동일)
    soft_consecutive_work_penalty_weight: int = 180  # 소프트 연속근무 위반 패널티 가중치
    # 연속 OFF 최대 개수(max_conseq_off). 기본 3 = 4연속+ OFF만 벌점, 3 이하는 전부 무차별.
    # (k+1)연속 OFF마다 고weight 벌점 → hard처럼 억제하되, OFF 과잉 등으로 불가피하면
    # 양보한다(soft라 절대 infeasible 유발 안 함). ORM 컬럼 max_conseq_off 와 동명.
    max_conseq_off: int = 3
    max_conseq_off_penalty_weight: int = 5000
    # 같은 시프트(D/E/N) 연속 ≤3 soft. True면 4연속 같은 시프트 (예: D D D D)에 패널티.
    max_same_shift: bool = True
    max_same_shift_penalty_weight: int = 10000
    # 4O 연속 휴무 hard 제약(당월 내 + 월경계). True면 4연속 OFF 금지. False면 제약 제거.
    # 디폴트는 False(해제): 운영 검증 결과 자연 발생률 ~10-13%로 무리 없는 수준.
    enforce_4o_hard: bool = False
    # N 블록 종료 → 다음 N 블록 시작 사이 간격 soft. 대칭 페널티(많아도 적어도). 동일 블록 내부는 제외.
    # 목표 10일에서 멀어질수록 |gap - target| × weight 누적. 낮은 weight로 약하게 유도.
    n_to_n_interval_target: int = 10
    n_to_n_interval_penalty_weight: int = 50
    n_to_n_interval_max_window: int = 15
    # D/E(데이·이브닝) per-nurse 균등 — fallback stage2 lex 5-pass.
    # OFF/N/n2n 동결 후 nurse별 |D-E| 의 tolerance 초과분만 최소화(완전 동일이 아닌 밴드).
    # 수렴된 균등해가 stage3 warm-start hint로 상속되어 X축(D/E 1.5배) 균형을 안정화한다.
    de_balance_enable: bool = True
    de_balance_tolerance: int = 2  # |D-E| 허용 밴드(일). 초과분만 soft 벌점.
    off_placement_mode: int = 1  # 주휴 인접 OFF 배치 모드(0=미적용, 1=앞/뒤, 2=앞 우선)
    off_first: bool = False  # off_first=False(default): OFF 규칙 우선(OFF cap 충족 + max coverage 무시 + 남는 셀 근무 배정 + fixed_wanted 차감) / off_first=True: 근무 규칙 우선(현행 min-max coverage 균등화 유지, min 근접 배정)
    distribution_mode: str = "hybrid"  # auto|hybrid|balanced|preference|off
    monthly_preference_weight: int = 60  # 월단위 선호(개인 입력) 보너스 가중치(soft)
    enforce_clustered_offs: bool = False  # 고립 OFF 금지 하드 제약 사용 여부
    isolated_off_slack_penalty: int = 300000  # 고립 OFF 허용 슬랙 패널티 가중치
    
    # 교대 배정 비율 - 각 교대 유형에 대한 선호도 가중치 제어
    day_shift_ratio: float = 1.0  # 주간 근무 비율
    evening_shift_ratio: float = 1.0  # 저녁 근무 비율
    night_shift_ratio: float = 1.0  # 야간 근무 비율
    off_shift_ratio: float = 1.2  # 휴무일 비율 (높을수록 휴무일 선호도 증가)
    
    # 선호도 행렬 가중치
    night_nurse_weight: float = 2.0  # 야간 간호사의 야간 근무 가중치
    experience_weight: float = 1.5  # 경력 간호사 가중치
    consecutive_shift_penalty: float = -1.0  # 원치 않는 연속 근무에 대한 패널티
    
    # 선호도 가중치
    shift_preference_weights: Dict[str, float] = field(default_factory=lambda: {
        'D': 5.0,  # 주간 근무 선호도 가중치
        'E': 5.0,  # 저녁 근무 선호도 가중치
        'N': 5.0,  # 야간 근무 선호도 가중치
        'OFF': 10.0  # 휴무 선호도 가중치
    })
    
    # 페어링 가중치
    pair_preference_weight: float = 3.0  # 페어링 선호도 반영 가중치
    # ── 프리셉터(페어) 보너스 항 제어 파라미터 ──
    preceptor_enable: bool = True                   # 프리셉터 보너스 항 사용 여부
    preceptor_strength_multiplier: float = 1.5      # 보너스 항 강도 배수
    preceptor_top_days: int = 30                    # 쌍별 상위 일수 K
    preceptor_min_pair_weight: float = 5.0          # 쌍 가중치 하한 필터
    preceptor_focus_shifts: Optional[List[str]] = None  # 특정 교대만 고려(e.g., ['N','E'])
    # ── 프리셉티 팔로우/커버리지 제어 ──
    preceptee_on: bool = False                          # 프리셉티 팔로우 모드 (ON 시 프리셉티는 프리셉터 근무 따라감)
    preceptee_shift_count: bool = True                  # 프리셉티 커버리지 포함 여부 (ON: DEN에 포함, OFF: DEN에서 제외, preceptee_on=True일 때만 유효)
    # ── 미드(M) 시프트 사용 여부 ──
    use_mid: bool = False                               # True 시 DENO → DENMO 커버리지 전환

    # --- 신규 Hard Constraint 제어 파라미터 ---
    enforce_seniority_pairing: bool = True # 시니어-주니어 동반 근무 규칙 강제 여부
    ban_night_before_fixed_off: bool = True  # fixed_wanted 비근무(휴무/휴가/공가 등) 직전일 N 배치 금지
    junior_pairing_max_experience: int = 2 # 주니어로 간주할 최대 연차
    senior_pairing_min_experience: int = 6 # 시니어로 간주할 최소 연차
    enforce_E_after_D_constraint: bool = True # E -> D 근무 금지 규칙 강제 여부
    
    # 소프트맥스 샘플링 온도
    sampling_temperature: float = 2.0
    
    # 근무 요구사항 우선순위 (0~1) - 1에 가까울수록 더 강하게 근무 요구사항 강제
    shift_requirement_priority: float = 0.8  # 근무 요구사항 우선순위
    
    # 주말 휴무 제약: is_weekend_off=True인 간호사가 주말에만 휴무를 받도록 강제(평일 O 금지)
    weekend_off_only_enable: bool = True  # 주말 휴무 제약 활성화 여부(기본 True)
    
    # --- Oversupply(여유 인원) 균등화 제어 ---
    oversupply_equalize_enable: bool = True  # 일별 D/E/N 초과 인원(L1) 균등화 활성화
    oversupply_equalize_weight: int = 120    # L1 차이 패널티 가중치(클수록 균등화 강함)
    # --- 팀 균등/집중 분배 옵션 ---
    team_balance_enable: bool = False               # 팀 보너스 활성화
    team_balance_gauge: int = 0                     # 0~10 게이지
    team_balance_weight: int = 0                    # 계산된 기본 가중치
    team_balance_top_days: int = 0                  # 동일 교대 보너스에서 고려할 상위 일수
    team_balance_focus_shifts: Optional[List[str]] = None  # 교대 제한 (없으면 D/E/N)
    team_balance_mode: str = "balanced"             # balanced | focus_D | focus_DE
    team_balance_shift_weights: Dict[str, float] = field(default_factory=dict)  # 모드별 파생 가중치
    # ── 팀별 일일 최소 시프트 커버리지 ──
    # team_min_by_team[team_id_str] = {'D': 1, 'E': 1, 'N': 0}
    # 팀마다 개별 min 설정 가능. use_mid=True 일 때 'M' 키 사용 가능.
    # 팀이 맵에 없거나 값이 {} 이면 "제약 없음".
    team_min_by_team: Dict[str, Dict[str, int]] = field(default_factory=dict)
    team_min_soft_fallback: bool = True  # True면 슬랙 + 패널티로 소프트 처리, False면 하드
    team_min_penalty_weight: int = 80000   # 소프트 모드 팀 미달 슬랙 패널티. grade(160000)의 절반 = 한 단계 아래
    # ── 팀 내 인계 제한(handoff restrictions) ──
    # team_handoff_policy_by_team[team_id_str] = {
    #   "restrictions": [
    #     {"grades":[..], "block_same_shift":bool, "block_adjacent":bool},
    #     # future: {"from":[..], "to":[..], "bidirectional":bool, ...}
    #   ]
    # }
    team_handoff_policy_by_team: Dict[str, Dict] = field(default_factory=dict)
    team_handoff_soft_fallback: bool = True
    team_handoff_penalty_weight: int = 80000  # grade(160000) 아래, team_min(80000)과 동급
    def __post_init__(self):
        if self.daily_shift_requirements is None:
            self.daily_shift_requirements = {'D': 3, 'E': 3, 'N': 2}
        if self.soft_max_consecutive_work_days is None:
            self.soft_max_consecutive_work_days = int(self.max_consecutive_work_days)
        # 팀 게이지 → 가중치/탑K
        gauge = max(0, min(10, int(self.team_balance_gauge or 0)))
        if not self.team_balance_enable or gauge == 0:
            self.team_balance_weight = 0
            self.team_balance_top_days = 0
        else:
            if not self.team_balance_weight:
                # 정규화된 팀 보너스 강도(soft) 매핑:
                # weight는 개인 선호도 항의 계수(P*100) 스케일을 기준으로 "대략 0~240" 범위에서 동작하도록 캡을 둔다.
                # 식: weight = round(cap * (g/10)^p)
                # 예) cap=240, p=1.7, g=5 → 약 74, g=10 → 240
                cap = 240
                power = 1.7
                g_norm = gauge / 10.0
                self.team_balance_weight = int(round(cap * (g_norm ** power)))
            if not self.team_balance_top_days:
                self.team_balance_top_days = int(6 + (30 - 6) * (gauge / 10.0))
        # 모드 기반 shift weight 설정 (없을 때만 세팅)
        if not self.team_balance_shift_weights:
            mode = (self.team_balance_mode or "balanced").lower()
            if mode == "focus_d":
                self.team_balance_shift_weights = {"D": 1.5, "E": 0.6, "N": 0.3}
            elif mode == "focus_de":
                self.team_balance_shift_weights = {"D": 1.2, "E": 1.2, "N": 0.5}
            else:
                self.team_balance_shift_weights = {"D": 1.0, "E": 1.0, "N": 1.0}
            
    @property
    def shift_types(self) -> List[str]:
        """휴무일을 포함한 교대 유형 목록을 반환합니다."""
        return list(self.daily_shift_requirements.keys()) + ['O']
        
    @property
    def num_shifts(self) -> int:
        """휴무일을 포함한 교대 유형 수를 반환합니다."""
        return len(self.shift_types)
        
    def calculate_total_off_days(self, personal_off_adjustment: int = 0) -> int:
        """전체 및 개인 할당량을 기반으로 간호사의 총 휴무일을 계산합니다.
        
        Args:
            personal_off_adjustment: 표준 개인 휴무일에 대한 조정(양수 또는 음수 가능)
            
        Returns:
            월별 할당된 총 휴무일 수
        """
        return self.global_monthly_off_days + self.standard_personal_off_days + personal_off_adjustment

# 기본 설정
DEFAULT_CONFIG = NurseRosterConfig() 