"""setting/*_upload 는 마스터 관리자(ADM) 전용 — 권한 게이트 회귀 방지.

배경(2026-08-26 실측): member/division/position 업로드 세 곳 모두 **권한 검사가 없어**
로그인만 하면 통과했다. 프론트 Nav 가 is_master_admin 으로 메뉴를 숨기고 있었을 뿐이라
URL 을 알면 누구나 실행 가능했다 — UI 은폐가 유일한 방어선이었다.

이 연산이 위험한 이유:
  1) delete-then-insert 로 **office 전체** 인사 마스터를 갈아끼운다(병동 조건 없음).
  2) 성공 시 외부 그룹웨어(gw.meditong.com)로 **역전파**한다.
  3) 엑셀에 없는 사람은 사라진다(스냅샷 replace).
그래서 병동 단위 권한(HN)으로는 열 수 없다.
"""
from pathlib import Path

import pytest
from fastapi import HTTPException

from schemas.auth_schema import User as UserSchema
from services.group_access import assert_master_admin


def _user(**over):
    base = dict(
        nurse_id="N001", account_id="acc_N001", office_id="OFF001", group_id="GRP001",
        name="김민지", mb_part="", office_name="", mb_part_name="",
        gw_useYN="", qpis_useYN="", official_title_name=None,
    )
    base.update(over)
    return UserSchema(**base)


# ── 게이트 자체 ─────────────────────────────────────────


def test_admin_passes():
    assert assert_master_admin(_user(is_master_admin=True)) is None


def test_head_nurse_blocked():
    """HN 도 막힌다 — 병동 권한으로 office 전체를 갈아끼울 수 없다."""
    with pytest.raises(HTTPException) as ei:
        assert_master_admin(_user(is_head_nurse=True, hn_auth="HN"))
    assert ei.value.status_code == 403


def test_general_nurse_blocked():
    with pytest.raises(HTTPException) as ei:
        assert_master_admin(_user())
    assert ei.value.status_code == 403


def test_anonymous_is_401_not_403():
    """미인증은 403(권한없음)이 아니라 401(인증없음)이어야 한다."""
    with pytest.raises(HTTPException) as ei:
        assert_master_admin(None)
    assert ei.value.status_code == 401


# ── 실제 엔드포인트에 걸려 있는가 ───────────────────────


@pytest.mark.parametrize("module_name", ["member", "division", "position"])
def test_every_upload_handler_has_guard(module_name):
    """current_user 를 받는 setting 라우트 핸들러는 전부 가드를 호출해야 한다.

    새 핸들러를 추가하면서 가드를 빼먹으면 여기서 걸린다.

    ★ 모듈을 import 하지 않고 **소스 텍스트만** 파싱한다. routers.setting.* 는 전이
      의존으로 ortools 를 끌어오는데, 그 계열은 환경(protobuf gencode/runtime 불일치)에
      따라 import 자체가 실패한다. 권한 가드 유무는 런타임과 무관한 정적 사실이므로
      환경에 물릴 이유가 없다.
    """
    import ast

    path = (
        Path(__file__).resolve().parents[2]
        / "app" / "routers" / "setting" / f"{module_name}.py"
    )
    assert path.exists(), f"소스 없음: {path}"
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # 다운로드(정적 파일)는 대상 아님 — 인사 데이터를 쓰지 않는다.
        if "download" in node.name:
            continue
        args = [a.arg for a in node.args.args] + [a.arg for a in node.args.kwonlyargs]
        if "current_user" not in args:
            continue
        body = ast.get_source_segment(src, node) or ""
        if "assert_master_admin(current_user)" not in body:
            missing.append(node.name)

    assert not missing, (
        f"routers/setting/{module_name}.py 에 ADM 가드 없는 핸들러: {missing}. "
        "assert_master_admin(current_user) 를 첫 줄에 추가하세요."
    )
    assert "assert_master_admin" in src, f"{module_name}: 가드 import 자체가 없음"
