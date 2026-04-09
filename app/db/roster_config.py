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
    
    # 병원 내규 (소프트 제약)
    sequential_offs: bool = True  # OFF 연속 배정
    even_nights: bool = True  # N 개수 균등 배정
    nod_noe: bool = True  # N-O-D/E 패턴 최소화 적용 여부
    
    # 휴무일 관리
    global_monthly_off_days: int = 3  # 모든 간호사에게 적용되는 전체 휴무일(공휴일, 특별 휴무일)
    standard_personal_off_days: int = 8  # 간호사별 표준 개인 휴무일 수
    max_extra_off_days: int = 3  # 월 최소 휴무 기준 대비 허용되는 추가 OFF 상한(n)
    extra_off_penalty_weight: int = 80  # 추가 OFF(여유 OFF)를 기피하는 목적함수 패널티 가중치
    soft_max_consecutive_work_days: Optional[int] = None  # 소프트 연속근무 상한(없으면 hard와 동일)
    soft_consecutive_work_penalty_weight: int = 180  # 소프트 연속근무 위반 패널티 가중치
    off_placement_mode: int = 1  # 주휴 인접 OFF 배치 모드(0=미적용, 1=앞/뒤, 2=앞 우선)
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
    # ── max coverage 모드 ──
    use_max_coverage: bool = False  # True 시 daily coverage를 상한(max)으로 적용, 초과 배정 불가, 잔여 인원 Off
    
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