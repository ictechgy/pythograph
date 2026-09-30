"""파이썬 클라이언트 × Django 서버 종단 trace의 오프라인 확인.

`experiments/client_e2e/run_trace.py`가 pythograph(클라이언트 routes·impact)와 isthmus trace(3a45450)로 기록한 입력과
출력(`experiments/client_e2e/recorded/`)을 쓴다. 서버 member는 Phase 6 기록(`experiments/e2e/recorded/`)이다.

1. 지금의 pythograph가 기록과 같은 클라이언트 문서(route-call·역방향 순회)를 낸다(다르면 기록을 다시 만든다).
2. 기록한 context가 기대 목록에서 만든 context와 같다.
3. 기록한 trace에서 네 route 모두 파이썬 호출부가 exact로 서버 핸들러에 붙고, 호출부 → 화면 함수(깊이 1), 핸들러 →
   relation-use 테이블로 이어진다.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from tests.conftest import FIXTURES, GENERATED_AT, REPOSITORY, run_cli

#: 종단 검증 디렉터리다.
E2E = REPOSITORY / "experiments" / "client_e2e"

#: 기록 디렉터리다.
RECORDED = E2E / "recorded"

#: 클라이언트 fixture다.
CLIENT = FIXTURES / "e2e" / "py-client"


def _expectations() -> ModuleType:
    """기대 경로 모듈을 읽는다(표준 라이브러리만 쓴다).

    Returns:
        모듈.
    """
    spec = importlib.util.spec_from_file_location("client_e2e_expectations", E2E / "expectations.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: 기대 경로 모듈이다.
EXPECTATIONS = _expectations()


def _recorded(name: str) -> Any:
    """기록한 JSON을 읽는다.

    Args:
        name: 파일 이름.

    Returns:
        JSON 값.
    """
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


def _normalized(text: str) -> dict[str, Any]:
    """문서의 project를 합성 경로로 바꾼다(`run_trace.client_documents`와 같다).

    Args:
        text: 문서 JSON.

    Returns:
        문서.
    """
    document: dict[str, Any] = json.loads(text)
    document["project"] = EXPECTATIONS.CLIENT_PROJECT
    return document


def test_client_routes_match_recording() -> None:
    """지금의 route-call 문서가 기록과 같다."""
    code, out, err = run_cli(["routes", "--role", "client", "--project", str(CLIENT), "--generated-at", GENERATED_AT])
    assert code == 0, err
    assert _normalized(out) == _recorded("python.http.json")


def test_client_reverse_matches_recording(tmp_path: Path) -> None:
    """지금의 역방향 순회(root = route-call usr 전체)가 기록과 같다."""
    usrs = list(dict.fromkeys(fact["symbol"]["usr"] for fact in _recorded("python.http.json")["facts"]))
    roots = tmp_path / "roots.json"
    roots.write_text(json.dumps(usrs), encoding="utf-8")
    code, out, err = run_cli(
        [
            "impact",
            "--project",
            str(CLIENT),
            "--revision",
            EXPECTATIONS.CLIENT_REVISION,
            "--generated-at",
            GENERATED_AT,
            "--roots-from",
            str(roots),
        ]
    )
    assert code == 0, err
    assert _normalized(out) == _recorded("python-reverse.json")


def test_context_matches_expectations() -> None:
    """기록한 context가 기대 목록에서 만든 context와 같고, 서버 문서 경로가 실제 기록을 가리킨다."""
    context = _recorded("routes.context.json")
    assert context == EXPECTATIONS.trace_context()
    server = next(member for member in context["members"] if member["name"] == "server")
    paths = [*server["documents"], *(analysis["path"] for analysis in server["analyses"])]
    assert all((RECORDED / path).resolve().is_file() for path in paths)


def test_recorded_trace_has_expected_chains() -> None:
    """기록한 trace에서 네 route의 체인(호출부 → 핸들러 → 테이블, 호출부 → 영향 심볼)이 기대와 같다."""
    trace = _recorded("routes.trace.json")
    assert EXPECTATIONS.check(trace) == []
    assert trace["summary"]["calls"] == 4
    assert trace["summary"]["clientSymbols"] == 4
