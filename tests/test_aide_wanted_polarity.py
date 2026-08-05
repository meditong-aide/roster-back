"""AIDE 원티드 자연어 분석 평가셋 — 선호(want)와 기피(avoid)를 제대로 가르는가.

## 왜 이 파일이 있나

프롬프트는 "한 번 돌아간다" 로 끝나지 않는다. 이 그래프는 과거에 두 번 무너졌다.

1. `Except` 카테고리를 어휘("말고/빼고/제외/안 돼")로 판정하다가, 한국어 완곡 부정이
   열린 집합이라 `"10일 나이트는 좀 부담스러워요"` 가 **희망으로 뒤집혔다.**
   기피는 하드 제약이라 근무표가 통째로 뒤집힌다.
2. `"가능하면 15일 나이트는 피했으면 좋겠어요"` 는 **통째로 소멸**했다.
3. `"주말 빼고 평일에 D 위주로"` 의 "주말 빼고"(범위 축소)를 회피로 읽어, 간호사가
   말한 적 없는 **주말 10일 O 금지**를 만들었다. 기피는 하드라 그대로 근무표에 걸린다
   (생성 실측으로 확인: 대조군 25% → 금지 적용 0%).

고친 방식은 어휘 매칭을 버리고 `polarity` 를 독립 축으로 둔 것이다. 그 회귀를
잡아 둘 평가셋이 없으면 프롬프트를 손댈 때마다 같은 자리에서 다시 깨진다.

## 실행 방법

실제 LLM 과 실 DB 를 쓴다. 그래서 기본 수집에서 제외되도록 `aide` 마커를 단다.

    pytest -m aide tests/test_aide_wanted_polarity.py -v

프롬프트(`app/agents/query_analyzer_agent.py` 등)를 고쳤다면 **반드시** 돌린다.

## 케이스 고르는 기준

트렌드 권고대로 **경계 사례**를 우선한다 — 잘 되는 문장을 늘리는 건 의미가 없고,
틀리기 쉬운 자리를 덮어야 한다. 특히 아래 둘은 실제로 깨졌던 자리다.

- 완곡 부정(어휘 없이 회피 의도)
- 제외구가 오히려 희망인 문장(`"10일 빼고는 다 D"`)
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

pytestmark = pytest.mark.aide

YEAR, MONTH = 2026, 8

#: (라벨, 입력 문장, 기대 선호 [(일, 코드)], 기대 기피 [(일, 코드)])
#: 기대값이 `ANY` 면 개수/내용을 고정하지 않고 "선호가 있고 기피는 없다" 만 본다
#: (규칙형 요청은 날짜가 월 길이에 따라 달라져 고정할 수 없다).
ANY = "ANY"

CASES: list[tuple[str, str, object, list[tuple[int, str]]]] = [
    # ── 기본형 ────────────────────────────────────────────────────────────
    ("선호만", "10일은 데이로 주세요", [(10, "D")], []),
    ("기피만", "12일 나이트는 피하고 싶어요", [], [(12, "N")]),
    # ── 혼합: 한 문장에 선호와 기피가 함께 (두 테이블로 갈려야 한다) ──────
    ("혼합", "10일은 데이로 주고 12일 나이트는 피하고 싶어요", [(10, "D")], [(12, "N")]),
    ("혼합·완곡", "8일 데이 주시고 15일 나이트는 좀 부담스러워요", [(8, "D")], [(15, "N")]),
    # ── 경계: 완곡 부정 (어휘 매칭으로는 못 잡던 자리) ────────────────────
    ("완곡·부담", "10일 나이트는 좀 부담스러워요", [], [(10, "N")]),
    ("완곡·피했으면", "가능하면 15일 나이트는 피했으면 좋겠어요", [], [(15, "N")]),
    ("완곡·자신없음", "20일 나이트는 자신이 없어요", [], [(20, "N")]),
    # ── 경계: 대체 근무가 지정되면 avoid 를 만들지 않는다 ────────────────
    #   만들면 같은 날 want+avoid 두 건이 되어 duplicate_date 로 저장 전체가 422.
    ("같은날 충돌", "10일은 E 말고 D로 줘", [(10, "D")], []),
    # ── 경계: 제외구가 있지만 주된 의도는 희망 ───────────────────────────
    ("제외구=희망", "10일 빼고는 다 데이로 주세요", ANY, []),
    # ── 경계: 범위를 좁히는 제외구는 **항목 자체를 만들지 않는다** ────────
    #   실제로 깨졌던 자리다. "주말 빼고" 를 avoid 로 읽어 주말 10일에 O 금지를
    #   만들었고(의도 정반대), 그게 솔버까지 하드로 내려가는 것을 실측했다.
    #   날짜 버전("10일 빼고")만 방어 예시가 있고 요일 버전이 없어서 생긴 구멍이다.
    ("범위제외구", "주말 빼고 평일에 데이 위주로 부탁드려요", ANY, []),
    ("범위제외구·평일만", "평일만 근무하고 싶어요", ANY, []),
    ("범위제외구·전부", "주말 제외 전부 나이트로 부탁드려요", ANY, []),
    # ── 경계: **단독** 제외("~는 빼주세요")는 반대로 OFF 희망이다 ──────────
    #   뒤에 다른 요청이 없고 근무코드도 안 나오면 "그날 근무에서 빼달라" 는 뜻이다.
    #   여기를 avoid 로 만들면 "쉬고 싶다" 가 "쉬지 마라" 로 뒤집힌다. 고치기 전
    #   실측에서 "주말은 제외해주세요" 가 3/3 회차 모두 **주말 O 금지**였다.
    (
        "단독제외=OFF희망",
        "주말은 빼주세요",
        [(d, "O") for d in (1, 2, 8, 9, 15, 16, 22, 23, 29, 30)],
        [],
    ),
    ("단독제외·날짜", "8일은 빼주세요", [(8, "O")], []),
    #   "근무"·"스케줄" 은 근무코드가 아니다. 질문형도 요청이다(Chat 으로 새면 빈 결과).
    ("단독제외·일반명사", "8일은 스케줄에서 빼주세요", [(8, "O")], []),
    ("단독제외·질문형", "8일 근무 제외 가능할까요", [(8, "O")], []),
    #   반대쪽 — 근무코드가 명시되면 그대로 avoid 다(과억제 금지). 2026-08 기준 주말.
    ("코드명시=회피", "8일 데이는 빼주세요", [], [(8, "D")]),
    (
        "주말코드=회피",
        "주말 나이트는 힘들어요",
        [],
        [(d, "N") for d in (1, 2, 8, 9, 15, 16, 22, 23, 29, 30)],
    ),
    # ── 다건 ─────────────────────────────────────────────────────────────
    ("다건 기피", "5일이랑 20일 나이트는 안 했으면 좋겠어요", [], [(5, "N"), (20, "N")]),
]


def _split(entries: list[dict]) -> tuple[list, list]:
    """분석 결과를 (선호, 기피) 로 가른다. 각 원소는 (일, 코드)."""
    want, avoid = [], []
    for e in entries:
        day = int(str(e["date"])[8:10])
        item = (day, str(e["shift_id"]).upper())
        (avoid if e.get("intent") == "avoid" else want).append(item)
    return sorted(want), sorted(avoid)


@pytest.fixture(scope="module")
def live_ctx():
    """실 DB 세션과 대상 그룹. 접속 정보가 없으면 스킵한다(CI 격리 환경 대비).

    ★ `conftest.py` 가 `db.client2` 를 인메모리 SQLite 로 통째로 갈아끼운다. 이 평가셋은
      실제 근무코드·간호사 명단이 있어야 분석이 성립하므로 그 대체본을 쓰면 안 된다.
      환경변수로 엔진을 직접 만들어 우회한다.
    """
    import os

    import dotenv
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    # pytest 는 앱 진입점을 안 타므로 .env 가 아직 안 읽혔다. 직접 읽는다.
    # ★ 인자 없는 load_dotenv() 는 **호출한 파일** 기준으로 위로 훑는다. 이 파일은
    #   tests/ 아래라 프로젝트 루트의 .env 를 못 찾고 전부 스킵된다. 경로를 명시한다.
    from pathlib import Path

    dotenv.load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    try:
        from db.models import Nurse, Shift
    except Exception as exc:  # pragma: no cover - 환경 의존
        pytest.skip(f"모델 로드 실패: {exc}")

    # ★ 앱이 쓰는 변수는 `MS_DB_*` 다(`DB_*` 는 다른 DB). `db/client2.py:172` 참조.
    host, port = os.getenv("MS_DB_HOST"), os.getenv("MS_DB_PORT")
    user, pw = os.getenv("MS_DB_USER"), os.getenv("MS_DB_PASSWORD")
    name = os.getenv("EUN_DB_NAME")
    if not all([host, port, user, pw, name]):
        pytest.skip("실 DB 접속 환경변수 미설정 — 평가셋 스킵")

    engine = create_engine(
        f"mssql+pymssql://{user}:{pw}@{host}:{port}/{name}", pool_pre_ping=True
    )
    SessionLocal = sessionmaker(bind=engine)

    db = SessionLocal()
    try:
        row = (
            db.query(Shift.group_id)
            .filter(Shift.show_in_preference == True)  # noqa: E712
            .first()
        )
        if not row:
            pytest.skip("원티드 노출 근무코드가 있는 그룹이 없다")
        group_id = row[0]
        nurse = db.query(Nurse).filter(Nurse.group_id == group_id).first()
        if not nurse:
            pytest.skip(f"그룹 {group_id} 에 간호사가 없다")
        yield db, group_id, nurse.nurse_id
    finally:
        db.close()


@pytest.mark.parametrize(
    "label,text,exp_want,exp_avoid", CASES, ids=[c[0] for c in CASES]
)
def test_polarity(live_ctx, label, text, exp_want, exp_avoid):
    """자연어 한 문장이 선호/기피로 정확히 갈리는지."""
    from services.wanted_service import analyze_wanted_text

    db, group_id, nurse_id = live_ctx
    entries = asyncio.run(
        analyze_wanted_text(db, nurse_id, group_id, text, YEAR, MONTH)
    )
    want, avoid = _split(entries)

    assert avoid == sorted(exp_avoid), (
        f"[{label}] 기피 불일치\n  입력: {text}\n"
        f"  기대: {sorted(exp_avoid)}\n  실제: {avoid}"
    )
    if exp_want is ANY:
        assert want, f"[{label}] 선호가 비었다 — 입력: {text}"
    else:
        assert want == sorted(exp_want), (
            f"[{label}] 선호 불일치\n  입력: {text}\n"
            f"  기대: {sorted(exp_want)}\n  실제: {want}"
        )


def test_same_date_never_both(live_ctx):
    """같은 날짜에 선호와 기피가 동시에 나오면 안 된다.

    날짜당 한 건이 저장 계약이라, 둘 다 나오면 `duplicate_date` 422 로 저장이
    통째로 실패한다. 분석 단계에서 이미 걸러져야 한다.
    """
    from services.wanted_service import analyze_wanted_text

    db, group_id, nurse_id = live_ctx
    for label, text, _w, _a in CASES:
        entries = asyncio.run(
            analyze_wanted_text(db, nurse_id, group_id, text, YEAR, MONTH)
        )
        want, avoid = _split(entries)
        overlap = {d for d, _ in want} & {d for d, _ in avoid}
        assert not overlap, f"[{label}] 같은 날 선호·기피 동시 발생: {sorted(overlap)}"


def test_empty_input_is_noop(live_ctx):
    """빈 문장은 LLM 을 부르지 않고 빈 목록을 준다(비용·지연 절약)."""
    from services.wanted_service import analyze_wanted_text

    db, group_id, nurse_id = live_ctx
    for text in ("", "   ", None):
        assert (
            asyncio.run(analyze_wanted_text(db, nurse_id, group_id, text, YEAR, MONTH))
            == []
        )
