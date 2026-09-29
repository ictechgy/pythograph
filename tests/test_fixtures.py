"""합성 fixture 전체 출력 회귀와 기록한 오라클 대조(오프라인).

- golden: 두 fixture의 전체 문서가 `tests/golden/*.json`과 바이트 단위로 같다(`project`만 자리표시자).
  의도한 변경이면 `PYTHOGRAPH_UPDATE_GOLDEN=1 uv run pytest tests/test_fixtures.py`로 갱신한다.
- 오라클: `experiments/oracle/recorded/*.json`은 스크래치 가상 환경에서 fixture 앱을 실제로 import해
  resolver 순회·DRF 라우터·Flask url_map과 대조한 기록이다. 지금 출력의 정적 사실이 기록에서 검증된 사실과
  정확히 같아야 한다(정밀도 100%). 출력이 바뀌면 오라클을 다시 돌려 기록을 갱신해야 한다.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import FIXTURES, REPOSITORY, routes_document

#: (fixture 경로, golden 이름, 오라클 기록 이름) 목록이다.
TARGETS = (
    (FIXTURES / "django" / "drf-shop", "drf-shop.json"),
    (FIXTURES / "flask" / "blog-app", "blog-app.json"),
)

#: golden 문서 디렉터리다.
GOLDEN = Path(__file__).resolve().parent / "golden"

#: 오라클 기록 디렉터리다.
RECORDED = REPOSITORY / "experiments" / "oracle" / "recorded"


def _golden_text(document: dict[str, Any]) -> str:
    """문서의 `project`를 자리표시자로 바꿔 golden 비교용 문자열을 만든다.

    Args:
        document: 문서.

    Returns:
        키 정렬 JSON.
    """
    return json.dumps({**document, "project": "<project>"}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


@pytest.mark.parametrize(("fixture", "name"), TARGETS, ids=[name for _, name in TARGETS])
def test_golden_document(fixture: Path, name: str) -> None:
    """fixture 문서가 golden과 같다."""
    text = _golden_text(routes_document(fixture))
    path = GOLDEN / name
    if os.environ.get("PYTHOGRAPH_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def _compare_module() -> ModuleType:
    """오라클 비교 스크립트를 모듈로 읽는다(표준 라이브러리만 쓴다).

    Returns:
        `experiments/oracle/compare.py` 모듈.
    """
    spec = importlib.util.spec_from_file_location("oracle_compare", RECORDED.parent / "compare.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _summary(fact: dict[str, Any]) -> str:
    """오라클 기록과 같은 요약 키를 만든다.

    Args:
        fact: route-decl 사실.

    Returns:
        정렬 JSON 문자열.
    """
    keys = ("method", "channel", "pathAnchor", "trailingSlash", "paramConstraints", "catchAllPrefix", "order")
    summary = {key: fact[key] for key in keys if key in fact}
    summary["usr"] = fact.get("symbol", {}).get("usr")
    return json.dumps(summary, sort_keys=True)


@pytest.mark.parametrize(("fixture", "name"), TARGETS, ids=[name for _, name in TARGETS])
def test_recorded_oracle_verifies_every_static_fact(fixture: Path, name: str) -> None:
    """지금 출력의 정적 사실이 오라클이 검증한 사실과 정확히 같다(정밀도 100%)."""
    recorded = json.loads((RECORDED / name).read_text(encoding="utf-8"))
    document = routes_document(fixture)
    facts = [fact for fact in document["facts"] if not fact["dynamic"]]  # type: ignore[union-attr]
    current = sorted(_summary(fact) for fact in facts)
    probes = recorded["probes"]
    assert current == sorted(json.dumps(probe["fact"], sort_keys=True) for probe in probes)
    assert all(probe["verified"] for probe in probes)


@pytest.mark.parametrize(("name", "minimum"), [("drf-shop.json", 58), ("blog-app.json", 27)])
def test_recorded_oracle_recall(name: str, minimum: int) -> None:
    """기록한 오라클 재현율이 기대 이상이다(놓친 항목은 의도한 dynamic 한 건뿐)."""
    compare = _compare_module()
    recorded = json.loads((RECORDED / name).read_text(encoding="utf-8"))
    verified = [probe for probe in recorded["probes"] if probe["verified"]]
    covered, total, _ = compare.recall(compare.entry_rows(recorded["endpoints"]), verified, flagged=False)
    assert covered >= minimum
    assert total - covered <= 1
    assert compare.order_consistent(verified) in (True, None)
