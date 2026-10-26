"""CP-SAT 모델 지문 — 설계서 §7.1 보강(EI-1c, 10-09 리뷰 실험 반영).

다 지어진(아직 풀지 않은) CP-SAT 모델의 `Proto()` 만 읽어 변수·제약·목적식을 **변수 이름 기준
정렬 정규형**으로 만든다. 이 모듈은 모델을 조금도 바꾸지 않고, solve 를 절대 부르지 않는다
(그건 이 모듈을 부르는 쪽의 책임 — 모델이 이미 다 지어진 뒤, solve 전에만 쓴다).

정규형 규칙
  - 변수: `{"name", "domain"}` 목록, 이름으로 정렬. 도메인은 `[lo, hi]` 쌍 목록.
  - 제약: `{"kind", "enforcement", "body"}`, body 안의 변수 색인은 전부 이름으로 바뀐다
    (`vars`·`literals`·`enforcement_literal` 필드 — cp_model.proto 의 변수 참조 관례).
    음수 색인(`-i-1`)은 리터럴 부정이라 `"not(이름)"` 으로 적는다. 제약 목록은 canonical JSON
    문자열 기준으로 정렬한다 — **모델에 쌓인 순서와 무관**하게 같은 제약 집합이면 같은 정규형이
    나오게 하려는 것이다(결정론 — 쌓는 순서가 set 순회 등으로 흔들려도 흡수한다).
    ★ 제약 "목록"의 순서뿐 아니라, **자리가 의미 없는 제약의 "안"**(linear 의 (vars,coeffs)
    짝·bool_or/exactly_one 류의 literals·lin_max/int_prod/all_diff 의 exprs)도 정렬한다
    (2026-10-13 보강 — ortools 가 linear 의 vars 를 변수 "색인"(=생성 순서) 순으로 저장해서,
    전에는 무해한 변수-생성-순서 리팩터에도 digest 가 달라졌다). table·element·automaton·
    circuit·routes·inverse·reservoir·interval·no_overlap[_2d]·cumulative·int_div·int_mod 처럼
    **자리 자체가 의미**인 종류는 손대지 않는다. 분류 안 된 새 제약 종류가 나오면(ortools
    업그레이드 등) 조용히 넘어가지 않고 예외를 던진다.
  - 목적식: 변수 이름 기준으로 정렬한 (이름, 계수) 쌍 목록 + offset·scaling_factor.
  - 이름 없는 변수는 `__unnamed_{색인}` 로 대신하고 개수를 따로 적는다 — 이 색인은 결정론이
    깨지는 자리라, 그 사실 자체를 기록으로 남긴다(설계서 §7.1 '원인 찾아 정규화').

근무 색인 재표기(`shift_types` 인자, 2026-10-14 보강 — EI-1c 마무리)
  `cp_sat_basic.py` 의 `_normalize_requirements` 가 근무 요구치 키를 **set** 으로 모아
  `shift_types`(= 변수 이름 안 근무 색인 `s`)의 순서가 `PYTHONHASHSEED` 에 따라 프로세스마다
  바뀐다(실측 — EI-16 결함 큐 '근무 순서 결정론', `shift_order/REPORT.md`). 이 결함의 수정은
  **앱 로직**이라 이 작업 범위(tests/) 밖이다(EI-16 이 나중에 고친다) — 그동안 지문이 흔들리지
  않게, `model_fingerprint(model, shift_types=...)` 에 그 실행이 실제로 쓴 `shift_types` 목록을
  주면, 변수 이름 속 근무 색인을 **근무 코드 알파벳순**(시드와 무관한, 코드 집합에서만 정해지는
  결정적 순서 — "진짜 운영 순서"가 무엇인지는 EI-16 이 정한다)으로 다시 매긴 뒤 정규형을 다시
  정렬한다(이름이 바뀌면 (vars,coeffs)/literals/exprs 정렬·제약 목록 정렬·목적식 항 정렬이
  전부 다시 필요하다 — `_relabel_shift_indices`). `shift_types=None`(기본값)이면 재표기를
  하지 않는다(장난감 모델 단위 테스트는 근무 색인 이름 규약이 없어 필요 없다).
  대상 이름 패턴(현재 `golden_model/` 변수 이름 전수를 보고 정함 — 참고 구현
  `shift_order/exp/compare_relabel.py`):
    - `x_{n}_{d}_{s}`(cp_sat_basic.py:2784 · fallback_lex.py:1305)
    - `gcasc_d{d}_s{s}_g{g}_c{c}`(grade_constraints.py:591)
    - `tmin_present_t{tid}_d{d}_s{s}`(team_constraints.py:133)
    - `tmin_cover_slack_d{d}_s{s}`(team_constraints.py:146) — 현재 9개 golden_model 조합에서는
      한 번도 안 불리지만(사각지대 — soft 모드가 꺼진 조합뿐), 소스가 같은 `s{s}` 꼴이라 함께 둔다.
    - `hoff_same_t{tid}_r{r}_d{d}_s{s}`(team_grade_handoff_constraints.py:195) — 위와 같은 이유.
    - `ov_diff_(fb_)?{d}_{c1}_{c2}`(objective_terms.py:1062 · fallback_objectives.py:577) — 이
      둘은 색인이 아니라 코드 **문자**가 직접 박힌다(`work_codes` 순회 순서가 흔들려 c1·c2 자리가
      바뀐다). 색인 재표기가 아니라 둘을 알파벳순으로 정렬해 대칭을 맞춘다.
  재표기 안전성(2026-10-14 전수 확인 — 위 목록 밖은 전부 나머지 색인이 간호사·일자·주·등급·코드
  "문자"(이미 코드 식별이라 안전)뿐임을 `golden_model/*.json` 변수 이름 106가지 모양 전수 + 그
  각각의 생성 지점을 grep 으로 대조해 확인했다): `pc_` 는 현재 소스 어디에도 없다(참고 구현이
  쓰인 뒤 이름이 바뀌었거나 삭제됨) — 없는 패턴을 추측해 넣지 않는다.
"""

from __future__ import annotations

import functools
import re
from collections import Counter
from collections.abc import Sequence

from google.protobuf.descriptor import FieldDescriptor

from .canonical import canonical_json, sha256_digest
from .proto_compat import model_to_pb

#: cp_model.proto 관례상 변수/리터럴 색인을 담는 필드 이름(일반화된 재귀 변환의 대상).
_VAR_REF_FIELDS = ("vars", "literals", "enforcement_literal")

# ── 제약 하위 필드 정렬(2026-10-13 보강 — ⑦ 변이 오탐 수정) ─────────────────
#
# 기존 정규형은 제약 "목록"의 쌓인 순서만 흡수했지(canonical_json 정렬), 제약 "안"의
# (vars, coeffs) 짝·literals 나열 순서는 proto 그대로 남겼다. ortools 는 linear 의 vars 를
# 변수 색인 순으로 저장하므로, 변수 "생성 순서"만 바뀌어도(모델 의미는 완전히 같은 무해한
# 리팩터에도) digest 가 달라졌다(실측 — review_fingerprint/exp/e1_toy_order.py). 아래는
# "자리가 의미 없는"(덧셈·논리곱/합·최댓값/곱처럼 교환법칙이 성립하는) 제약 종류만 하위
# 필드를 정렬한다. table·element·automaton·circuit·routes·inverse·reservoir·interval·
# no_overlap[_2d]·cumulative·int_div·int_mod 처럼 **자리 자체가 의미**인 종류는 손대지
# 않는다 — 특히 no_overlap/cumulative 의 `intervals` 는 다른 제약(interval)의 색인을
# 가리키므로, 그 필드를 정렬하면 참조가 깨진다.

#: 리터럴 목록이 집합 의미라 정렬해도 안전한 불리언 논리 종류(BoolArgumentProto).
_LITERAL_SET_KINDS = frozenset({"bool_or", "bool_and", "at_most_one", "exactly_one", "bool_xor"})
#: (vars, coeffs) 짝을 변수 이름 기준으로 정렬해도 안전한 종류 — 덧셈은 교환법칙이 성립한다.
_LINEAR_PAIR_KINDS = frozenset({"linear"})
#: 하위 식(expr) 목록이 집합 의미라 정렬해도 안전한 종류 — 최댓값·곱·전부-다름은 교환법칙이
#: 성립한다. `int_div`/`int_mod` 는 같은 LinearArgumentProto(exprs) 를 쓰지만 자리가
#: 피제수/제수를 가리켜 순서가 의미를 가지므로 **여기 넣지 않는다**.
_EXPR_SET_KINDS = frozenset({"lin_max", "int_prod", "all_diff"})
#: 자리(위치)가 의미를 가져 정렬하면 안 되는 종류 — 분류 누락을 막기 위해, 이 집합과
#: 위 세 집합을 합쳐 ortools 가 실제로 아는 모든 종류(`_known_constraint_kinds`)와
#: 정확히 같아야 한다(아래 `model_fingerprint` 의 검증).
_ORDER_SENSITIVE_KINDS = frozenset({
    "int_div", "int_mod", "element", "circuit", "routes", "table", "automaton",
    "inverse", "reservoir", "interval", "no_overlap", "no_overlap_2d", "cumulative",
    "dummy_constraint",
})


# ── 근무 색인 재표기(2026-10-14 보강 — EI-1c 마무리) ───────────────────────
#
# 변수 이름 꼬리의 근무 색인(`s{n}` 또는 이름 끝자리)을 캡처하는 정규식들. 각 패턴의 첫 번째
# 캡처 그룹은 "색인 앞", 두 번째는 "색인 자체"(정수), 세 번째(있으면)는 "색인 뒤"다. 참고 구현
# `shift_order/exp/compare_relabel.py` 와 같은 모양 — golden_model/*.json 변수 이름 전수 대조로
# 확정했다(위 docstring '근무 색인 재표기' 참고).
_SHIFT_INDEX_NAME_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^(x_\d+_\d+_)(\d+)$"),                               # x_{n}_{d}_{s}
    re.compile(r"^(gcasc_d\d+_s)(\d+)(_g\d+_c\d+)$"),                 # gcasc_d{d}_s{s}_g{g}_c{c}
    re.compile(r"^(tmin_present_t[^_]+_d\d+_s)(\d+)$"),               # tmin_present_t{tid}_d{d}_s{s}
    re.compile(r"^(tmin_cover_slack_d\d+_s)(\d+)$"),                  # tmin_cover_slack_d{d}_s{s}
    re.compile(r"^(hoff_same_t[^_]+_r\d+_d\d+_s)(\d+)$"),             # hoff_same_t{tid}_r{r}_d{d}_s{s}
)
#: `ov_diff_{d}_{c1}_{c2}`(primary) · `ov_diff_fb_{d}_{c1}_{c2}`(lex fallback) — 색인이 아니라
#: 코드 문자 두 개가 직접 박힌다. `work_codes` 순회 순서가 흔들려 c1·c2 자리가 바뀌므로, 색인
#: 재표기 대신 둘을 알파벳순으로 정렬해 대칭을 맞춘다.
_OV_DIFF_PATTERN = re.compile(r"^ov_diff_(fb_)?(\d+)_([A-Z]+)_([A-Z]+)$")


def _relabel_name(name: str, shift_index_map: dict[int, int]) -> str:
    """이름 하나를 근무 색인(코드 알파벳순)으로 다시 매긴다. 대상이 아니면 그대로 돌려준다."""
    if name.startswith("not(") and name.endswith(")"):
        return f"not({_relabel_name(name[4:-1], shift_index_map)})"
    ov = _OV_DIFF_PATTERN.match(name)
    if ov:
        fb, day, c1, c2 = ov.groups()
        lo, hi = sorted((c1, c2))
        return f"ov_diff_{fb or ''}{day}_{lo}_{hi}"
    for pattern in _SHIFT_INDEX_NAME_PATTERNS:
        m = pattern.match(name)
        if not m:
            continue
        groups = m.groups()
        old_index = int(groups[1])
        tail = groups[2] if len(groups) > 2 else ""
        if old_index not in shift_index_map:
            raise ValueError(
                f"model_canon: 이름 '{name}' 의 근무 색인({old_index})이 shift_types 범위 밖 — "
                f"패턴이 대상이 아닌 자리에 잘못 걸렸을 수 있다.",
            )
        return f"{groups[0]}{shift_index_map[old_index]}{tail}"
    return name


def _relabel_value(value, shift_index_map: dict[int, int]):
    """지문 조각(문자열·리스트·dict) 안의 이름 전부를 재귀적으로 재표기한다."""
    if isinstance(value, str):
        return _relabel_name(value, shift_index_map)
    if isinstance(value, list):
        return [_relabel_value(v, shift_index_map) for v in value]
    if isinstance(value, dict):
        return {k: _relabel_value(v, shift_index_map) for k, v in value.items()}
    return value


def _relabel_shift_indices(fp: dict, shift_types: Sequence[str]) -> dict:
    """`model_fingerprint` 결과의 근무 색인을 코드 알파벳순으로 다시 매기고 정규형을 다시
    정렬한다(이름이 바뀌면 (vars,coeffs)/literals/exprs·제약 목록·목적식 항 정렬이 전부 다시
    필요하다 — `shift_order/exp/compare_relabel.py` 의 `resort()` 와 같은 2단계 구조)."""
    if len(set(shift_types)) != len(shift_types):
        raise ValueError(f"model_canon: shift_types 에 중복 코드 — {shift_types!r}")
    canonical_rank = {code: rank for rank, code in enumerate(sorted(shift_types))}
    shift_index_map = {old: canonical_rank[code] for old, code in enumerate(shift_types)}

    variables = sorted(
        (_relabel_value(v, shift_index_map) for v in fp["variables"]),
        key=lambda r: r["name"],
    )

    constraints = []
    for c in fp["constraints"]:
        body = _canonicalize_constraint_body(c["kind"], _relabel_value(c["body"], shift_index_map))
        enforcement = sorted(_relabel_name(lit, shift_index_map) for lit in c["enforcement"])
        constraints.append({"kind": c["kind"], "enforcement": enforcement, "body": body})
    constraints.sort(key=canonical_json)

    objective = dict(fp["objective"])
    if objective.get("terms"):
        objective["terms"] = sorted(
            ([_relabel_name(name, shift_index_map), coeff] for name, coeff in objective["terms"]),
            key=lambda t: t[0],
        )

    return {**fp, "variables": variables, "constraints": constraints, "objective": objective}


@functools.lru_cache(maxsize=1)
def _known_constraint_kinds() -> frozenset[str]:
    """`ConstraintProto` 의 `constraint` oneof 필드 이름 전부 — 하드코딩하지 않고 설치된
    ortools 에서 동적으로 모은다(새 버전에서 종류가 늘어도 자동으로 안다, rule_audit.py 의
    `dir(CpModel)` 동적 수집과 같은 태도)."""
    from ortools.sat import cp_model_pb2

    oneof = cp_model_pb2.ConstraintProto.DESCRIPTOR.oneofs_by_name["constraint"]
    return frozenset(f.name for f in oneof.fields)


def _canonicalize_constraint_body(kind: str | None, body: dict) -> dict:
    """순서 의미가 없는 제약 종류만 하위 필드를 정렬한다(위 분류 참고)."""
    if kind in _LITERAL_SET_KINDS and "literals" in body:
        return {**body, "literals": sorted(body["literals"])}
    if kind in _LINEAR_PAIR_KINDS and "vars" in body:
        coeffs = body.get("coeffs", [])
        pairs = sorted(zip(body["vars"], coeffs), key=lambda p: p[0])
        out = dict(body)
        out["vars"] = [p[0] for p in pairs]
        if coeffs:
            out["coeffs"] = [p[1] for p in pairs]
        return out
    if kind in _EXPR_SET_KINDS and "exprs" in body:
        return {**body, "exprs": sorted(body["exprs"], key=canonical_json)}
    return body


def _literal_name(index: int, names: list[str]) -> str:
    """리터럴 색인 → 이름(음수면 부정 — `-i-1` 이 cp_model 의 NegatedRef 관례)."""
    if index >= 0:
        return names[index]
    return f"not({names[-index - 1]})"


def _message_to_canon(msg, names: list[str]) -> dict:
    """제약 하위 메시지 → 변수 참조 필드를 이름으로 바꾄 dict(일반 protobuf 재귀 변환)."""
    out: dict = {}
    for field in msg.DESCRIPTOR.fields:
        fname = field.name
        is_message = field.type == FieldDescriptor.TYPE_MESSAGE
        # protobuf 6.33(ortools 9.15 동봉판)부터 `field.label` 직접 접근이
        # DeprecationWarning 을 던진다 — `is_repeated` 속성(6.33 신설)을 우선 쓰고,
        # 없는(구) protobuf 판이면 `label` 비교로 대체한다(판과 무관).
        is_repeated = getattr(field, "is_repeated", None)
        if is_repeated is None:
            is_repeated = field.label == FieldDescriptor.LABEL_REPEATED
        if is_repeated:
            values = list(getattr(msg, fname))
            if not values:
                continue
            if is_message:
                out[fname] = [_message_to_canon(v, names) for v in values]
            elif fname in _VAR_REF_FIELDS:
                out[fname] = [_literal_name(v, names) for v in values]
            else:
                out[fname] = values
        elif is_message:
            if msg.HasField(fname):
                out[fname] = _message_to_canon(getattr(msg, fname), names)
        else:
            value = getattr(msg, fname)
            if value:
                out[fname] = value
    return out


def _variable_names(proto) -> tuple[list[str], list[int]]:
    """변수 색인 → 이름 목록 + 이름 없는 변수의 색인 목록(결정론 경고용)."""
    names: list[str] = []
    unnamed: list[int] = []
    for i, v in enumerate(proto.variables):
        if v.name:
            names.append(v.name)
        else:
            names.append(f"__unnamed_{i}")
            unnamed.append(i)
    return names, unnamed


def _variable_record(index: int, name: str, proto) -> dict:
    """변수 하나 → `{name, domain}` — domain 은 `[lo, hi]` 쌍 목록(cp_model 의 flat 쌍 인코딩)."""
    domain = list(proto.variables[index].domain)
    pairs = [[domain[i], domain[i + 1]] for i in range(0, len(domain), 2)]
    return {"name": name, "domain": pairs}


def model_fingerprint(model, shift_types: Sequence[str] | None = None) -> dict:
    """CP-SAT 모델 → `{variables, constraints, objective, unnamed_variable_count,
    duplicate_names}` 정렬 정규형(설계서 §7.1 '변수 이름 기준 정렬 정규형').

    `shift_types` 를 주면(그 실행이 실제로 쓴 `NurseRosterConfig.shift_types` 그대로) 변수
    이름 속 근무 색인을 코드 알파벳순으로 다시 매긴다(위 모듈 docstring '근무 색인 재표기' —
    EI-16 '근무 순서 결정론' 결함이 흔드는 자리를 흡수한다). `None`(기본값)이면 재표기 없이
    기존 그대로다."""
    proto = model_to_pb(model)
    names, unnamed = _variable_names(proto)
    seen = Counter(names)
    duplicates = sorted(n for n, c in seen.items() if c > 1)

    variables = sorted(
        (_variable_record(i, n, proto) for i, n in enumerate(names)),
        key=lambda r: r["name"],
    )

    classified = _LITERAL_SET_KINDS | _LINEAR_PAIR_KINDS | _EXPR_SET_KINDS | _ORDER_SENSITIVE_KINDS
    unclassified = _known_constraint_kinds() - classified
    if unclassified:
        raise ValueError(
            f"model_canon: 정렬 안전성을 분류하지 않은 제약 종류 {sorted(unclassified)} — "
            f"_LITERAL_SET_KINDS/_LINEAR_PAIR_KINDS/_EXPR_SET_KINDS(정렬 가능)·"
            f"_ORDER_SENSITIVE_KINDS(정렬 불가) 중 하나에 넣어 분류한 뒤 다시 돌려라.",
        )

    constraints = []
    for c in proto.constraints:
        kind = c.WhichOneof("constraint")
        if kind and kind not in classified:
            raise ValueError(f"model_canon: 알 수 없는 제약 종류 '{kind}'(ortools 버전 확인 필요)")
        body = _message_to_canon(getattr(c, kind), names) if kind else {}
        body = _canonicalize_constraint_body(kind, body)
        enforcement = sorted(_literal_name(i, names) for i in c.enforcement_literal)
        constraints.append({"kind": kind, "enforcement": enforcement, "body": body})
    constraints.sort(key=canonical_json)

    objective: dict = {}
    if proto.HasField("objective"):
        obj = proto.objective
        terms = sorted(
            ([names[v], coeff] for v, coeff in zip(obj.vars, obj.coeffs, strict=True)),
            key=lambda t: t[0],
        )
        objective = {"terms": terms, "offset": obj.offset, "scaling_factor": obj.scaling_factor}

    fp = {
        "variables": variables,
        "constraints": constraints,
        "objective": objective,
        "unnamed_variable_count": len(unnamed),
        "duplicate_names": duplicates,
    }
    if shift_types is not None:
        fp = _relabel_shift_indices(fp, shift_types)
    return fp


def fingerprint_summary(model) -> dict:
    """빠른 요약 — 변수 수·제약 종류별 수·목적식 항 수(순회 순서와 무관한 값만, `golden.py`
    `primary_build_summary` 와 같은 모양)."""
    proto = model_to_pb(model)
    kinds = Counter(c.WhichOneof("constraint") for c in proto.constraints)
    enforced = sum(1 for c in proto.constraints if len(c.enforcement_literal) > 0)
    return {
        "variables": len(proto.variables),
        "constraints": len(proto.constraints),
        "constraint_kinds": dict(sorted(kinds.items())),
        "enforced_constraints": enforced,
        "objective_terms": len(proto.objective.vars),
        "has_objective": proto.HasField("objective"),
    }


def fingerprint_digest(fp: dict) -> str:
    """지문의 sha256 — `variables`·`constraints`·`objective` 만(이름 없는 변수 경고는 뺀다:
    그 경고 자체는 결정론이 흔들리는 신호라 digest 에 넣으면 그 흔들림을 숨기지 못하고 오히려
    같은 모델인데 색인이 달라 digest 가 갈릴 수 있다 — 그 경우는 `unnamed_variable_count` 로
    따로 본다)."""
    return sha256_digest({
        "variables": fp["variables"], "constraints": fp["constraints"], "objective": fp["objective"],
    })
