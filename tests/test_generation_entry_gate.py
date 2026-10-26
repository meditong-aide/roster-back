"""EI-11 11a-2 — 생성 진입 관문(H3 · `.omc/aide_docs/v2/engine_input/ei11/EI11_SPEC_v2.md` §2.3·§4.1).

`_generate_roster_service_impl` 의 그룹 해석 바로 뒤에서 두 줄이 차례로 판정한다 — ① `assert_night_caps_within_hard_lock`
(라우터 가드와 같은 함수) ② `card_filter.assert_treatments_allowed`(금지 처방이면 400). 라우터 가드를 안 지나는 통로
(큐 잔류 메시지·DLQ redrive·JOB_JSON·직접 호출)도 같은 판정으로 400 이고, 개인 속성 쓰기·materialize·`_ctx["config"]`
보다 앞이라 부분 반영·실패 낙인이 남지 않는다. 11b 뒤 ① 은 하드락 8종 BLOCK 도 400 이다(`config_override` 하드락 완화 1건).

`tests/test_generation_group_scope.py` 의 `seeded` 방식(그룹 해석은 통과하고 'wanted 미작성'에서 멈추는 sqlite 시드)을 쓴다 —
'wanted' 예외까지 가면 관문을 통과한 증거다. DB·엔진·LLM 없이 돈다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import services.roster_create_service as rcs
from db.models import Group, Nurse, NurseMonthlyLimit, Office
from fastapi import HTTPException
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import RosterRequest
from services.policy import card_filter as cf
from services.roster_create_service import generate_roster_service

APP = Path(__file__).resolve().parents[1] / "app"
_FORBIDDEN = "treatment:disable:night_recovery"
_LEGIT = "treatment:soft:grade_max"
_RELEASE = [{"nurse_id": "HN", "field": "n_max", "value": 10}]


def _user(*, group_id):
    return UserSchema(
        nurse_id="HN", account_id="acc_HN", office_id="o1", group_id=group_id,
        is_head_nurse=True, is_master_admin=False, name="수간",
        EmpSeqNo="", EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True,
        hn_auth=None, original_group_id=group_id, gw_useYN="Y", qpis_useYN="Y",
    )


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="HN", account_id="acc_HN", group_id="A", office_id="o1",
                 name="수간", active=1, is_head_nurse=True, allowed_shifts=[]))
    db.flush()
    return db


@pytest.fixture
def spies(monkeypatch):
    """관문 뒤 단계 — 실패 낙인(`_mark_config_generate_status`)·draft 근무표(`request_schedule_service`)·솔버
    (`_run_cp_sat_basic`) 호출을 센다."""
    calls: dict[str, int] = {"mark": 0, "schedule": 0, "solver": 0}

    def _spy(name):
        def _record(*_args, **_kwargs):
            calls[name] += 1
            raise AssertionError(f"관문 뒤 단계가 불렸다: {name}")
        return _record

    monkeypatch.setattr(rcs, "_mark_config_generate_status", _spy("mark"))
    monkeypatch.setattr(rcs, "request_schedule_service", _spy("schedule"))
    monkeypatch.setattr(rcs, "_run_cp_sat_basic", _spy("solver"))
    return calls


def _generate(db, **kwargs):
    return generate_roster_service(RosterRequest(year=2026, month=8), _user(group_id="A"), db, **kwargs)


def _rejected(db, **kwargs) -> HTTPException:
    with pytest.raises(HTTPException) as exc_info:
        _generate(db, **kwargs)
    assert exc_info.value.status_code == 400
    return exc_info.value


def _passes_gate(db, **kwargs) -> None:
    with pytest.raises(Exception) as exc_info:
        _generate(db, **kwargs)
    assert not isinstance(exc_info.value, HTTPException), exc_info.value
    assert "wanted" in str(exc_info.value), "관문을 지나 'wanted 미작성'에서 멈춰야 한다"


def _worker_is_input_failure():
    """`worker._is_input_failure` 를 원문에서 떼어 실행한다 — worker 모듈은 머리에서 `aws_lambda_powertools`(테스트 venv
    미설치)를 import 해 통째로 못 읽는다. 복사본이 아니라 지금 원문의 함수 정의 그대로다."""
    path = APP / "worker.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    (func,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_is_input_failure"]
    namespace: dict = {}
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_is_input_failure"]


# ── 처방 판정 ────────────────────────────────────────────────────────────────


def test_forbidden_treatment_alone_is_rejected_before_wanted(seeded, spies):
    """금지 처방 단독 → 400 · 문구에 처방 라벨과 `하드락 #4` · 'wanted' 단계에 닿지 않는다."""
    exc = _rejected(seeded, treatment_ids=[_FORBIDDEN])
    assert "'2N 후 2일 OFF 규칙'" in exc.detail and "하드락 #4" in exc.detail and "하드락 #5" in exc.detail
    assert "wanted" not in exc.detail
    # 11r — 누른 카드 제목과 같은 이름 · 화면 상태에 맞는 안내(새로 고침이 아니라 다시 생성)
    assert exc.detail.endswith("— 근무표를 다시 생성하면 지금 적용할 수 있는 조정 방법만 보여 드립니다.")
    assert spies == {"mark": 0, "schedule": 0, "solver": 0}


def test_forbidden_bundled_with_legit_treatment_is_rejected(seeded, spies):
    """금지 처방 + 정당 처방(`treatment:soft:grade_max`) 묶음 → 400(정당 처방 라벨은 문구에 안 든다)."""
    exc = _rejected(seeded, treatment_ids=[_LEGIT, _FORBIDDEN])
    assert "'2N 후 2일 OFF 규칙'" in exc.detail and "grade" not in exc.detail.lower()
    assert spies == {"mark": 0, "schedule": 0, "solver": 0}


def test_legit_treatment_passes_the_gate(seeded):
    """정당 처방만 → 'wanted' 예외(관문 통과 증거)."""
    _passes_gate(seeded, treatment_ids=[_LEGIT])


def test_rejection_writes_no_personal_attribute(seeded, spies):
    """금지 처방 + `monthly_limit_release` → 400 이고 `NurseMonthlyLimit` 행 0 — 개인 속성 쓰기보다 앞에서 끊는다."""
    _rejected(seeded, treatment_ids=[_FORBIDDEN], monthly_limit_release=list(_RELEASE),
              weekend_off_release=["HN"])
    assert seeded.query(NurseMonthlyLimit).count() == 0
    assert spies == {"mark": 0, "schedule": 0, "solver": 0}


def test_personal_attribute_write_happens_after_the_gate(seeded):
    """대조 — 관문을 지나면 같은 `monthly_limit_release` 가 실제로 쓰인다(위 '쓰기 0' 이 빈 단언이 아니다)."""
    _passes_gate(seeded, treatment_ids=[_LEGIT], monthly_limit_release=list(_RELEASE))
    rows = seeded.query(NurseMonthlyLimit).all()
    assert [(r.nurse_id, r.year, r.month, r.n_max) for r in rows] == [("HN", 2026, 8, 10)]


def test_monthly_night_cap_override_over_hard_lock_is_rejected(seeded, spies):
    """`config_override` 의 월 나이트 16 → 400(#6 메시지 글자 그대로) — 라우터 가드와 같은 함수가 서비스 진입에서도 막는다."""
    exc = _rejected(seeded, config_override={"max_nig_per_month": 16})
    assert exc.detail == "월 나이트는 1인 최대 15회(하드락)입니다: max_nig_per_month=16"
    assert spies == {"mark": 0, "schedule": 0, "solver": 0}


def test_hard_lock_relax_override_is_rejected_at_entry(seeded, spies):
    """(EI-11 11b) `config_override` 의 하드락 완화(#5 끄기) → 400 — 큐 잔류·DLQ redrive·JOB_JSON 처럼 라우터를 안 지나는
    생성도 같은 판정으로 막는다. 개인 속성 쓰기보다 앞이라 `monthly_limit_release` 가 함께 와도 쓰이지 않는다."""
    exc = _rejected(seeded, config_override={"two_offs_after_two_nig": False}, monthly_limit_release=list(_RELEASE))
    assert exc.detail == ("하드락 규칙은 요청으로 완화할 수 없습니다: 하드락 #5(나이트 2연속 후 OFF 2회)"
                          " — two_offs_after_two_nig=False")
    assert seeded.query(NurseMonthlyLimit).count() == 0
    assert spies == {"mark": 0, "schedule": 0, "solver": 0}
    assert _worker_is_input_failure()(exc) is True


def test_release_over_hard_lock_from_request_is_rejected(seeded, spies):
    """인자로 안 넘어와도 요청(`req.monthly_limit_release`)의 #6 초과 값을 본다(서비스 쓰기와 같은 폴백)."""
    req = RosterRequest(year=2026, month=8, monthly_limit_release=[{"nurse_id": "HN", "field": "n_max", "value": 16}])
    with pytest.raises(HTTPException) as exc_info:
        generate_roster_service(req, _user(group_id="A"), seeded)
    assert exc_info.value.status_code == 400 and "HN:n_max=16" in exc_info.value.detail
    assert seeded.query(NurseMonthlyLimit).count() == 0


def test_gate_rejection_is_an_input_failure_for_the_worker(seeded):
    """워커는 관문 400 을 입력 오류로 본다(`_is_input_failure` True → FAILED 기록 뒤 ack, 재시도·DLQ 0)."""
    exc = _rejected(seeded, treatment_ids=[_FORBIDDEN])
    assert _worker_is_input_failure()(exc) is True
    assert str(exc).startswith("400: 선택한 해결책 "), "워커가 저장하는 문자열 모양(토스트에 그대로 뜬다)"


# ── 판정 실패(§2.0.6 ④·⑧) ─────────────────────────────────────────────────────


def test_judgement_failure_falls_back_to_hard_lock_families(seeded, monkeypatch, caplog):
    """처방 판정이 터지면 하드락 family 처방은 400(판정 실패 문구), 그 밖 처방은 통과 + WARNING(§2.0.6 ④)."""
    def _boom(ontology=None):
        raise RuntimeError("주입 — 처방 판정 실패")

    monkeypatch.setattr(cf, "forbidden_treatments", _boom)
    exc = _rejected(seeded, treatment_ids=[_FORBIDDEN])
    assert exc.detail == ("선택한 해결책 '2N 후 2일 OFF 규칙'의 하드락 판정을 지금 할 수 없어 적용하지 않았습니다"
                          " — 잠시 뒤 다시 시도해 주세요.")
    _passes_gate(seeded, treatment_ids=[_LEGIT])
    assert any(r.levelname == "WARNING" and r.name == "services.policy.card_filter" for r in caplog.records)


def test_first_line_judgement_defect_propagates_unchanged(seeded, spies, monkeypatch):
    """첫 줄(`assert_night_caps_within_hard_lock`) 판정 코드 결함은 감싸지 않고 그대로 올린다(§2.0.6 ⑧) — 400 이 아니라
    원래 예외라 워커는 재시도·알람 경로로 간다. 그래도 개인 속성 쓰기·솔버는 0 이다(fail-closed)."""
    import services.policy.hard_lock as hard_lock

    def _defect(_override):
        raise KeyError("주입 — 판정 코드 결함")

    monkeypatch.setattr(hard_lock, "override_violations", _defect)
    with pytest.raises(KeyError) as exc_info:
        _generate(seeded, treatment_ids=[_LEGIT], monthly_limit_release=list(_RELEASE))
    assert seeded.query(NurseMonthlyLimit).count() == 0
    assert spies == {"mark": 0, "schedule": 0, "solver": 0}
    assert _worker_is_input_failure()(exc_info.value) is False


# ── 원문 — 관문 자리 ──────────────────────────────────────────────────────────


def test_gate_sits_right_after_group_resolution_and_before_any_write():
    """관문 두 줄은 그룹 해석 바로 뒤, 개인 속성 쓰기(`_wor =`)·materialize·`_ctx["config"]` 보다 앞이다(순서 고정).
    두 줄 모두 호출 자리에서 try 로 감싸지 않는다.
    NM — 관문 뒤에서 바깥 이름을 파이썬 이름으로 바꾼다(`to_internal` — 별칭 정규화와 같은 줄)."""
    tree = ast.parse((APP / "services/roster_create_service.py").read_text(encoding="utf-8"))
    (func,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_generate_roster_service_impl"]
    body = [ast.unparse(n) for n in func.body]
    resolve = next(i for i, s in enumerate(body) if s.startswith("current_user.group_id = resolve_effective_group("))
    assert body[resolve + 1].startswith("assert_night_caps_within_hard_lock(config_override, monthly_limit_release or ")
    assert body[resolve + 2] == "from services.policy.card_filter import assert_treatments_allowed"
    assert body[resolve + 3] == "assert_treatments_allowed(treatment_ids)"
    # 11r — 관문 바로 뒤에서 정수 상한 키 값을 판정이 본 정수로 맞춘다(쓰기 0 · 그 뒤가 개인 속성 쓰기).
    assert body[resolve + 4] == "config_override = _normalize_int_cap_override(config_override)"
    assert body[resolve + 5] == "_override_given = bool(config_override)"  # EI-12 12r — 정규화 전 '개입 여부'(값 없는 별칭도 개입)
    assert body[resolve + 6] == "config_override = normalize_aliases(to_internal(config_override))"  # EI-12 12a — 판정 뒤 별칭 → 정본 · NM — 그 앞에서 바깥 이름 → 파이썬 이름
    assert body[resolve + 7].startswith("_wor = weekend_off_release or ")
    config_line = next(n.lineno for n in ast.walk(func) if isinstance(n, ast.Assign)
                       and ast.unparse(n) == "_ctx['config'] = latest_config")
    assert func.body[resolve + 3].lineno < config_line
