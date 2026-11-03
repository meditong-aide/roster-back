"""후보(없으면 지금 근무표)가 규칙을 지키는지 — 묻는 말에 답할 재료(사용자 지시 11-03: 후보를 본 뒤 '그레이드는 다 지켜져?').

후보는 규칙을 지키도록 찾지만, 지금 근무표에서 이미 어긋나 있던 곳은 '더 나빠지지 않게'만 지킨다(`solver` 의 상대 상한).
그래서 '모두 지킴'이라고 뭉뚱그리지 않고 셋으로 나눈다 — 이 안에서 새로 어기는 곳(고른 설정·그날 인원 요청으로 넘긴 것) ·
원래 어긋나 있던 곳 · 이 안으로 풀리는 곳 수.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from services.schedule_edit.context import EditContext
from services.schedule_edit.explain import Bend, bend_text
from services.schedule_edit.requests import EditRequest
from services.schedule_edit.rules import WISH, RuleSet, build_rules, measure
from services.schedule_edit.solver import Candidate, before_after

#: 묻는 주제 → (답에 쓸 이름, 규칙 종류). 이름은 화면 설정 용어로
TOPICS: Dict[str, Tuple[str, FrozenSet[str]]] = {
    "grade": ("등급별 최소 인원", frozenset({"등급최소"})),
    "coverage": ("최소·최대 근무 인원", frozenset({"인원최소", "인원최대"})),
    "team": ("팀별 최소 인원", frozenset({"팀최소"})),
    "consecutive": ("연속 근무 규칙", frozenset({"연속근무", "같은근무연속", "고립근무"})),
    "night": ("나이트 규칙", frozenset({"연속나이트", "3N후OFF", "2N후OFF", "2N후OFF(화면)", "1N", "N간격", "월나이트",
                                    "휴무전날N"})),
    "transition": ("근무 전환 금지", frozenset({"전환금지"})),
    "off": ("OFF 수·월 근무 한도", frozenset({"OFF수", "개인한도"})),
    "shift_rule": ("허용 근무형·기피 근무·주말휴무", frozenset({"금지근무", "주말휴무"})),
    "mutex": ("상호배제", frozenset({"상호배제"})),
    "wanted": ("반영된 원티드", frozenset({WISH})),
}
ALL = "all"
ALL_LABEL = "규칙(하드락·병동 설정)"


@dataclass
class TopicCheck:
    """후보 하나(rank=None 이면 지금 근무표)의 한 주제 점검."""

    rank: Optional[int]
    #: 이 안에서 새로 어기거나 더 나빠지는 곳(넘김 문장)
    new: List[str] = field(default_factory=list)
    #: 원래 어긋나 있고 그대로인 곳(상태 문장)
    kept: List[str] = field(default_factory=list)
    #: 이 안으로 풀리거나 줄어드는 곳 수
    eased: int = 0


def topic_of(topic: Optional[str]) -> Tuple[str, FrozenSet[str]]:
    if topic in TOPICS:
        return TOPICS[topic]
    return ALL_LABEL, frozenset().union(*(fams for _label, fams in TOPICS.values()))


def configured(rules: RuleSet, families: FrozenSet[str]) -> bool:
    """이 병동에 그 규칙이 하나라도 걸려 있나 — 없으면 '지킨다'가 아니라 '설정이 없다'고 답한다."""
    return any(i.key[0] in families for i in rules.ineqs) or any(c.key[0] in families for c in rules.covers)


def check_current(ctx: EditContext, rules: Optional[RuleSet], families: FrozenSet[str]) -> TopicCheck:
    rules = rules or build_rules(ctx)
    now = _only(measure(ctx, ctx.main, rules), families)
    return TopicCheck(None, kept=[_text(ctx, k, v, state=True) for k, v in _ordered(now)])


def check_candidate(ctx: EditContext, rules: Optional[RuleSet], candidate: Candidate,
                    requests: Sequence[EditRequest], families: FrozenSet[str]) -> TopicCheck:
    _rules, before, after = before_after(ctx, rules, candidate, list(requests))
    before, after = _only(before, families), _only(after, families)
    worse = {k: v - before.get(k, 0) for k, v in after.items() if v > before.get(k, 0)}
    kept = {k: v for k, v in after.items() if k not in worse}
    return TopicCheck(candidate.rank,
                      new=[_text(ctx, k, v) for k, v in _ordered(worse)],
                      kept=[_text(ctx, k, v, state=True) for k, v in _ordered(kept)],
                      eased=sum(1 for k, v in before.items() if after.get(k, 0) < v))


def _only(found: Dict[Tuple, int], families: FrozenSet[str]) -> Dict[Tuple, int]:
    return {k: v for k, v in found.items() if k[0] in families}


def _ordered(found: Dict[Tuple, int]) -> List[Tuple[Tuple, int]]:
    """날짜순(월 단위 규칙은 맨 뒤) — 사람이 표에서 따라가기 쉽게."""
    return sorted(found.items(), key=lambda kv: (kv[0][2] if isinstance(kv[0][2], int) else 99, str(kv[0])))


def _text(ctx: EditContext, key: Tuple, amount: int, *, state: bool = False) -> str:
    return bend_text(Bend(key[0], key, amount), ctx.names, state=state)
