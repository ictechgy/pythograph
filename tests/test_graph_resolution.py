"""호출 그래프 해석 규칙을 합성 fixture(`fixtures/graph/resolution`)와 기존 fixture로 확인한다.

- 기대 간선: import(절대·상대·별칭·`__init__` 재수출·별 import), 생성자와 물려받은 `__init__`, C3 MRO의 `super()`,
  정확한 수신자의 상속 멤버 정점, 재정의 후보(candidate), 하위 클래스 전용 메서드, property 읽기·쓰기, 장식자·
  장식자 팩토리, 콜백·참조, 모듈 수준 인스턴스, 반환·매개변수 주석.
- 미해석 이유: 매개변수·getattr·동적 피호출·람다·컴프리헨션·풀지 못한 import·없는 이름·반복 변수.
- routes·schema가 내는 모든 `symbol.usr`가 그래프 정점이다(isthmus가 문자열로 잇는 조건).
- golden: 그래프 스냅샷 전체가 `tests/golden/graph-*.json`과 같다(`PYTHOGRAPH_UPDATE_GOLDEN=1`로 갱신).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pythograph.graph.build import build_graph
from pythograph.graph.model import CallGraph
from pythograph.source.project import Project
from tests.conftest import FIXTURES, GENERATED_AT, run_cli

#: 해석 fixture다.
RESOLUTION = FIXTURES / "graph" / "resolution"

#: golden 디렉터리다.
GOLDEN = Path(__file__).resolve().parent / "golden"

#: 모델 파일 접두사다.
MODELS = "app/core/models.py#"

#: 서비스 파일 접두사다.
SERVICES = "app/core/services.py#"


def graph_of(root: Path, include_tests: bool = False) -> CallGraph:
    """그래프를 만든다.

    Args:
        root: 프로젝트 경로.
        include_tests: 테스트 소스를 포함할지.

    Returns:
        그래프.
    """
    return build_graph(Project.open(root.resolve()), include_tests)


def edge_set(graph: CallGraph) -> set[tuple[str, str, tuple[str, ...], str]]:
    """간선을 (출발, 도착, 종류, 등급) 집합으로 바꾼다.

    Args:
        graph: 그래프.

    Returns:
        집합.
    """
    return {(edge.source, edge.target, edge.kinds, edge.evidence) for edge in graph.edges}


EXPECTED_EDGES = [
    # C3: Diamond(Left, Right)의 super().__init__은 Left·Right를 건너 Base.__init__이다.
    (f"{MODELS}Diamond.__init__", f"{MODELS}Base.__init__", ("call",), "direct"),
    # cls(...)는 하위 클래스일 수 있는 생성이다.
    (f"{MODELS}Base.create", f"{MODELS}Base.__init__", ("new",), "direct"),
    (f"{MODELS}Base.create", f"{MODELS}Diamond.__init__", ("new",), "candidate"),
    # self 호출: 정적 정의(direct)와 하위 클래스 재정의(candidate), 하위 클래스 전용 메서드.
    (f"{MODELS}Base.describe", f"{MODELS}Base.label", ("call",), "direct"),
    (f"{MODELS}Base.describe", f"{MODELS}Left.label", ("call",), "candidate"),
    (f"{MODELS}Base.describe", f"{MODELS}Right.suffix", ("call",), "candidate"),
    (f"{MODELS}Base.render", f"{MODELS}Left.template", ("call",), "candidate"),
    (f"{MODELS}Left.label", f"{MODELS}Base.label", ("call",), "direct"),
    # 정확한 수신자의 상속 멤버 정점: 정의로 가는 inherit와 수신자 MRO로 다시 푼 self 호출.
    (f"{MODELS}Diamond.describe", f"{MODELS}Base.describe", ("inherit",), "direct"),
    (f"{MODELS}Diamond.describe", f"{MODELS}Diamond.label", ("call",), "direct"),
    (f"{MODELS}Diamond.label", f"{MODELS}Left.label", ("inherit",), "direct"),
    (f"{MODELS}Diamond.suffix", f"{MODELS}Right.suffix", ("inherit",), "direct"),
    (f"{MODELS}Plain.__init__", f"{MODELS}Base.__init__", ("inherit",), "direct"),
    (f"{MODELS}Diamond", f"{MODELS}Left", ("inherit",), "direct"),
    # 생성자·정확한 인스턴스·정적 메서드·장식자.
    (f"{SERVICES}exact_calls", f"{MODELS}Diamond.__init__", ("new",), "direct"),
    (f"{SERVICES}exact_calls", f"{MODELS}Plain.__init__", ("new",), "direct"),
    (f"{SERVICES}exact_calls", f"{MODELS}Plain.describe", ("call",), "direct"),
    (f"{SERVICES}exact_calls", f"{MODELS}Diamond.describe", ("call",), "direct"),
    (f"{SERVICES}exact_calls", f"{MODELS}Base.normalize", ("call",), "direct"),
    (f"{SERVICES}exact_calls", "app/helpers.py#traced", ("decorator",), "direct"),
    (f"{SERVICES}inexact_calls", "app/helpers.py#cached", ("decorator",), "direct"),
    (f"{SERVICES}<module>", "app/helpers.py#cached", ("call",), "direct"),
    # 주석으로 아는 수신자와 반환 주석.
    (f"{SERVICES}inexact_calls", f"{MODELS}Base.describe", ("call",), "direct"),
    (f"{SERVICES}inexact_calls", f"{MODELS}build", ("call",), "direct"),
    (f"{SERVICES}inexact_calls", f"{MODELS}Left.label", ("call",), "candidate"),
    # 콜백·참조, `__init__` 재수출(`from app import slugify`), 상대 import 별칭.
    (f"{SERVICES}callbacks_and_references", f"{SERVICES}exact_calls", ("callback",), "direct"),
    (f"{SERVICES}callbacks_and_references", f"{SERVICES}inexact_calls", ("reference",), "direct"),
    (f"{SERVICES}callbacks_and_references", "app/helpers.py#slugify", ("callback", "reference"), "direct"),
    (f"{SERVICES}callbacks_and_references", "app/helpers.py#dump", ("reference",), "direct"),
    (f"{SERVICES}callbacks_and_references", f"{SERVICES}Registry.register", ("call",), "direct"),
    # getattr 리터럴, property 읽기·쓰기, 중첩 함수, 별 import, 외부 기반 클래스의 자기 메서드.
    (f"{SERVICES}dynamic_calls", f"{SERVICES}Registry.fire", ("call",), "direct"),
    (f"{SERVICES}property_use", f"{MODELS}Base.title", ("call",), "direct"),
    (f"{SERVICES}nested", f"{SERVICES}nested.inner", ("call",), "direct"),
    (f"{SERVICES}nested.inner", f"{SERVICES}exact_calls", ("call",), "direct"),
    ("app/plugins/extra.py#use_star", "app/helpers.py#slugify", ("call",), "direct"),
    ("app/plugins/extra.py#Counter.bump", "app/plugins/extra.py#Counter.total", ("call",), "direct"),
    ("app/helpers.py#traced", "app/helpers.py#traced.wrapper", ("reference",), "direct"),
]

EXPECTED_REASONS = {
    f"{SERVICES}dynamic_calls": {
        "dynamic-callee": 2,
        "getattr": 1,
        "parameter": 1,
        "unresolved-import": 1,
        "unresolved-name": 1,
        "untyped-receiver": 2,
    },
    f"{SERVICES}Registry.fire": {"local-value": 1},
    f"{SERVICES}inexact_calls": {"overridden-method": 1},
    f"{MODELS}Base.render": {"subclass-method": 1},
    f"{MODELS}Base.describe": {"overridden-method": 2},
    "app/helpers.py#traced.wrapper": {"parameter": 1},
}


#: bound 간선으로 이은 호출 수다(`build()`는 `Plain("p")`만 돌려준다 → `Plain.label`).
BOUND_LINKED = {f"{SERVICES}inexact_calls": 1}


@pytest.fixture(scope="module")
def resolution() -> CallGraph:
    """해석 fixture 그래프다.

    Returns:
        그래프.
    """
    return graph_of(RESOLUTION)


@pytest.mark.parametrize("edge", EXPECTED_EDGES, ids=lambda edge: f"{edge[0]}->{edge[1]}")
def test_expected_edges(resolution: CallGraph, edge: tuple[str, str, tuple[str, ...], str]) -> None:
    """기대 간선이 있다."""
    assert edge in edge_set(resolution)


def test_unresolved_reasons(resolution: CallGraph) -> None:
    """미해석 이유와 모드별 수가 기대와 같다(partial은 candidates에서, bound로 이은 호출은 bound에서 빠진다)."""
    nodes = resolution.node_map()
    for node_id, reasons in EXPECTED_REASONS.items():
        assert nodes[node_id].reasons == reasons, node_id
        partial = reasons.get("overridden-method", 0) + reasons.get("subclass-method", 0)
        total = sum(reasons.values())
        linked = BOUND_LINKED.get(node_id, 0)
        assert nodes[node_id].unresolved == {"direct": total, "bound": total - linked, "candidates": total - partial}


def test_no_guessed_edges(resolution: CallGraph) -> None:
    """모르는 호출에는 간선이 없다(매개변수·getattr 동적 이름·반복 변수)."""
    targets = {edge.target for edge in resolution.edges if edge.source == f"{SERVICES}Registry.fire"}
    assert targets == set()
    assert f"{MODELS}Base.render" not in {
        edge.target for edge in resolution.edges if edge.source == f"{SERVICES}dynamic_calls"
    }


def test_test_sources_are_optional() -> None:
    """테스트 소스는 기본으로 정점이 아니고 `--include-tests`로 더한다."""
    assert not any(node.id.startswith("tests/") for node in graph_of(RESOLUTION).nodes)
    included = graph_of(RESOLUTION, include_tests=True)
    assert ("tests/test_services.py#test_exact", f"{SERVICES}exact_calls", ("call",), "direct") in edge_set(included)


def _golden_usrs(name: str) -> set[str]:
    """golden 문서 사실의 usr 집합이다.

    Args:
        name: golden 파일 이름.

    Returns:
        usr 집합.
    """
    facts = json.loads((GOLDEN / name).read_text(encoding="utf-8"))["facts"]
    return {fact["symbol"]["usr"] for fact in facts if "usr" in fact.get("symbol", {})}


@pytest.mark.parametrize(
    ("fixture", "golden"),
    [
        ("django/drf-shop", "drf-shop.json"),
        ("flask/blog-app", "blog-app.json"),
        ("persistence/django-shop", "persistence-django-shop.json"),
        ("persistence/flask-blog", "persistence-flask-blog.json"),
        ("persistence-naming/django-library", "persistence-django-library.json"),
        ("persistence-naming/fsa-shop", "persistence-fsa-shop.json"),
        ("persistence-naming/sa-catalog", "persistence-sa-catalog.json"),
    ],
)
def test_fact_usrs_are_graph_nodes(fixture: str, golden: str) -> None:
    """routes·schema의 모든 `symbol.usr`(상속 핸들러 포함)가 그래프 정점이다."""
    ids = {node.id for node in graph_of(FIXTURES / fixture).nodes}
    assert _golden_usrs(golden) <= ids


def test_view_handlers_link_framework_hooks() -> None:
    """프레임워크가 구현한 핸들러(ListView.get)가 프로젝트 훅(get_queryset)과 클래스 속성에 닿는다."""
    edges = edge_set(graph_of(FIXTURES / "django" / "drf-shop"))
    view = "catalog/views.py#ProductListView"
    assert (f"{view}.get", f"{view}.get_queryset", ("framework",), "direct") in edges
    assert (f"{view}.get", view, ("attribute",), "direct") in edges
    assert ("catalog/views.py#BrandView.get", "catalog/views.py#ReadOnlyBaseView.get", ("inherit",), "direct") in edges


@pytest.mark.parametrize(
    ("fixture", "name"), [(RESOLUTION, "graph-resolution.json"), (FIXTURES / "e2e" / "shop-api", "graph-shop-api.json")]
)
def test_golden_snapshot(fixture: Path, name: str) -> None:
    """그래프 스냅샷이 golden과 같다."""
    code, out, err = run_cli(
        ["graph", "--project", str(fixture), "--generated-at", GENERATED_AT, "--revision", "golden"]
    )
    assert code == 0, err
    text = json.dumps({**json.loads(out), "project": "<project>"}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path = GOLDEN / name
    if os.environ.get("PYTHOGRAPH_UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")
