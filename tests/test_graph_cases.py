"""호출 그래프의 경계 사례: 프레임워크 디스패치, 계층 근사, 이름 필터, 지역 이름 규칙, 한계 문구."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pythograph.graph.build import build_graph
from pythograph.graph.model import CallGraph
from pythograph.source.project import Project

#: 합성 프로젝트 도우미 타입이다.
MakeProject = Callable[[dict[str, str]], Path]


def graph_for(make_project: MakeProject, files: dict[str, str], include_tests: bool = False) -> CallGraph:
    """합성 프로젝트의 그래프를 만든다.

    Args:
        make_project: 합성 프로젝트 도우미.
        files: 상대 경로 → 내용.
        include_tests: 테스트 소스 포함 여부.

    Returns:
        그래프.
    """
    return build_graph(Project.open(make_project(files)), include_tests)


def edges(graph: CallGraph) -> set[tuple[str, str, str]]:
    """간선을 (출발, 도착, 등급) 집합으로 바꾼다(종류는 뺀다).

    Args:
        graph: 그래프.

    Returns:
        집합.
    """
    return {(edge.source, edge.target, edge.evidence) for edge in graph.edges}


def kinds(graph: CallGraph, source: str, target: str) -> tuple[str, ...]:
    """두 정점 사이 direct 간선의 종류다.

    Args:
        graph: 그래프.
        source: 출발.
        target: 도착.

    Returns:
        종류(없으면 빈 튜플).
    """
    return next((edge.kinds for edge in graph.edges if (edge.source, edge.target) == (source, target)), ())


def test_django_dispatch_path_hooks(make_project: MakeProject) -> None:
    """디스패치 경로 훅(dispatch 재정의, 믹스인 뒤 프로젝트 클래스의 super().dispatch, __init__)이 핸들러에 닿는다."""
    graph = graph_for(
        make_project,
        {
            "views.py": """
                from django.contrib.auth.mixins import LoginRequiredMixin
                from django.views import View


                class Audited:
                    def dispatch(self, request, *args, **kwargs):
                        return record(request)


                def record(request):
                    return request


                class Page(LoginRequiredMixin, Audited, View):
                    def __init__(self, **kwargs):
                        super().__init__(**kwargs)

                    def get(self, request):
                        return request


                class Child(Page):
                    pass
            """
        },
    )
    assert kinds(graph, "views.py#Page.get", "views.py#Audited.dispatch") == ("dispatch",)
    assert kinds(graph, "views.py#Page.get", "views.py#Page.__init__") == ("dispatch",)
    assert kinds(graph, "views.py#Child.get", "views.py#Page.get") == ("inherit",)
    assert kinds(graph, "views.py#Child.get", "views.py#Audited.dispatch") == ("dispatch",)
    assert kinds(graph, "views.py#Child.get", "views.py#Child.__init__") == ("dispatch",)


def test_drf_callback_attributes_and_serializer_hooks(make_project: MakeProject) -> None:
    """프로젝트 클래스 값 속성은 framework-callback으로 세고, 정확한 직렬화기 수신자는 프레임워크 훅을 잇는다."""
    graph = graph_for(
        make_project,
        {
            "api.py": """
                from rest_framework import permissions, serializers, viewsets
                from rest_framework.views import APIView


                class IsOwner(permissions.BasePermission):
                    pass


                class ItemSerializer(serializers.Serializer):
                    def create(self, validated_data):
                        return validated_data


                class ItemView(APIView):
                    permission_classes = [IsOwner]
                    serializer_class = ItemSerializer

                    def post(self, request):
                        serializer = self.serializer_class(data=request.data)
                        serializer.is_valid()
                        serializer.save()
                        return serializer.data


                class Items(viewsets.ModelViewSet):
                    serializer_class = ItemSerializer
                    queryset = None
            """
        },
    )
    nodes = graph.node_map()
    assert nodes["api.py#ItemView.post"].reasons == {"framework-callback": 1}
    assert kinds(graph, "api.py#ItemView.post", "api.py#ItemView") == ("attribute",)
    assert kinds(graph, "api.py#ItemView.post", "api.py#ItemSerializer.save") == ("call",)
    assert kinds(graph, "api.py#ItemSerializer.save", "api.py#ItemSerializer.create") == ("framework",)
    assert kinds(graph, "api.py#ItemView.post", "api.py#ItemSerializer.data") == ("call",)
    assert nodes["api.py#Items.list"].reasons == {"framework-callback": 1}
    assert kinds(graph, "api.py#Items.list", "api.py#Items") == ("attribute",)


def test_flask_method_view_handlers(make_project: MakeProject) -> None:
    """Flask MethodView 핸들러와 View.dispatch_request 재정의가 정점이 된다."""
    graph = graph_for(
        make_project,
        {
            "views.py": """
                from flask.views import MethodView, View


                class Base(MethodView):
                    def get(self):
                        return self.load()

                    def load(self):
                        return 1


                class Item(Base):
                    def load(self):
                        return 2


                class Settings(View):
                    methods = ["GET"]

                    def dispatch_request(self):
                        return "ok"
            """
        },
    )
    nodes = graph.node_map()
    assert nodes["views.py#Item.get"].kind == "inherited-method"
    assert kinds(graph, "views.py#Item.get", "views.py#Item.load") == ("call",)
    assert ("views.py#Base.get", "views.py#Item.load", "candidate") in edges(graph)
    assert nodes["views.py#Settings.dispatch_request"].kind == "method"


def test_inconsistent_and_cyclic_hierarchies_are_approximated(make_project: MakeProject) -> None:
    """C3가 실패하거나 순환하는 계층은 근사하고 한계로 밝힌다."""
    graph = graph_for(
        make_project,
        {
            "bad.py": """
                class A:
                    def run(self):
                        return 1


                class B(A):
                    pass


                class C(A, B):
                    def go(self):
                        return self.run()


                class X(Y):
                    pass


                class Y(X):
                    def go(self):
                        return self.missing()
            """
        },
    )
    assert graph.statistics["mroApproximated"] == 2
    assert any(line.startswith("mro-approximated:") for line in graph.limitations["direct"])
    assert ("bad.py#C.go", "bad.py#A.run", "direct") in edges(graph)
    assert graph.node_map()["bad.py#Y.go"].reasons == {"unknown-base": 1}


def test_getattr_hook_disables_name_filter(make_project: MakeProject) -> None:
    """`__getattr__`을 정의한 클래스가 있으면 모르는 수신자의 어떤 호출도 외부로 확정하지 않는다."""
    files = {
        "a.py": """
            def use(value):
                return value.anything()
        """
    }
    assert graph_for(make_project, files).node_map()["a.py#use"].reasons == {}
    files["b.py"] = """
        class Proxy:
            def __getattr__(self, name):
                return name
    """
    assert graph_for(make_project, files).node_map()["a.py#use"].reasons == {"untyped-receiver": 1}


def test_scope_rules(make_project: MakeProject) -> None:
    """지역 이름 규칙: global·nonlocal·바다코끼리·풀기·with·except·match·함수 안 import·클래스 본문 이름."""
    graph = graph_for(
        make_project,
        {
            "pkg/__init__.py": "",
            "pkg/tools.py": """
                def helper():
                    return 1
            """,
            "pkg/main.py": """
                import os
                from . import tools

                target = tools.helper


                def outer():
                    counter = 0
                    first, second = tools.helper, tools.helper

                    def inner():
                        nonlocal counter
                        global target
                        counter += 1
                        return target()

                    if (found := tools.helper):
                        found()
                    first()
                    with open("x") as handle:
                        handle.close()
                    try:
                        pass
                    except ValueError as error:
                        error.args
                    match counter:
                        case {"k": captured, **rest}:
                            captured()
                        case [*items]:
                            items()
                        case other:
                            other()
                    from .tools import helper as local_helper
                    from os import path
                    import json as js
                    local_helper()
                    js.dumps(path.sep)
                    return inner


                class Holder:
                    alias = tools.helper
                    run = alias

                    def call(self):
                        return self.run()


                async def waiter(value):
                    await value.close()
                    return os.getcwd()
            """,
        },
    )
    all_edges = edges(graph)
    assert ("pkg/main.py#outer.inner", "pkg/tools.py#helper", "direct") in all_edges
    assert ("pkg/main.py#outer", "pkg/tools.py#helper", "direct") in all_edges
    assert ("pkg/main.py#Holder.call", "pkg/main.py#Holder", "direct") in all_edges
    assert ("pkg/main.py#Holder.call", "pkg/tools.py#helper", "direct") in all_edges
    reasons = graph.node_map()["pkg/main.py#outer"].reasons
    assert reasons == {"local-value": 4}


def test_annotations_and_special_calls(make_project: MakeProject) -> None:
    """주석 변형(Optional·Annotated·문자열·잘못된 문자열), `type(self)`, `cls()`, 외부 결과 호출."""
    graph = graph_for(
        make_project,
        {
            "m.py": """
                import typing
                from typing import Annotated, Optional


                class Thing:
                    def ping(self):
                        return 1

                    def clone(self):
                        return type(self)().ping()

                    @classmethod
                    def make(cls):
                        return cls().ping()


                class Other(Thing):
                    def ping(self):
                        return 2


                def a(
                    x: Optional[Thing], y: "Annotated[Thing, 1]", z: "not valid(", w: typing.List[int], v: None | Thing
                ):
                    x.ping()
                    y.ping()
                    v.ping()
                    z.ping()
                    return w


                def b(factory: typing.Callable[[], int]):
                    return dict().get("k")()
            """
        },
    )
    all_edges = edges(graph)
    assert ("m.py#a", "m.py#Thing.ping", "direct") in all_edges
    assert ("m.py#a", "m.py#Other.ping", "candidate") in all_edges
    assert ("m.py#Thing.clone", "m.py#Other.ping", "candidate") in all_edges
    assert ("m.py#Thing.make", "m.py#Thing", "direct") in all_edges
    nodes = graph.node_map()
    assert nodes["m.py#a"].reasons == {"overridden-method": 3, "untyped-receiver": 1}
    assert nodes["m.py#b"].reasons == {"dynamic-callee": 1}


def test_limitations_for_scan_gaps(make_project: MakeProject) -> None:
    """읽지 못한 파일·계산된 setattr·BOM 위치·테스트 모듈 참조를 다룬다."""
    root = make_project(
        {
            "ok.py": """
                import tests.helpers as th
                from tests.helpers import Fake


                def configure(target, key, value):
                    setattr(target, key, value)
                    setattr(target, "named", value)
                    return Fake().run() or th.Fake
            """,
            "tests/helpers.py": """
                class Fake:
                    def run(self):
                        return 1
            """,
        }
    )
    (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (root / "bom.py").write_bytes(b"\xef\xbb\xbfdef first():\n    return 1\n")
    graph = build_graph(Project.open(root), include_tests=False)
    assert graph.statistics["unparsedFiles"] == 1
    assert graph.statistics["dynamicAttributeWrites"] == 1
    lines = graph.limitations["candidates"]
    assert any(line.startswith("unparsed-files:") for line in lines)
    assert any(line.startswith("dynamic-attribute-writes:") for line in lines)
    assert graph.node_map()["bom.py#first"].location.column == 4
    assert graph.node_map()["ok.py#configure"].reasons == {"excluded-source": 1}
