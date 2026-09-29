"""Phase 6 종료 조건의 오프라인 확인: Django 백엔드 × iOS/Android 체인에서 세 질문의 기대 경로가 일치한다.

`experiments/e2e/run_trace.py`가 실제 도구(pythograph, schemagraph, cartograph, kartograph, isthmus trace)로 기록한
입력과 출력(`experiments/e2e/recorded/`)을 쓴다. Swift·JVM·Node 없이 CI에서 돌도록:

1. 지금의 pythograph가 기록 당시와 같은 서버 문서(routes·schema·reach·impact)를 내는지 확인한다(다르면 기록을 다시
   만들어야 한다 — 기록한 trace가 더는 이 코드의 결과가 아니다).
2. 기록한 isthmus trace 출력에서 세 질문의 기대 경로(`experiments/e2e/expectations.py`)를 확인한다:
   (a) API → DB 테이블·DB 의존자, (b) API → 클라이언트 호출부 → 영향 심볼, (c) 테이블 → API → 클라이언트.
3. 기록한 trace context가 기대 목록에서 만든 context와 같은지 확인한다.
4. Android(Retrofit) 주문·결제 호출이 host link에 귀속돼 체인에 붙는지 확인한다. 이 기록은 kartograph `4c09d91`
   (#122 route-call usr, #123 baseUrl 결합) 이후 판으로 만든 것이다 — 옛 판으로 다시 기록하면 여기서 실패한다.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import FIXTURES, GENERATED_AT, REPOSITORY, run_cli

#: 종단 검증 디렉터리다.
E2E = REPOSITORY / "experiments" / "e2e"

#: 기록 디렉터리다.
RECORDED = E2E / "recorded"

#: 서버 fixture다.
SERVER = FIXTURES / "e2e" / "shop-api"


def _load(name: str) -> ModuleType:
    """실험 디렉터리의 표준 라이브러리 전용 모듈을 읽는다.

    Args:
        name: 모듈 이름.

    Returns:
        모듈.
    """
    spec = importlib.util.spec_from_file_location(name, E2E / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: 기대 경로 모듈이다.
EXPECTATIONS = _load("expectations")


def _recorded(name: str) -> Any:
    """기록한 JSON을 읽는다.

    Args:
        name: 파일 이름.

    Returns:
        JSON 값.
    """
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


def _normalized(text: str) -> dict[str, Any]:
    """문서의 project를 합성 경로로 바꾼다(`run_trace.normalize`와 같다).

    Args:
        text: 문서 JSON.

    Returns:
        문서.
    """
    document: dict[str, Any] = json.loads(text)
    document["project"] = EXPECTATIONS.SERVER_PROJECT
    return document


def _roots(name: str, tmp_path: Path) -> Path:
    """기록한 사실 문서에서 순회 root 파일을 만든다(비테스트 사실의 usr, 처음 나온 순서).

    Args:
        name: 기록한 사실 문서 이름.
        tmp_path: 임시 디렉터리.

    Returns:
        root 파일.
    """
    facts = _recorded(name)["facts"]
    usrs = list(
        dict.fromkeys(fact["symbol"]["usr"] for fact in facts if "symbol" in fact and not fact.get("testSource"))
    )
    path = tmp_path / f"{name}.roots.json"
    path.write_text(json.dumps(usrs), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("server.http.json", ["routes", "--role", "server"]),
        ("server.persistence.json", ["schema"]),
    ],
)
def test_server_facts_match_recording(name: str, arguments: list[str]) -> None:
    """지금의 routes·schema 출력이 기록과 같다."""
    code, out, err = run_cli([*arguments, "--project", str(SERVER), "--generated-at", GENERATED_AT])
    assert code == 0, err
    assert _normalized(out) == _recorded(name)


@pytest.mark.parametrize(
    ("name", "command", "facts"),
    [
        ("server-forward.json", "reach", "server.http.json"),
        ("server-reverse.json", "impact", "server.persistence.json"),
    ],
)
def test_server_traversals_match_recording(name: str, command: str, facts: str, tmp_path: Path) -> None:
    """지금의 reach·impact 출력이 기록과 같다."""
    roots = _roots(facts, tmp_path)
    code, out, err = run_cli(
        [
            command,
            "--project",
            str(SERVER),
            "--revision",
            "e2e-shop-api",
            "--generated-at",
            GENERATED_AT,
            "--roots-from",
            str(roots),
        ]
    )
    assert code == 0, err
    assert _normalized(out) == _recorded(name)


def test_recorded_contexts_match_expectations() -> None:
    """기록한 trace context가 기대 목록의 context와 같다."""
    for name, context in EXPECTATIONS.trace_contexts().items():
        assert _recorded(f"{name}.context.json") == context


def test_questions_a_and_b_route_selection() -> None:
    """(a) API → DB 테이블·DB 의존자, (b) API → 클라이언트 호출부 → 영향 심볼의 기대 경로가 일치한다."""
    assert EXPECTATIONS.check_routes(_recorded("routes.trace.json")) == []


def test_question_c_relation_selection() -> None:
    """(c) 테이블 → API → 클라이언트의 기대 경로가 일치한다."""
    assert EXPECTATIONS.check_relations(_recorded("relations.trace.json")) == []


#: Android Retrofit 호출이 붙어야 하는 route와 (호출 심볼, 깊이별 영향 심볼)이다.
ANDROID_RETROFIT_CHAINS = {
    ("GET", "/api/orders/{}/"): (
        EXPECTATIONS.ANDROID["get_order"],
        [(EXPECTATIONS.ANDROID["load"], 1), (EXPECTATIONS.ANDROID["order_refresh"], 2)],
    ),
    ("POST", "/api/checkout/"): (
        EXPECTATIONS.ANDROID["checkout"],
        [(EXPECTATIONS.ANDROID["submit"], 1), (EXPECTATIONS.ANDROID["pay"], 2)],
    ),
}


def test_android_retrofit_facts_are_attributed() -> None:
    """기록한 Android Retrofit route-call이 usr와 authority·root 앵커를 싣는다(kartograph #122·#123 이후 모양)."""
    facts = {fact["symbol"]["usr"]: fact for fact in _recorded("android.http.json")["facts"]}
    for usr, _ in ANDROID_RETROFIT_CHAINS.values():
        assert facts[usr]["authority"] == "api.example.com"
        assert facts[usr]["pathAnchor"] == "root"


def test_android_retrofit_calls_join_chains() -> None:
    """Android 주문·결제 호출이 체인에 붙고, 귀속되지 않아 빠진 호출 gap이 없다."""
    routes = _recorded("routes.trace.json")
    for (method, template), (usr, affected) in ANDROID_RETROFIT_CHAINS.items():
        chain = next(
            item for item in routes["chains"] if item["selector"] == {"route": {"method": method, "template": template}}
        )
        route = next(item for item in chain["routes"] if item["scope"] == "android->api")
        calls = {call["call"]["symbol"]["usr"]: call["affected"] for call in route["calls"]}
        assert sorted((hop["usr"], hop["depth"]) for hop in calls[usr]) == sorted(affected)
    for name in ("routes.trace.json", "relations.trace.json"):
        assert all(gap["code"] != "unattributed-calls-omitted" for gap in _recorded(name)["gaps"])


def test_checker_detects_broken_paths() -> None:
    """검사기가 끊긴 경로를 찾아낸다(빈 결과가 우연히 통과하지 않게)."""
    routes = copy.deepcopy(_recorded("routes.trace.json"))
    for chain in routes["chains"]:
        chain["relationUses"] = []
        for route in chain["routes"]:
            route["calls"] = []
    problems = EXPECTATIONS.check_routes(routes)
    assert any("not reached" in problem for problem in problems)
    assert any("calls" in problem for problem in problems)
    relations = copy.deepcopy(_recorded("relations.trace.json"))
    relations["chains"] = relations["chains"][:1]
    relations["gaps"] = []
    assert len(EXPECTATIONS.check_relations(relations)) == 2
