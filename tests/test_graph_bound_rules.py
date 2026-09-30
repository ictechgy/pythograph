"""`bound` 흐름·열린 자리 규칙의 세부 사례(한 모듈짜리 합성 프로그램).

각 사례는 `app.py`의 `site` 함수(또는 지정한 정점) 안 호출 하나가 `bound`로 이어지는지, 아니면 어떤 이유로 열리는지를
확인한다. 저장소 클래스는 `tests/test_graph_bound.py`의 `REPOS`와 같다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.graph import flow as flow_module
from pythograph.graph.build import build_graph
from pythograph.graph.model import CallGraph
from pythograph.source.project import Project
from tests.test_graph_bound import REPOS

#: 합성 프로젝트 도우미 타입이다.
MakeProject = Callable[[dict[str, str]], Path]

#: `SqlRepo.save` 정점이다.
SQL = "repos.py#SqlRepo.save"

#: `MemRepo.save` 정점이다.
MEM = "repos.py#MemRepo.save"


def build(make_project: MakeProject, app: str, **extra: str) -> CallGraph:
    """`repos.py`와 `app.py`(와 추가 파일)로 그래프를 만든다.

    Args:
        make_project: 합성 프로젝트 도우미.
        app: `app.py` 내용.
        extra: 추가 파일(키의 `__`는 `/`, `_py` 끝은 `.py`).

    Returns:
        그래프.
    """
    files = {"repos.py": REPOS, "app.py": "from repos import MemRepo, Plain, Repo, SqlRepo\n" + app}
    for key, content in extra.items():
        files[key.replace("__", "/").removesuffix("_py") + ".py"] = content
    return build_graph(Project.open(make_project(files)), False)


def outcome(graph: CallGraph, node: str = "app.py#site") -> tuple[set[str], dict[str, int]]:
    """정점의 `bound` 대상과 그래프 전체의 열린 이유다.

    Args:
        graph: 그래프.
        node: 정점 id.

    Returns:
        (대상, 열린 이유).
    """
    targets = {edge.target for edge in graph.edges if edge.source == node and edge.evidence == "bound"}
    bound = graph.statistics["boundDispatch"]
    assert isinstance(bound, dict)
    return targets, dict(bound["open"])


#: (이름, app.py, 기대 대상, 기대 열린 이유) 사례다.
CASES: list[tuple[str, str, set[str], dict[str, int]]] = [
    (
        "global-writes",
        """
import os

repo = None
if os.environ.get("X"):
    repo = SqlRepo()


def configure():
    global repo
    repo = MemRepo()


def site():
    return repo.save(1)
""",
        {SQL, MEM},
        {},
    ),
    (
        "class-attribute-default-and-instance-write",
        """
class Holder:
    repo = None

    def __init__(self):
        self.repo = SqlRepo()

    def site(self):
        return self.repo.save(1)
""",
        {SQL},
        {},
    ),
    (
        "class-object-write",
        """
class Holder:
    repo = SqlRepo()

    def site(self):
        return self.repo.save(1)


def configure():
    Holder.repo = MemRepo()
""",
        {SQL, MEM},
        {},
    ),
    (
        "descriptor-class-attribute",
        """
class Lazy:
    def __get__(self, instance, owner):
        return MemRepo()

    def save(self, item):
        return item


class Holder:
    repo = Lazy()

    def site(self):
        return self.repo.save(1)
""",
        set(),
        {},
    ),
    (
        "descriptor-written-to-class",
        """
class Lazy:
    def __get__(self, instance, owner):
        return MemRepo()

    def save(self, item):
        return item


class Holder:
    repo = None

    def site(self):
        return self.repo.save(1)


def configure():
    Holder.repo = Lazy()
""",
        set(),
        {"descriptor": 1},
    ),
    (
        "property-is-not-data",
        """
class Holder:
    @property
    def repo(self):
        return SqlRepo()

    def site(self, flag):
        value = self.repo
        if flag:
            value = MemRepo()
        return value.save(1)
""",
        set(),
        {"not-data-attribute": 1},
    ),
    (
        "keyword-only-and-positional-only",
        """
def store(first, /, *, repo):
    return repo.save(first)


def site():
    return None


def main():
    store(1, repo=SqlRepo())
""",
        set(),
        {},
    ),
    (
        "explicit-and-super-init",
        """
class Base:
    def __init__(self, repo):
        self.repo = repo

    def site(self):
        return self.repo.save(1)


class Child(Base):
    def __init__(self, repo):
        super().__init__(repo)


class Other(Base):
    def __init__(self):
        Base.__init__(self, MemRepo())


def main():
    Child(SqlRepo())
    Other()
""",
        {SQL, MEM},
        {},
    ),
    (
        "super-with-arguments-opens-inits",
        """
class Base:
    def __init__(self, repo):
        self.repo = repo

    def site(self):
        return self.repo.save(1)


class Child(Base):
    def __init__(self, repo):
        super(Base, self).__init__(repo)


def main():
    Child(SqlRepo())
    Base(SqlRepo())
""",
        set(),
        {"super-with-arguments": 1},
    ),
    (
        "untyped-init-call-opens-inits",
        """
class Holder:
    def __init__(self, repo):
        self.repo = repo

    def site(self):
        return self.repo.save(1)


def main(thing, value):
    Holder(SqlRepo())
    thing.__init__(value)
""",
        set(),
        {"untyped-init-call": 1},
    ),
    (
        "untyped-reference-opens-functions",
        """
def store(repo):
    return repo.save(1)


def main(module):
    store(SqlRepo())
    return module.store
""",
        set(),
        {"untyped-reference": 1},
    ),
    (
        "staticmethod-parameter-is-method-parameter",
        """
class Tools:
    @staticmethod
    def site(repo):
        return repo.save(1)


def main():
    Tools.site(SqlRepo())
""",
        set(),
        {"method-parameter": 1},
    ),
    (
        "generator-and-coroutine-returns",
        """
def many():
    yield SqlRepo()


async def later():
    return SqlRepo()


def site():
    value = next(many())
    other = many()
    return other.save(1)
""",
        set(),
        {"generator": 1},
    ),
    (
        "class-object-receiver-is-open",
        """
class Holder:
    repo = SqlRepo()


def site():
    return Holder.repo.save(1)
""",
        set(),
        {},
    ),
    (
        "globals-exposes-module",
        """
def store(repo):
    return repo.save(1)


def main(name, value):
    store(SqlRepo())
    return globals()[name](value)
""",
        set(),
        {"module-namespace": 1},
    ),
    (
        "sys-modules-own-module-with-computed-access",
        """
import sys


def store(repo):
    return repo.save(1)


def main(name, value, thing):
    store(SqlRepo())
    module = sys.modules[__name__]
    return getattr(thing, name)(value)
""",
        set(),
        {"module-namespace": 1},
    ),
    (
        "computed-dynamic-import-with-computed-access",
        """
import importlib


def store(repo):
    return repo.save(1)


def main(name, attribute, value):
    store(SqlRepo())
    module = importlib.import_module(name)
    return getattr(module, attribute)(value)
""",
        set(),
        {"module-namespace": 1},
    ),
    (
        "literal-dynamic-import-without-computed-access-stays-closed",
        """
import importlib


def store(repo):
    return repo.save(1)


def site():
    return store(SqlRepo())


def main():
    return importlib.import_module("app")
""",
        set(),
        {},
    ),
    (
        "inspect-getmembers-on-module",
        """
import inspect
import sys


def store(repo):
    return repo.save(1)


def main():
    store(SqlRepo())
    return inspect.getmembers(sys.modules[__name__])
""",
        set(),
        {"module-namespace": 1},
    ),
    (
        "class-assignment-write-dirties-class",
        """
class Holder:
    def __init__(self):
        self.repo = SqlRepo()

    def site(self):
        return self.repo.save(1)


def main(kind):
    holder = Holder()
    holder.__class__ = kind
""",
        set(),
        {"computed-attribute-write": 1},
    ),
    (
        "unpacking-augmented-for-and-with-writes-are-unknown",
        """
class Holder:
    def __init__(self, pair, items, context):
        self.repo = SqlRepo()
        self.a, self.b = pair
        self.count = 0
        self.count += 1
        for self.item in items:
            pass
        with context as self.handle:
            pass

    def site(self):
        return self.a.save(1)


def main(pair, items, context):
    return Holder(pair, items, context)
""",
        set(),
        {"unknown-write": 1},
    ),
    (
        "harmless-type-probes-keep-constructors-closed",
        """
class Holder:
    def __init__(self, repo):
        self.repo = repo

    def site(self):
        return self.repo.save(1)


def main(thing):
    holder = Holder(SqlRepo())
    if type(thing) is Holder or isinstance(thing, (Holder, Repo)):
        return type(holder).__name__
    try:
        pass
    except (KeyError, ValueError):
        return holder.__class__.__name__
    return None
""",
        {SQL},
        {},
    ),
    (
        "escaped-type-probe-opens-constructor",
        """
class Holder:
    def __init__(self, repo):
        self.repo = repo

    def site(self):
        return self.repo.save(1)


def main():
    holder = Holder(SqlRepo())
    factory = type(holder)
    return factory
""",
        set(),
        {"dynamic-construction": 1},
    ),
    (
        "all-and-docstrings-are-not-mentions",
        '''
"""store 함수 설명."""

__all__ = ["store"]
__all__ += ["store"]


def store(repo):
    """store."""
    return repo.save(1)


def main():
    return store(SqlRepo())
''',
        set(),
        {},
    ),
    (
        "three-argument-type-opens-cls-construction",
        """
class Base:
    @classmethod
    def make(cls):
        return cls()

    def save(self, item):
        return item


def site():
    return Base.make().save(1)


Dynamic = type("Dynamic", (Base,), {})
""",
        set(),
        {"dynamic-subclass": 1},
    ),
    (
        "lambda-scoped-arguments",
        """
def store(repo):
    return repo.save(1)


def main(items):
    return [store(item) for item in items] + list(map(lambda value: store(value), items))
""",
        set(),
        {"lambda-scope": 1},
    ),
    (
        "decorated-init-and-class-decorator",
        """
import functools


def register(cls):
    return cls


class Holder:
    @functools.lru_cache
    def __init__(self, repo):
        self.repo = repo

    def site(self):
        return self.repo.save(1)


def main():
    Holder(SqlRepo())
""",
        set(),
        {"decorated": 1},
    ),
]


@pytest.mark.parametrize(("name", "app", "targets", "reasons"), CASES, ids=[case[0] for case in CASES])
def test_flow_rules(make_project: MakeProject, name: str, app: str, targets: set[str], reasons: dict[str, int]) -> None:
    """사례마다 `bound` 대상과 열린 이유가 기대와 같다."""
    graph = build(make_project, app)
    site = "app.py#Holder.site" if "class Holder" in app and "def site(self" in app else "app.py#site"
    if name in ("explicit-and-super-init", "super-with-arguments-opens-inits"):
        site = "app.py#Base.site"
    if name == "staticmethod-parameter-is-method-parameter":
        site = "app.py#Tools.site"
    actual_targets, actual_reasons = outcome(graph, site)
    assert actual_targets == targets
    for reason, count in reasons.items():
        assert actual_reasons.get(reason) == count, actual_reasons
    if not targets and not reasons:
        assert actual_targets == set()


def test_keyword_only_parameter_links(make_project: MakeProject) -> None:
    """키워드 전용 매개변수는 키워드 실인자로, 위치 전용 매개변수는 위치 실인자로만 채운다."""
    graph = build(
        make_project,
        """
def store(repo, /, *, other):
    repo.save(1)
    return other.save(2)


def main():
    store(SqlRepo(), other=MemRepo())
""",
    )
    targets, _ = outcome(graph, "app.py#store")
    assert targets == {SQL, MEM}


def test_star_import_global_flow(make_project: MakeProject) -> None:
    """별 import로 들어온 모듈 전역은 정의한 모듈의 흐름이다."""
    graph = build(
        make_project,
        """
from wiring import *


def site():
    return repo.save(1)
""",
        wiring_py="""
import os

from repos import MemRepo, SqlRepo

repo = SqlRepo()
if os.environ.get("X"):
    repo = MemRepo()
""",
    )
    assert outcome(graph)[0] == {SQL, MEM}


def test_budget_and_depth_limits_open_flows(make_project: MakeProject, monkeypatch: pytest.MonkeyPatch) -> None:
    """단계 예산·깊이 상한을 넘은 질의는 열림이다(잇지 않는다)."""
    app = """
def site(flag):
    repo = SqlRepo()
    if flag:
        repo = MemRepo()
    return repo.save(1)
"""
    monkeypatch.setattr(flow_module, "MAX_FLOW_STEPS", 1)
    assert outcome(build(make_project, app)) == (set(), {"flow-budget": 1})
    monkeypatch.setattr(flow_module, "MAX_FLOW_STEPS", 20_000)
    monkeypatch.setattr(flow_module, "MAX_FLOW_DEPTH", 0)
    assert outcome(build(make_project, app))[1] == {"flow-depth": 1}


def test_self_feeding_slot_is_a_cycle(make_project: MakeProject) -> None:
    """자기를 거쳐 돌아오는 자리(`self.repo = self.repo or MemRepo()`)는 순환이라 열린다(줄 뿐 틀리지 않는다)."""
    graph = build(
        make_project,
        """
class Holder:
    def __init__(self):
        self.repo = SqlRepo()

    def refresh(self):
        self.repo = self.repo or MemRepo()

    def site(self):
        return self.repo.save(1)
""",
    )
    assert outcome(graph, "app.py#Holder.site") == (set(), {"flow-cycle": 1})


def test_computed_write_through_rebound_receiver(make_project: MakeProject) -> None:
    """정적 값으로 대상을 모르는 계산된 쓰기도 수신자 흐름이 닫혔으면 그 클래스를 연다."""
    graph = build(
        make_project,
        """
class Holder:
    def __init__(self):
        self.repo = SqlRepo()

    def site(self):
        return self.repo.save(1)


class Other:
    pass


def main(flag, name, value):
    target = Holder()
    if flag:
        target = Other()
    setattr(target, name, value)
""",
    )
    targets, reasons = outcome(graph, "app.py#Holder.site")
    assert targets == set()
    assert reasons == {"computed-attribute-write": 1}
    assert graph.statistics["boundDispatch"]["unknownTargetWrites"] == 0  # type: ignore[index]


def test_framework_methods_properties_and_lambda_sites(make_project: MakeProject) -> None:
    """프레임워크 구현 메서드는 상속 멤버 정점으로 잇고, property 호출·람다 안 호출 지점은 잇지 않는다.

    (프로젝트가 정의하지 않는 이름의 호출은 이름 필터가 외부로 확정하므로 `dispatch`를 한 클래스가 재정의한다.)
    """
    graph = build(
        make_project,
        """
from django.views import View
from rest_framework import serializers


class ItemView(View):
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)


class OtherView(View):
    pass


class ItemSerializer(serializers.Serializer):
    pass


class Holder:
    @property
    def run(self):
        return len


def site(flag, request):
    view = ItemView()
    if flag:
        view = OtherView()
    return view.dispatch(request)


def serializers_many(flag, data):
    serializer = ItemSerializer(data=data)
    if flag:
        serializer = ItemSerializer(data, many=True)
    return serializer.save()


def props(flag):
    value = Holder()
    if flag:
        value = Holder()
    return value.run()


def lambdas(flag):
    value = SqlRepo()
    if flag:
        value = MemRepo()
    return (lambda: value.save(1))()
""",
    )
    targets, reasons = outcome(graph)
    assert targets == {"app.py#ItemView.dispatch", "app.py#OtherView.dispatch"}
    # DRF 직렬화기는 `__new__`(many=True면 ListSerializer)가 있어 생성 결과를 모른다.
    assert outcome(graph, "app.py#serializers_many")[0] == set()
    assert reasons.get("custom-new") == 1
    assert outcome(graph, "app.py#props")[0] == set()
    assert outcome(graph, "app.py#lambdas")[0] == set()
    assert reasons.get("property") == 1
    assert reasons.get("lambda-scope") == 1


def test_dunder_setattr_calls_are_writes(make_project: MakeProject) -> None:
    """`object.__setattr__(h, "repo", v)`·`super().__setattr__("repo", v)`·`h.__setattr__(name, v)`도 쓰기다."""
    graph = build(
        make_project,
        """
class Holder:
    def __init__(self):
        super().__setattr__("repo", SqlRepo())

    def site(self):
        return self.repo.save(1)


def main():
    holder = Holder()
    object.__setattr__(holder, "repo", MemRepo())
""",
    )
    assert outcome(graph, "app.py#Holder.site") == ({SQL, MEM}, {})
    dirty = build(
        make_project,
        """
class Holder:
    def __init__(self):
        self.repo = SqlRepo()

    def site(self):
        return self.repo.save(1)


def main(name, value):
    holder = Holder()
    holder.__setattr__(name, value)
""",
    )
    assert outcome(dirty, "app.py#Holder.site") == (set(), {"computed-attribute-write": 1})


def test_redefined_functions_are_open(make_project: MakeProject) -> None:
    """조건부로 다시 정의한 함수는 색인이 첫 정의만 보므로(다른 정의의 장식자가 다를 수 있다) 연다."""
    graph = build(
        make_project,
        """
import os


def wrap(function):
    return function


if os.environ.get("X"):
    def store(repo):
        return repo.save(1)
else:
    @wrap
    def store(repo):
        return repo.save(2)


def main():
    return store(SqlRepo())
""",
    )
    assert outcome(graph, "app.py#store") == (set(), {"redefined": 1})


def test_class_body_rebinding_is_open(make_project: MakeProject) -> None:
    """클래스 본문이 이름을 두 번(조건부) 묶거나 반복 변수로 묶으면 그 속성 값을 모른다(마지막 값만 보지 않는다)."""
    graph = build(
        make_project,
        """
import os


class Holder:
    if os.environ.get("X"):
        repo = SqlRepo()
    else:
        repo = MemRepo()

    for backup in (SqlRepo(), MemRepo()):
        pass

    def site(self):
        return self.repo.save(1)

    def other(self):
        return self.backup.save(2)
""",
    )
    assert outcome(graph, "app.py#Holder.site")[0] == set()
    assert outcome(graph, "app.py#Holder.other")[0] == set()
    assert outcome(graph, "app.py#Holder.site")[1] == {"rebound-attribute": 2}


#: 리뷰에서 재현한 건전성 구멍(생성 결과를 바꾸는 `__new__` 쓰기, 물려받은 메타클래스, 동적 하위 클래스)이다.
CONSTRUCTION_HOLES = {
    "new-written-to-class": (
        """
class Loose:
    def save(self, item):
        return item


def pooled(cls):
    return Loose()


SqlRepo.__new__ = staticmethod(pooled)
""",
        "custom-new",
    ),
    "inherited-metaclass": (
        """
class Loose:
    def save(self, item):
        return item


class Singleton(type):
    def __call__(cls):
        return Loose()


class Base(metaclass=Singleton):
    pass


class SqlRepo(Base):
    def save(self, item):
        return item
""",
        "metaclass",
    ),
    "dynamic-subclass-through-bases": (
        """
import types


class Loose:
    def save(self, item):
        return item


class Service(Client):
    pass


Dynamic = types.new_class("Dynamic", Service.__bases__, {})
Dynamic(Loose()).run()
""",
        "dynamic-subclass",
    ),
}


@pytest.mark.parametrize("name", sorted(CONSTRUCTION_HOLES))
def test_construction_holes_are_open(make_project: MakeProject, name: str) -> None:
    """`Repo.__new__ = …`, 기반 클래스에서 물려받은 메타클래스, `types.new_class`로 만든 하위 클래스는 생성 결과나
    생성자 매개변수를 모르므로 `bound`를 내지 않는다."""
    extra, reason = CONSTRUCTION_HOLES[name]
    graph = build(
        make_project,
        """
class Client:
    def __init__(self, repo):
        self.repo = repo

    def run(self):
        return self.repo.save(1)
"""
        + extra
        + """

Client(SqlRepo()).run()
""",
    )
    targets, reasons = outcome(graph, "app.py#Client.run")
    assert targets == set()
    assert reasons.get(reason) == 1, reasons
