"""isthmus 공유 적합성 벡터(벤더링 사본)로 생산자 규칙을 검증한다.

- 모든 벡터 파일은 `SHA256SUMS`와 `conformance.lock`(isthmus 커밋·파일별 sha256)과 같아야 한다.
- 생산자 대상 사례(`template.grammar`·`template.normalize`·`scope.validate`·`scope.applies`·`dispatch.validate`)는
  100% 통과해야 한다. `dispatch.validate`는 pythograph routes 출력(golden)에도 같은 검증기를 적용한다.
- 해당 없는 사례는 이유별로 분류하고, 모르는 ruleId가 생기면 실패해 벤더링 갱신 때 판단하게 한다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from pythograph.exchange.order import order_problem
from pythograph.exchange.scope import scope_applies, scope_problem
from pythograph.exchange.template import normalize_uri_path, template_problem

#: 벤더링한 벡터 디렉터리다.
CONFORMANCE = Path(__file__).resolve().parent.parent / "conformance"

#: 잠금 파일이다.
LOCK = CONFORMANCE.parent / "conformance.lock"

#: 생산자로서 실행하는 ruleId다.
APPLICABLE = {"template.grammar", "template.normalize", "scope.validate", "scope.applies", "dispatch.validate"}

#: routes golden 문서다(registration-order 문서 포함).
GOLDEN = Path(__file__).resolve().parent / "golden"

#: 건너뛰는 ruleId 접두사와 이유다.
SKIPPED = {
    "match.": "consumer-only matching rule",
    "dispatch.match": "consumer-only registration-order binding",
    "dispatch.shadow": "consumer-only shadowing diagnostics",
    "framework.openapi.": "another producer (openapi)",
    "framework.spring.": "another producer (kartograph)",
    "compose.": "client route-call composition (pythograph emits server declarations only)",
    "wrapper.": "client route-call wrappers (pythograph emits server declarations only)",
}


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
        if case["ruleId"] in APPLICABLE and any(target == "producer" for target in case["appliesTo"])
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
    assert len(_applicable()) == 78


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
