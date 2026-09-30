"""합성 클라이언트 fixture의 전체 출력 회귀와 모의 서버 오라클 대조(오프라인).

- golden: `fixtures/client/shop-client`의 route-call 문서(래퍼 선언 포함)가 `tests/golden/client-shop-client.json`과
  바이트 단위로 같다(`project`만 자리표시자). 의도한 변경이면 `PYTHOGRAPH_UPDATE_GOLDEN=1`로 갱신한다.
- 오라클: `experiments/client_oracle/recorded.json`은 스크래치 가상 환경에서 fixture 함수를 실제로 불러 requests·
  httpx·aiohttp·urllib가 127.0.0.1 모의 서버에 보낸 요청(동사·경로·Host)을 기록한 것이다. 지금 사실이 기록과
  모두 맞아야 한다(root는 전체 경로, base는 꼬리, dynamic은 접두사). 출력이 바뀌면 오라클을 다시 돌린다.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any

from tests.conftest import FIXTURES, GENERATED_AT, REPOSITORY, run_cli

#: 합성 클라이언트 fixture다.
FIXTURE = FIXTURES / "client" / "shop-client"

#: 오라클 디렉터리다.
ORACLE = REPOSITORY / "experiments" / "client_oracle"

#: golden 문서다.
GOLDEN = Path(__file__).resolve().parent / "golden" / "client-shop-client.json"


def client_document(project: Path, *extra: str) -> dict[str, Any]:
    """`routes --role client` 문서를 만든다.

    Args:
        project: 프로젝트 경로.
        extra: 추가 인자.

    Returns:
        문서.
    """
    code, out, err = run_cli(
        ["routes", "--role", "client", "--project", str(project), "--generated-at", GENERATED_AT, *extra]
    )
    assert code == 0, err
    document: dict[str, Any] = json.loads(out)
    return document


def _fixture_document() -> dict[str, Any]:
    """래퍼 선언을 준 fixture 문서다.

    Returns:
        문서.
    """
    return client_document(FIXTURE, "--wrappers", str(FIXTURE / "http-wrappers.json"))


def _load(name: str) -> ModuleType:
    """오라클 디렉터리의 표준 라이브러리 전용 모듈을 읽는다.

    Args:
        name: 파일 이름(확장자 제외).

    Returns:
        모듈.
    """
    spec = importlib.util.spec_from_file_location(f"client_oracle_{name}", ORACLE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_golden_document() -> None:
    """fixture 문서가 golden과 같다."""
    document = _fixture_document()
    text = json.dumps({**document, "project": "<project>"}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if os.environ.get("PYTHOGRAPH_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8")


def test_recorded_requests_match_facts() -> None:
    """기록한 실제 요청이 모두 사실과 맞는다(불일치·누락 0, dynamic은 세 시나리오)."""
    recorded = json.loads((ORACLE / "recorded.json").read_text(encoding="utf-8"))
    rows = _load("compare").compare(recorded, _fixture_document())
    results = [row[3] for row in rows]
    assert results.count("mismatch") == 0 and results.count("missing") == 0, [row for row in rows if row[3] != "match"]
    assert results.count("match") == 32
    assert {row[0] for row in rows if row[3] == "dynamic"} == {
        "shopclient/dynamic.py#download",
        "shopclient/dynamic.py#proxy_get",
        "shopclient/dynamic.py#glued",
    }


def test_recording_covers_every_scenario() -> None:
    """기록이 시나리오 목록 전체를 같은 순서로 담고, 라이브러리 버전을 싣는다."""
    recorded = json.loads((ORACLE / "recorded.json").read_text(encoding="utf-8"))
    scenarios = _load("scenarios").SCENARIOS
    assert [item["usr"] for item in recorded["scenarios"]] == [scenario[0] for scenario in scenarios]
    assert set(recorded["packages"]) == {"requests", "httpx", "aiohttp", "urllib3", "yarl"}


def test_every_emitted_static_fact_is_exercised() -> None:
    """오라클이 fixture의 모든 사실(래퍼 본문의 dynamic 제외)을 시나리오로 확인한다."""
    usrs = {fact["symbol"]["usr"] for fact in _fixture_document()["facts"]}
    scenarios = {scenario[0] for scenario in _load("scenarios").SCENARIOS}
    assert usrs - scenarios == {"shopclient/api.py#Endpoint.perform"}
