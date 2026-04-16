"""하드코딩된 가중치/점수 상수 모음.

이 모듈은 기존 로직의 수치(가중치/패널티/보너스)를 그대로 보존하면서,
어느 값이 하드코딩인지 명확히 표시하기 위해 분리했다.
"""

# 선호도 점수 스케일링 (preference_matrix × scale)
PREFERENCE_SCORE_SCALE = 100

# N 전담 간호사에게 N 근무 배정 시 추가 보너스
N_ONLY_NIGHT_BONUS = 500

# 커버리지 부족 최소화 단계(폴백 stage1)에서 shortage 가중치
FALLBACK_COVERAGE_SHORT_WEIGHT = 100000

# 경력자 부족 패널티(폴백 stage3)
FALLBACK_EXPERIENCE_SHORT_PENALTY = 100

# 경력자 부족 패널티(메인 모델)
EXPERIENCE_SHORT_PENALTY = 200

# 주 2OFF 부족 패널티(메인 모델)
WEEK_OFF_SHORT_PENALTY = 300

# D/E/N 균등화 편차 패널티
NIGHT_DEVIATION_PENALTY = 50000

# N-O-D/E 패턴 위반 패널티
NOD_NOE_PENALTY = 300

# 고립 OFF 패널티
ISOLATED_OFF_PENALTY = 100

# 2N 블록 패널티 (3N 유도): 2N2O+3N2O 동시 활성 시 3N 블록 선호
PREFER_3N_BLOCK_PENALTY = 80

# 팀 균형(TEAM 전략) 후처리 보너스 배수
TEAM_BALANCE_POSTPROCESS_BONUS = 0.2

# GRADE 전략 후처리 보너스
GRADE_POSTPROCESS_BONUS = 0.5


