"""같은 점 경로의 재정의와 모듈 수준 재묶음: 모든 본문을 훑고, 섞인 묶음은 추측하지 않는다."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pythograph.graph.build import build_graph
from pythograph.graph.model import CallGraph
from pythograph.source.project import Project

#: 합성 프로젝트 도우미 타입이다.
MakeProject = Callable[[dict[str, str]], Path]


def graph_for(make_project: MakeProject, files: dict[str, str]) -> CallGraph:
    """합성 프로젝트의 그래프를 만든다.

    Args:
        make_project: 합성 프로젝트 도우미.
        files: 상대 경로 → 내용.

    Returns:
        그래프.
    """
    return build_graph(Project.open(make_project(files)), False)


def direct_edges(graph: CallGraph) -> set[tuple[str, str]]:
    """direct 간선을 (출발, 도착) 집합으로 바꾼다.

    Args:
        graph: 그래프.

    Returns:
        집합.
    """
    return {(edge.source, edge.target) for edge in graph.edges if edge.evidence == "direct"}


def direct_reasons(graph: CallGraph, node_id: str) -> dict[str, int]:
    """정점의 direct 미해석 이유를 돌려준다.

    Args:
        graph: 그래프.
        node_id: 정점 id.

    Returns:
        이유 → 수.
    """
    return graph.node_map()[node_id].reasons


def test_property_setter_body_is_scanned(make_project: MakeProject) -> None:
    """property setter 본문의 호출도 같은 정점의 간선이 된다(getter만 보던 결함의 회귀)."""
    graph = graph_for(
        make_project,
        {
            "prop.py": """
                def validate(v):
                    return v


                def audit(v):
                    return v


                class Account:
                    @property
                    def balance(self):
                        return audit(self._balance)

                    @balance.setter
                    def balance(self, value):
                        self._balance = validate(value)
            """,
        },
    )
    edges = direct_edges(graph)
    assert ("prop.py#Account.balance", "prop.py#audit") in edges
    assert ("prop.py#Account.balance", "prop.py#validate") in edges


def test_overload_implementation_body_is_scanned(make_project: MakeProject) -> None:
    """`@overload` 스텁 뒤 실제 구현의 호출이 간선이 되고, 스텁의 매개변수 주석은 구현 매개변수를 가리지 않는다."""
    graph = graph_for(
        make_project,
        {
            "conv.py": """
                from typing import overload


                class Box:
                    def open(self):
                        return 1


                def real_impl(x):
                    return x


                @overload
                def conv(x: int) -> int: ...
                @overload
                def conv(x: str) -> str: ...
                def conv(x: Box):
                    x.open()
                    return real_impl(x)
            """,
        },
    )
    edges = direct_edges(graph)
    assert ("conv.py#conv", "conv.py#real_impl") in edges
    assert ("conv.py#conv", "conv.py#Box.open") in edges


def test_singledispatch_registrations_are_all_scanned(make_project: MakeProject) -> None:
    """`@process.register def _`를 여러 번 정의하면 모든 등록 본문의 호출이 `_` 정점에 실린다."""
    graph = graph_for(
        make_project,
        {
            "sd.py": """
                from functools import singledispatch


                def handle_int(x):
                    return x


                def handle_str(x):
                    return x


                @singledispatch
                def process(x):
                    return x


                @process.register
                def _(x: int):
                    return handle_int(x)


                @process.register
                def _(x: str):
                    return handle_str(x)
            """,
        },
    )
    edges = direct_edges(graph)
    assert ("sd.py#_", "sd.py#handle_int") in edges
    assert ("sd.py#_", "sd.py#handle_str") in edges


def test_conditional_definitions_merge_bodies(make_project: MakeProject) -> None:
    """모듈·클래스·함수 안 조건부 정의는 한 정점으로 합치고 두 본문의 호출을 모두 싣는다(재묶음으로 모르지 않는다)."""
    graph = graph_for(
        make_project,
        {
            "plat.py": """
                import sys


                def win_impl():
                    return 1


                def posix_impl():
                    return 2


                if sys.platform == "win32":
                    def plat():
                        return win_impl()
                else:
                    def plat():
                        return posix_impl()


                class Runner:
                    if sys.platform == "win32":
                        def start(self):
                            return win_impl()
                    else:
                        def start(self):
                            return posix_impl()


                def outer():
                    if sys.platform == "win32":
                        def helper():
                            return win_impl()
                    else:
                        def helper():
                            return posix_impl()
                    return helper()


                def main():
                    plat()
                    return Runner().start()
            """,
        },
    )
    edges = direct_edges(graph)
    assert {("plat.py#plat", "plat.py#win_impl"), ("plat.py#plat", "plat.py#posix_impl")} <= edges
    assert {("plat.py#Runner.start", "plat.py#win_impl"), ("plat.py#Runner.start", "plat.py#posix_impl")} <= edges
    assert {("plat.py#outer.helper", "plat.py#win_impl"), ("plat.py#outer.helper", "plat.py#posix_impl")} <= edges
    assert ("plat.py#main", "plat.py#plat") in edges
    assert ("plat.py#main", "plat.py#Runner.start") in edges
    assert ("plat.py#outer", "plat.py#outer.helper") in edges


def test_mixed_module_rebinding_is_not_a_direct_target(make_project: MakeProject) -> None:
    """모듈 수준에서 정의와 import(또는 import 둘)가 같은 이름을 다시 묶으면 어느 쪽도 direct로 확정하지 않는다."""
    graph = graph_for(
        make_project,
        {
            "plugins/__init__.py": "",
            "plugins/alpha.py": """
                def run():
                    return 3


                def other():
                    return 4
            """,
            "t1.py": """
                try:
                    from plugins.alpha import run as fast
                except ImportError:
                    def fast():
                        return 0


                def call_t1():
                    return fast()
            """,
            "t2.py": """
                def fast():
                    return 0
                from plugins.alpha import run as fast


                def call_t2():
                    return fast()
            """,
            "t5.py": """
                try:
                    from plugins.alpha import run as fast
                except ImportError:
                    from plugins.alpha import other as fast


                def call_t5():
                    return fast()
            """,
        },
    )
    edges = direct_edges(graph)
    for caller in ("t1.py#call_t1", "t2.py#call_t2", "t5.py#call_t5"):
        assert not {target for source, target in edges if source == caller}, caller
        assert direct_reasons(graph, caller).get("local-value") == 1, caller


def test_same_target_rebinding_stays_direct(make_project: MakeProject) -> None:
    """두 import가 같은 대상을 묶으면 값이 하나이므로 direct로 남는다."""
    graph = graph_for(
        make_project,
        {
            "plugins/__init__.py": "",
            "plugins/alpha.py": """
                def run():
                    return 3
            """,
            "same.py": """
                try:
                    from plugins.alpha import run
                except ImportError:
                    from plugins.alpha import run


                def call_same():
                    return run()
            """,
        },
    )
    assert ("same.py#call_same", "plugins/alpha.py#run") in direct_edges(graph)


def test_external_fallback_imports_stay_external(make_project: MakeProject) -> None:
    """외부 모듈끼리의 import 대체(`json`/`simplejson`)는 프로젝트 대상이 없으므로 미해석으로 세지 않는다."""
    graph = graph_for(
        make_project,
        {
            "codec.py": """
                try:
                    import simplejson as json
                except ImportError:
                    import json


                def dump(value):
                    return json.dumps(value)
            """,
        },
    )
    assert direct_reasons(graph, "codec.py#dump") == {}


def test_package_outside_module_roots_is_unresolved_import(make_project: MakeProject) -> None:
    """모듈 루트 밖 패키지(`backend/app`, `manage.py` 없음)로 가는 절대 import는 외부가 아니라 `unresolved-import`다."""
    graph = graph_for(
        make_project,
        {
            "backend/app/__init__.py": "",
            "backend/app/models.py": """
                def load():
                    return 1
            """,
            "backend/app/api.py": """
                from app.models import load
                import app.models as models


                def items():
                    load()
                    return models.load()
            """,
            "backend/app/sub/__init__.py": "",
            "client.py": """
                import requests


                def fetch():
                    return requests.get("/x")
            """,
        },
    )
    # `load()`는 풀지 못한 import, `models.load()`는 수신자 모듈을 모르므로 타입 모르는 수신자로 센다.
    assert direct_reasons(graph, "backend/app/api.py#items") == {"unresolved-import": 1, "untyped-receiver": 1}
    assert direct_reasons(graph, "client.py#fetch") == {}
