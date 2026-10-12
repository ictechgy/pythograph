"""같은 이름의 패키지·모듈이 공존할 때 실제 import 대상만 직접 간선으로 잇는다."""

from __future__ import annotations

from collections.abc import Callable
from importlib.machinery import FileFinder, SourceFileLoader
from pathlib import Path

import pytest

from pythograph.graph.build import build_graph
from pythograph.source.project import Project


@pytest.mark.parametrize("prefix", ["", "src/", "outer/"])
def test_regular_package_precedes_same_name_module(make_project: Callable[[dict[str, str]], Path], prefix: str) -> None:
    """표준 import finder와 대조하되 fixture 코드는 실행하지 않는다."""
    root = make_project(
        {
            f"{prefix}choice.py": "def target():\n    pass\n",
            f"{prefix}choice/__init__.py": "def target():\n    pass\n",
            "outer/__init__.py": "",
        }
    )
    finder = FileFinder(str(root / prefix), (SourceFileLoader, [".py"]))
    spec = finder.find_spec("choice")
    assert spec is not None and spec.origin is not None
    expected = Path(spec.origin).relative_to(root).as_posix()
    assert expected == f"{prefix}choice/__init__.py"
    name = "outer.choice" if prefix == "outer/" else "choice"
    assert Project.open(root).resolve_module(name) == expected


def test_namespace_directory_does_not_hide_module(make_project: Callable[[dict[str, str]], Path]) -> None:
    """__init__.py 없는 디렉터리는 같은 루트의 일반 모듈보다 먼저 선택하지 않는다."""
    root = make_project({"choice.py": "", "choice/child.py": ""})
    assert Project.open(root).resolve_module("choice") == "choice.py"


def test_imported_call_targets_package_not_shadowed_module(
    make_project: Callable[[dict[str, str]], Path],
) -> None:
    """동일 이름의 모듈로 향하는 거짓 direct 간선과 실제 패키지 호출의 누락을 막는다."""
    root = make_project(
        {
            "choice.py": "def target():\n    pass\n",
            "choice/__init__.py": "def target():\n    pass\n",
            "caller.py": "from choice import target\ndef run():\n    target()\n",
        }
    )
    graph = build_graph(Project.open(root), False)
    calls = {(edge.source, edge.target, edge.evidence) for edge in graph.edges if "call" in edge.kinds}
    assert ("caller.py#run", "choice/__init__.py#target", "direct") in calls
    assert ("caller.py#run", "choice.py#target", "direct") not in calls


def test_search_root_order_precedes_package_kind(make_project: Callable[[dict[str, str]], Path]) -> None:
    """뒤 검색 루트의 패키지가 앞 검색 루트의 일반 모듈을 가리지 않는다."""
    root = make_project({"choice.py": "", "src/choice/__init__.py": ""})
    assert Project.open(root).resolve_module("choice") == "choice.py"
