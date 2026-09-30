"""isthmus 공유 적합성 벡터(벤더링 사본)로 생산자 규칙을 검증한다.

- 모든 벡터 파일은 `SHA256SUMS`와 `conformance.lock`(isthmus 커밋·파일별 sha256)과 같아야 한다.
- 생산자 대상 사례(`template.grammar`·`template.normalize`·`scope.validate`·`scope.applies`·`dispatch.validate`,
  url-compose의 `compose.*`·`wrapper.*`)는 100% 통과해야 한다. `dispatch.validate`는 pythograph routes 출력(golden)에도
  같은 검증기를 적용하고, `wrapper.location`은 실제 클라이언트 스캐너로 확인한다.
- 해당 없는 사례는 이유별로 분류하고, 모르는 ruleId가 생기면 실패해 벤더링 갱신 때 판단하게 한다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from pythograph.exchange.order import order_problem
from pythograph.routes.client.compose import (
    UNKNOWN_BASE,
    Joined,
    Literal,
    Part,
    QueryTail,
    Value,
    base_from_parts,
    canonical_template,
    compose_path,
    finish,
    join_path,
    mask_template,
    split_url,
)
from pythograph.routes.client.wrappers import ArgumentSpec, CallArgument, MethodToken, WrapperDecl, bind_method
from pythograph.exchange.scope import scope_applies, scope_problem
from pythograph.exchange.template import normalize_uri_path, template_problem

#: 벤더링한 벡터 디렉터리다.
CONFORMANCE = Path(__file__).resolve().parent.parent / "conformance"

#: 잠금 파일이다.
LOCK = CONFORMANCE.parent / "conformance.lock"

#: 생산자로서 실행하는 ruleId다.
APPLICABLE = {
    "template.grammar",
    "template.normalize",
    "scope.validate",
    "scope.applies",
    "dispatch.validate",
    "compose.interpolation",
    "compose.query-tail",
    "compose.suffix",
    "compose.normalize",
    "compose.base-join",
    "compose.strip",
    "compose.mask",
    "wrapper.method",
    "wrapper.location",
}

#: routes golden 문서다(registration-order 문서 포함).
GOLDEN = Path(__file__).resolve().parent / "golden"

#: 건너뛰는 ruleId 접두사와 이유다.
SKIPPED = {
    "match.": "consumer-only matching rule",
    "dispatch.match": "consumer-only registration-order binding",
    "dispatch.shadow": "consumer-only shadowing diagnostics",
    "framework.openapi.": "another producer (openapi)",
    "framework.spring.": "another producer (kartograph)",
    "scope.dynamic-": "dynamicScope validation (pythograph does not emit dynamicScope yet)",
}

#: 이 생산자의 `appliesTo` 대상이다(`producer`와 `producer:pythograph`).
PRODUCER_TARGETS = ("producer", "producer:pythograph")


def _cases() -> list[dict[str, Any]]:
    """모든 벡터 사례를 읽는다.

    Returns:
        사례 목록(각 사례에 suite 이름을 더한다).
    """
    cases: list[dict[str, Any]] = []
    for path in sorted(CONFORMANCE.glob("*.json")):
        suite = json.loads(path.read_text(encoding="utf-8"))
        cases.extend({**case, "suite": suite["suite"]} for case in suite["cases"])
    return cases


def _applicable() -> list[dict[str, Any]]:
    """생산자로서 실행할 사례를 고른다.

    Returns:
        사례 목록.
    """
    return [
        case
        for case in _cases()
        if case["ruleId"] in APPLICABLE and any(target in PRODUCER_TARGETS for target in case["appliesTo"])
    ]


def test_vendored_files_match_sums_and_lock() -> None:
    """벡터 파일이 SHA256SUMS·잠금 파일과 같고 목록에 빠진 파일이 없다."""
    sums = dict(reversed(line.split()) for line in (CONFORMANCE / "SHA256SUMS").read_text().splitlines() if line)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    vectors = sorted(path.name for path in CONFORMANCE.glob("*.json"))
    assert sorted(sums) == vectors == sorted(lock["files"])
    assert len(lock["commit"]) == 40
    for name in vectors:
        digest = hashlib.sha256((CONFORMANCE / name).read_bytes()).hexdigest()
        assert digest == sums[name] == lock["files"][name], f"{name}: re-vendor it from isthmus"


def test_every_case_is_applicable_or_classified() -> None:
    """모든 사례는 실행하거나 알려진 이유로 건너뛴다(새 ruleId는 판단을 요구한다)."""
    unknown = [
        case["id"]
        for case in _cases()
        if case["ruleId"] not in APPLICABLE and not any(case["ruleId"].startswith(prefix) for prefix in SKIPPED)
    ]
    assert unknown == []


def test_applicable_case_count() -> None:
    """실행하는 생산자 사례 수가 벤더링한 벡터와 맞는다(조용히 줄지 않게)."""
    assert len(_applicable()) == 135


@pytest.mark.parametrize(
    "case", [case for case in _applicable() if case["ruleId"] == "template.grammar"], ids=lambda case: case["id"]
)
def test_template_grammar(case: dict[str, Any]) -> None:
    """정규 문법 검사기가 소비자와 같은 판정과 거부 사유를 낸다."""
    problem = template_problem(case["input"]["template"])
    assert (problem is None) == case["expect"]["valid"]
    if "reason" in case["expect"]:
        assert problem == case["expect"]["reason"]


@pytest.mark.parametrize(
    "case", [case for case in _applicable() if case["ruleId"] == "template.normalize"], ids=lambda case: case["id"]
)
def test_template_normalize(case: dict[str, Any]) -> None:
    """URI 경로 정규화가 벡터와 같다."""
    assert normalize_uri_path(case["input"]["path"]) == case["expect"]["template"]


@pytest.mark.parametrize(
    "case", [case for case in _applicable() if case["ruleId"] == "scope.validate"], ids=lambda case: case["id"]
)
def test_scope_validate(case: dict[str, Any]) -> None:
    """스코프 검증기가 소비자와 같이 판정한다."""
    assert (scope_problem({"limitationIndex": 0, **case["input"]["scope"]}) is None) == case["expect"]["valid"]


@pytest.mark.parametrize(
    "case", [case for case in _applicable() if case["ruleId"] == "scope.applies"], ids=lambda case: case["id"]
)
def test_scope_applies(case: dict[str, Any]) -> None:
    """참조 구현의 스코프 적용 판정이 벡터와 같다."""
    assert scope_applies(case["input"]["scope"], case["input"]["probe"]) == case["expect"]["applies"]


@pytest.mark.parametrize(
    "case", [case for case in _applicable() if case["ruleId"] == "dispatch.validate"], ids=lambda case: case["id"]
)
def test_dispatch_validate(case: dict[str, Any]) -> None:
    """`order` 검증기가 소비자와 같이 판정한다(isthmus 참조 실행기와 같게 빠진 위치를 채운다)."""
    facts = [
        {
            "kind": "route-decl",
            "dynamic": False,
            "pathAnchor": "root",
            **{key: value for key, value in fact.items() if key != "location"},
            "location": {
                "path": "shop/urls.py",
                "line": fact.get("location", {}).get("line", index + 1),
                "column": fact.get("location", {}).get("column", 1),
            },
        }
        for index, fact in enumerate(case["input"]["document"]["facts"])
    ]
    document = {"dispatch": case["input"]["document"]["dispatch"], "facts": facts}
    assert (order_problem(document) is None) == case["expect"]["valid"]


@pytest.mark.parametrize("name", ["drf-shop.json", "blog-app.json"])
def test_routes_output_passes_dispatch_validation(name: str) -> None:
    """pythograph routes 출력이 `dispatch.validate` 규칙을 지킨다(Django 문서는 모든 decl에 order가 있다)."""
    document = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    assert order_problem(document) is None
    if document["dispatch"] == "registration-order":
        assert all("order" in fact for fact in document["facts"])


def _cases_for(rule: str) -> list[dict[str, Any]]:
    """ruleId 하나의 생산자 사례를 고른다.

    Args:
        rule: ruleId.

    Returns:
        사례 목록.
    """
    return [case for case in _applicable() if case["ruleId"] == rule]


def _parts(items: list[dict[str, str]]) -> list[Part]:
    """벡터의 `parts`를 조각으로 바꾼다.

    Args:
        items: `{"literal"}`·`{"value"}`·`{"queryTail"}` 목록.

    Returns:
        조각 목록.
    """
    converted: list[Part] = []
    for item in items:
        if "literal" in item:
            converted.append(Literal(item["literal"]))
        elif "queryTail" in item:
            converted.append(QueryTail())
        else:
            converted.append(Value())
    return converted


@pytest.mark.parametrize(
    "case",
    [
        case
        for rule in ("compose.interpolation", "compose.query-tail", "compose.suffix", "compose.normalize")
        for case in _cases_for(rule)
    ],
    ids=lambda case: case["id"],
)
def test_compose_parts(case: dict[str, Any]) -> None:
    """경로 조각 조립(query 꼬리·세그먼트 보간·정규화)이 벡터와 같다."""
    result = compose_path(_parts(case["input"]["parts"]))
    expect = case.get("expect", {})
    if case.get("expectDynamic"):
        assert result.dynamic
        prefix = canonical_template(result.prefix, False) if result.prefix else None
        assert prefix == expect.get("channelPrefix")
        return
    assert result.raw is not None
    assert canonical_template(result.raw, False) == expect["template"]
    assert result.query_tail_stripped == expect.get("queryTailStripped", False)


def _minimum(range_text: str | None) -> tuple[int, int]:
    """벡터의 `versionRange`(`>=3.11`)에서 최소 (메이저, 마이너)를 읽는다. 없으면 확인한 최신 버전으로 본다.

    Args:
        range_text: 버전 범위 문자열 또는 None.

    Returns:
        (메이저, 마이너).
    """
    if range_text is None:
        return (3, 14)
    major, minor = range_text.removeprefix(">=").split(".")[:2]
    return int(major), int(minor)


@pytest.mark.parametrize("case", _cases_for("compose.base-join"), ids=lambda case: case["id"])
def test_compose_base_join(case: dict[str, Any]) -> None:
    """base 결합(`rfc3986`·`slash-join`·`dio-concat`·`httpx-base-url`·`aiohttp-base-url`)이 벡터와 같다.

    `versionRange`가 있는 사례는 그 하한을 증명한 프로젝트로 실행한다(aiohttp 버전 제약).
    """
    data = case["input"]
    base = UNKNOWN_BASE if data["base"] is None else base_from_parts([Literal(data["base"])])
    raw = compose_path([Literal(data["path"])]).raw
    assert raw is not None
    composed = finish(join_path(data["join"], base, raw, _minimum(case.get("versionRange"))), False)
    if case.get("expectDynamic"):
        assert composed.dynamic
        assert composed.ambiguous == (case.get("expectLimitation") == "ambiguous-base-join:")
        return
    assert composed.template == case["expect"]["template"]
    assert composed.anchor == case["expect"]["pathAnchor"]
    if "authority" in case["expect"]:
        assert composed.authority == case["expect"]["authority"]


@pytest.mark.parametrize("case", _cases_for("compose.strip"), ids=lambda case: case["id"])
def test_compose_strip(case: dict[str, Any]) -> None:
    """전체 URL에서 scheme·userinfo·query·fragment를 떼고 authority를 소문자로 싣는다."""
    split = split_url([Literal(case["input"]["url"])])
    path = compose_path(split.path)
    assert split.kind == "absolute" and path.raw is not None
    composed = finish(Joined(path.raw, "root", split.authority), path.query_tail_stripped)
    assert composed.template == case["expect"]["template"]
    assert composed.authority == case["expect"]["authority"]
    assert composed.query_tail_stripped == case["expect"].get("queryTailStripped", False)


@pytest.mark.parametrize("case", _cases_for("compose.mask"), ids=lambda case: case["id"])
def test_compose_mask(case: dict[str, Any]) -> None:
    """고엔트로피 세그먼트와 웹훅 경로를 가린다."""
    masked, count = mask_template(case["input"]["template"], case["input"]["authority"])
    assert masked == case["expect"]["template"]
    assert count == case["expect"]["maskedSegments"]


def _token(value: object) -> MethodToken:
    """벡터의 인자 값 토큰을 동사 토큰으로 바꾼다.

    Args:
        value: `{"literal"}`·`{"enumCase"}`·`{"value"}`.

    Returns:
        토큰.
    """
    assert isinstance(value, dict)
    if "literal" in value:
        return MethodToken("literal", value["literal"])
    if "enumCase" in value:
        return MethodToken("enum", value["enumCase"])
    return MethodToken("value")


def _spec(data: dict[str, Any] | None) -> ArgumentSpec | None:
    """벡터의 인자 자리 선언을 바꾼다.

    Args:
        data: `{index?, label?}` 또는 None.

    Returns:
        인자 자리 또는 None.
    """
    return None if data is None else ArgumentSpec(data.get("index"), data.get("label"))


@pytest.mark.parametrize("case", _cases_for("wrapper.method"), ids=lambda case: case["id"])
def test_wrapper_method(case: dict[str, Any]) -> None:
    """래퍼 동사 인자 바인딩(레이블·위치·기본값·enum)이 벡터와 같다."""
    declared = case["input"]["declaration"]
    declaration = WrapperDecl(
        position=0,
        language="python",
        kind="function",
        owner="api.py",
        name="send",
        method_arg=_spec(declared.get("methodArg")),
        path_arg=ArgumentSpec(1, "path"),
        default_method=declared.get("defaultMethod"),
        method_enum=declared.get("methodEnum", {}),
    )
    arguments = [CallArgument(item.get("label"), item["value"]) for item in case["input"]["call"]["args"]]
    method = bind_method(declaration, arguments, _token)
    if case.get("expectDynamic"):
        assert method is None
    else:
        assert method == case["expect"]["method"]


@pytest.mark.parametrize("case", _cases_for("wrapper.location"), ids=lambda case: case["id"])
def test_wrapper_location(case: dict[str, Any], tmp_path: Path) -> None:
    """여러 줄 래퍼 호출은 호출식이 시작하는 줄을 싣는다(실제 스캐너)."""
    from pythograph.routes.client.extract import ClientOptions, extract_client_calls
    from pythograph.source.project import Project

    data = case["input"]
    filler = "\n" * (data["callStartLine"] - 4)
    source = (
        "def send(method, path):\n    return method, path\n"
        + filler
        + "def caller():\n    return send(\n"
        + "        \"GET\",\n"
        + "        \"/x\",\n    )\n"
    )
    (tmp_path / "api.py").write_text(source, encoding="utf-8")
    lines = source.splitlines()
    assert lines[data["callStartLine"] - 1].strip().startswith("return send(")
    assert lines[data["methodArgLine"] - 1].strip() == '"GET",'
    assert lines[data["pathArgLine"] - 1].strip() == '"/x",'
    wrapper = WrapperDecl(0, "python", "function", "api.py", "send", ArgumentSpec(0), ArgumentSpec(1), None)
    extraction = extract_client_calls(Project.open(tmp_path), ClientOptions(False, (wrapper,)))
    assert [call.location.line for call in extraction.calls] == [case["expect"]["line"]]
