"""축소 CP-SAT 모델 제약 family 라이브러리.

50케이스(docs/CONSTRAINT_TESTCASE_MATRIX_SPEC.md)의 다중제약 충돌을 작은 모델로
재현하기 위한 wrap된 하드제약 빌더 모음. 각 빌더는 add_hard 로 assumption 화하므로
find_mcs/MUS 가 관측 가능. 한 family 라도 충돌에 관여하면 MCS 가 그 수선점을 짚는다.
"""

from __future__ import annotations

from ortools.sat.python import cp_model

from services.cp_sat.hard_assumption import add_hard

SHIFTS = ["D", "E", "N", "O"]
WORK = ["D", "E", "N"]


class Reduced:
    """작은 근무표 모델 + family 빌더."""

    def __init__(self, nurses=6, days=4, grade=None, team=None):
        self.N, self.D = nurses, days
        self.grade = grade or {n: (1, 2, 3)[n % 3] for n in range(nurses)}
        self.team = team or {n: str(n % 2 + 1) for n in range(nurses)}
        self.m = cp_model.CpModel()
        from services.cp_sat.hard_assumption import HardAssumptionRegistry
        self.reg = HardAssumptionRegistry(self.m)
        self.x = {(n, d, s): self.m.NewBoolVar(f"x_{n}_{d}_{s}")
                  for n in range(nurses) for d in range(days) for s in SHIFTS}
        for n in range(nurses):
            for d in range(days):
                self.m.Add(sum(self.x[(n, d, s)] for s in SHIFTS) == 1)

    def X(self, n, d, s):
        return self.x[(n, d, s)]

    def _add(self, name, expr, family, label, pattern):
        add_hard(self.m, self.reg, name=name, constraint_expr=expr,
                 meta={"node_id": name, "type": f"{family}Node", "pattern": pattern,
                       "label": label, "family": family})

    # ── coverage ──
    def coverage_min(self, s, k):
        for d in range(self.D):
            self._add(f"CoverageMin:{s}:d{d}", sum(self.X(n, d, s) for n in range(self.N)) >= k,
                      "CoverageMin", f"{s} 최소 {k}", "coverage")

    # ── transition ban N->D ──
    def transition_ban(self):
        for n in range(self.N):
            for d in range(1, self.D):
                self._add(f"TransitionBanN2D:n{n}:d{d}",
                          self.X(n, d, "D") + self.X(n, d - 1, "N") <= 1,
                          "BoundaryTransitionBan", f"전이금지 N→D n{n}d{d}", "transition_ban")

    # ── grade ──
    def grade_min(self, s, g, k):
        self._add(f"GradeMin:{s}:g{g}",
                  sum(self.X(n, d, s) for n in range(self.N) for d in range(self.D) if self.grade[n] == g) >= k * self.D,
                  "GradeMin", f"{s} 등급{g} 최소 {k}/일", "grade_min")

    def grade_max(self, s, g, k):
        self._add(f"GradeMax:{s}:g{g}",
                  sum(self.X(n, d, s) for n in range(self.N) for d in range(self.D) if self.grade[n] == g) <= k * self.D,
                  "GradeMax", f"{s} 등급{g} 상한 {k}/일", "grade_max")

    # ── team ──
    def team_min(self, t, s, k):
        for d in range(self.D):
            self._add(f"TeamMin:{t}:{s}:d{d}",
                      sum(self.X(n, d, s) for n in range(self.N) if self.team[n] == t) >= k,
                      "TeamMin", f"팀{t} {s} 최소 {k}", "team_min")

    # ── consecutive work limit ──
    def consecutive_work(self, limit):
        for n in range(self.N):
            for d in range(self.D - limit):
                window = range(d, d + limit + 1)
                self._add(f"ConsecWork:n{n}:d{d}",
                          sum(self.X(n, dd, "O") for dd in window) >= 1,
                          "ConsecutiveWorkLimit", f"연속근무≤{limit} n{n}d{d}", "consecutive_work")

    # ── monthly night cap (per nurse) ──
    def monthly_night_cap(self, cap):
        for n in range(self.N):
            self._add(f"MonthlyNightCap:n{n}",
                      sum(self.X(n, d, "N") for d in range(self.D)) <= cap,
                      "MonthlyNightCap", f"월N≤{cap} n{n}", "monthly_night_cap")

    # ── not one night (단일 N 금지) ──
    def not_one_night(self):
        for n in range(self.N):
            for d in range(1, self.D - 1):
                # N 이면 인접일 중 하나도 N 이어야 (단일 N 금지)
                self._add(f"NotOneNight:n{n}:d{d}",
                          self.X(n, d, "N") <= self.X(n, d - 1, "N") + self.X(n, d + 1, "N"),
                          "NotOneNight", f"단일N금지 n{n}d{d}", "not_one_night")

    # ── night recovery 2N->2O ──
    def night_recovery(self):
        for n in range(self.N):
            for d in range(self.D - 2):
                # 2N 연속이면 다음날 O
                self._add(f"NightRecovery2N2O:n{n}:d{d}",
                          self.X(n, d + 2, "O") >= self.X(n, d, "N") + self.X(n, d + 1, "N") - 1,
                          "NightRecovery", f"2N후 O n{n}d{d}", "night_recovery")

    # ── allowed shift mask (특정 간호사 N 전담) ──
    def allowed_mask(self, nurse, allowed):
        for d in range(self.D):
            for s in SHIFTS:
                if s not in allowed and s != "O":
                    self._add(f"AllowedMask:n{nurse}:{s}:d{d}", self.X(nurse, d, s) == 0,
                              "AllowedShiftMask", f"n{nurse} {s}불가", "allowed_shift_mask")

    # ── off cap (최소 OFF 수) ──
    def off_cap(self, nurse, min_off):
        self._add(f"OffCap:n{nurse}",
                  sum(self.X(nurse, d, "O") for d in range(self.D)) >= min_off,
                  "OffCap", f"n{nurse} OFF≥{min_off}", "off_cap")

    # ── weekend off only (주중 OFF 금지로 평일 공급 압박; 여기선 특정 nurse 가 평일만 O) ──
    def weekend_off_only(self, nurse):
        for d in range(self.D):
            self._add(f"WeekendOffOnly:n{nurse}:d{d}", self.X(nurse, d, "O") == 0,
                      "WeekendOffOnly", f"n{nurse} 평일 O금지", "weekend_off_only")

    # ── fixed cell ──
    def fixed(self, nurse, day, shift):
        self.m.Add(self.X(nurse, day, shift) == 1)   # 도메인 고정(assumption 아님)

    def finalize(self):
        self.reg.attach_to_model()
        return self.m, self.reg
