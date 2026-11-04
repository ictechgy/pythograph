"""같은 점 경로의 재정의와 모듈 수준 재묶음: 모든 본문을 훑고, 섞인 묶음은 추측하지 않는다."""

from __future__ import annotations

import textwrap
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


def test_cross_module_rebinding_is_not_direct(make_project: MakeProject) -> None:
    """다른 모듈이 재묶음 이름을 import하거나 모듈 속성으로 읽어도 어느 묶음으로도 확정하지 않는다(리뷰 t18·t19)."""
    graph = graph_for(
        make_project,
        {
            "pkg/__init__.py": """
                try:
                    from pkg._speedups import parse
                except ImportError:
                    from pkg._pure import parse
            """,
            "pkg/_pure.py": """
                def parse():
                    return 2
            """,
            "pkg/_speedups.py": """
                def parse():
                    return 1
            """,
            "plugins/__init__.py": "",
            "plugins/alpha.py": """
                def run():
                    return 3
            """,
            "t2.py": """
                def fast():
                    return 0
                from plugins.alpha import run as fast
            """,
            "user.py": """
                import pkg
                import t2
                from pkg import parse
                from t2 import fast


                def a():
                    return parse()


                def b():
                    return pkg.parse()


                def c():
                    return fast()


                def d():
                    return t2.fast()
            """,
        },
    )
    edges = direct_edges(graph)
    for caller in ("user.py#a", "user.py#b", "user.py#c", "user.py#d"):
        assert not {target for source, target in edges if source == caller}, caller
        assert sum(direct_reasons(graph, caller).values()) == 1, (caller, direct_reasons(graph, caller))


def test_cross_module_rebinding_through_reexport_is_not_direct(make_project: MakeProject) -> None:
    """재묶음이 재수출 사슬의 중간 모듈에 있어도 확정하지 않는다."""
    graph = graph_for(
        make_project,
        {
            "pkg/__init__.py": """
                from pkg.api import parse
            """,
            "pkg/api.py": """
                try:
                    from pkg._speedups import parse
                except ImportError:
                    from pkg._pure import parse
            """,
            "pkg/_pure.py": """
                def parse():
                    return 2
            """,
            "pkg/_speedups.py": """
                def parse():
                    return 1
            """,
            "user.py": """
                from pkg import parse


                def a():
                    return parse()
            """,
        },
    )
    assert not {target for source, target in direct_edges(graph) if source == "user.py#a"}
    assert sum(direct_reasons(graph, "user.py#a").values()) == 1


def test_external_fallback_keeps_framework_base(make_project: MakeProject) -> None:
    """외부끼리의 대체 묶음(`ListView`/`object`)은 이전처럼 import를 기반 클래스로 써 프레임워크 정점을 지킨다."""
    graph = graph_for(
        make_project,
        {
            "manage.py": """
                import os
                os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
            """,
            "settings.py": """
                ROOT_URLCONF = "urls"
                INSTALLED_APPS = []
            """,
            "urls.py": """
                from django.urls import path
                from views import Items

                urlpatterns = [path("items/", Items.as_view())]
            """,
            "views.py": """
                try:
                    from django.views.generic import ListView
                except ImportError:
                    ListView = object


                def compute():
                    return []


                class Items(ListView):
                    def get_queryset(self):
                        return compute()
            """,
        },
    )
    nodes = graph.node_map()
    assert "views.py#Items.get" in nodes
    assert ("views.py#Items.get", "views.py#Items.get_queryset") in {(edge.source, edge.target) for edge in graph.edges}


def test_redefinition_locals_do_not_leak_between_bodies(make_project: MakeProject) -> None:
    """한 정의에서만 묶인 지역 이름은 다른 정의의 전역 참조를 가리지 않는다: 거짓 간선 없이 모름으로 센다(t17)."""
    graph = graph_for(
        make_project,
        {
            "m.py": """
                import sys


                def helper():
                    return 1


                class Alt:
                    pass


                if sys.platform == "win32":
                    def f(registry):
                        helper = Alt
                        registry.append(helper)
                else:
                    def f(registry):
                        return helper()


                class Box:
                    @property
                    def x(self):
                        return helper()

                    @x.setter
                    def x(self, helper):
                        self._h = helper
            """,
        },
    )
    # 첫 정의의 `helper = Alt`는 진짜 참조(`reference`)다. 둘째 정의의 `helper()`를 그 지역 값으로 풀어 `Alt` 생성
    # (`new`) 간선을 만들면 안 된다.
    alt_edges = [edge for edge in graph.edges if (edge.source, edge.target) == ("m.py#f", "m.py#Alt")]
    alt_kinds = {kind for edge in alt_edges for kind in edge.kinds}
    assert "new" not in alt_kinds
    # 어느 정의의 `helper`인지 모르므로 추측하지 않고 지역 값 모름(`local-value`)으로 센다.
    assert direct_reasons(graph, "m.py#f").get("local-value") == 1
    assert direct_reasons(graph, "m.py#Box.x").get("local-value") == 1


def test_non_typing_overload_bodies_are_scanned(make_project: MakeProject) -> None:
    """`typing`이 아닌 `overload`(plum·자체 등록 장식자)는 스텁이 아니라 구현이므로 모든 본문을 훑는다(t5·t16)."""
    body = """
        def to_int():
            return 1


        def to_str():
            return "1"


        @overload
        def convert(x: int):
            return to_int()


        @overload
        def convert(x: str):
            return to_str()


        @dispatch
        def convert(x):
            pass
    """
    graph = graph_for(
        make_project,
        {
            "reg.py": """
                def overload(fn):
                    return fn


                def dispatch(fn):
                    return fn
            """,
            "plum_user.py": "from plum import dispatch, overload\n" + textwrap.dedent(body),
            "own_user.py": "from reg import dispatch, overload\n" + textwrap.dedent(body),
        },
    )
    edges = direct_edges(graph)
    for module in ("plum_user.py", "own_user.py"):
        assert (f"{module}#convert", f"{module}#to_int") in edges, module
        assert (f"{module}#convert", f"{module}#to_str") in edges, module


def test_typing_overload_alias_stubs_are_excluded(make_project: MakeProject) -> None:
    """`import typing as t`의 `@t.overload` 스텁은 빼서 구현 매개변수의 주석 값을 지킨다."""
    graph = graph_for(
        make_project,
        {
            "conv.py": """
                import typing as t


                class Impl:
                    def run(self):
                        return 1


                @t.overload
                def conv(x: int) -> int: ...


                @t.overload
                def conv(x: str) -> str: ...


                def conv(x: Impl):
                    return x.run()
            """,
        },
    )
    assert ("conv.py#conv", "conv.py#Impl.run") in {(edge.source, edge.target) for edge in graph.edges}


def test_redefinition_merges_identical_parameters(make_project: MakeProject) -> None:
    """getter·setter의 같은 `self`는 한 값이라 `self` 메서드 호출이 타입 모르는 수신자가 되지 않는다."""
    graph = graph_for(
        make_project,
        {
            "acct.py": """
                class Account:
                    def _load(self):
                        return 1

                    def _store(self, value):
                        return value

                    @property
                    def balance(self):
                        return self._load()

                    @balance.setter
                    def balance(self, value):
                        self._store(value)
            """,
        },
    )
    targets = {edge.target for edge in graph.edges if edge.source == "acct.py#Account.balance"}
    assert {"acct.py#Account._load", "acct.py#Account._store"} <= targets
    assert "untyped-receiver" not in direct_reasons(graph, "acct.py#Account.balance")
