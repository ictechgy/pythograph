"""`bound` 근거 등급: 닫힌 흐름의 양성 사례, 열린 자리의 음성 사례, 프로그램 판정, 모드별 미해석 수.

양성 사례는 수신자로 들어오는 관찰된 흐름이 모두 프로젝트 클래스 인스턴스인 호출(생성자, 모듈 수준 인스턴스,
`__init__` 매개변수로 받은 속성, 모든 호출 지점이 프로젝트 인스턴스를 넘기는 함수 매개변수, 합성 루트의 DI)이다.
음성 사례는 흐름이 열린 자리(라이브러리 공개 함수, 프레임워크가 부르는 진입점, `getattr`·`setattr`, `**kwargs` 펼치기,
감싸는 장식자, 몽키패치, 테스트 소스)다. 런타임 건전성 탐침은 `tests/test_graph_bound_probes.py`에 있다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.graph.build import build_graph
from pythograph.graph.exposure import _toml_tables, detect_library, is_importable, is_public
from pythograph.graph.model import CallGraph
from pythograph.source.project import Project

#: 합성 프로젝트 도우미 타입이다.
MakeProject = Callable[[dict[str, str]], Path]

#: 저장소 클래스 두 개(같은 메서드 이름)와 공통 기반이다.
REPOS = """
class Repo:
    def save(self, item):
        return item


class SqlRepo(Repo):
    def save(self, item):
        return ("sql", item)


class MemRepo(Repo):
    def save(self, item):
        return ("mem", item)


class Plain:
    def save(self, item):
        return item
"""


def graph_for(make_project: MakeProject, files: dict[str, str], include_tests: bool = False) -> CallGraph:
    """합성 프로젝트의 그래프를 만든다.

    Args:
        make_project: 합성 프로젝트 도우미.
        files: 상대 경로 → 내용.
        include_tests: 테스트 소스를 정점으로 포함할지.

    Returns:
        그래프.
    """
    return build_graph(Project.open(make_project(files)), include_tests)


def bound_targets(graph: CallGraph, source: str) -> set[str]:
    """정점에서 나가는 `bound` 간선의 도착 정점이다.

    Args:
        graph: 그래프.
        source: 출발 정점 id.

    Returns:
        도착 id 집합.
    """
    return {edge.target for edge in graph.edges if edge.source == source and edge.evidence == "bound"}


def open_reasons(graph: CallGraph) -> dict[str, int]:
    """`bound` 후보 중 잇지 못한 호출의 열린 이유다.

    Args:
        graph: 그래프.

    Returns:
        이유 → 수.
    """
    bound = graph.statistics["boundDispatch"]
    assert isinstance(bound, dict)
    return dict(bound["open"])


# --- 양성: 닫힌 흐름 ---


def test_rebound_local_links_every_constructed_class(make_project: MakeProject) -> None:
    """두 번 묶인 지역 이름(생성자 두 개)은 두 구현 모두에 bound로 잇는다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "app.py": """
                from repos import MemRepo, SqlRepo


                def store(flag, item):
                    repo = SqlRepo()
                    if flag:
                        repo = MemRepo()
                    return repo.save(item)
            """,
        },
    )
    assert bound_targets(graph, "app.py#store") == {"repos.py#SqlRepo.save", "repos.py#MemRepo.save"}
    node = graph.node_map()["app.py#store"]
    assert node.unresolved == {"direct": 1, "bound": 0, "candidates": 0}


def test_module_level_instances(make_project: MakeProject) -> None:
    """조건부 모듈 수준 인스턴스(두 대입)는 그 둘로 잇는다. import한 모듈의 전역도 따라간다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "wiring.py": """
                import os

                from repos import MemRepo, SqlRepo

                if os.environ.get("MEMORY"):
                    repo = MemRepo()
                else:
                    repo = SqlRepo()
            """,
            "app.py": """
                from wiring import repo


                def store(item):
                    return repo.save(item)
            """,
        },
    )
    assert bound_targets(graph, "app.py#store") == {"repos.py#SqlRepo.save", "repos.py#MemRepo.save"}


def test_init_attribute_from_constructor_parameters(make_project: MakeProject) -> None:
    """`self.repo = repo`(DI)는 `__init__` 매개변수를 거쳐 모든 생성 지점의 실인자로 이어진다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "service.py": """
                class Service:
                    def __init__(self, repo, label=None):
                        self.repo = repo
                        self.label = label

                    def run(self, item):
                        return self.repo.save(item)
            """,
            "main.py": """
                from repos import MemRepo, SqlRepo
                from service import Service


                def production():
                    repo = SqlRepo()
                    return Service(repo).run(1)


                def development():
                    return Service(repo=MemRepo(), label="dev").run(2)
            """,
        },
    )
    assert bound_targets(graph, "service.py#Service.run") == {"repos.py#SqlRepo.save", "repos.py#MemRepo.save"}
    reasons = graph.node_map()["service.py#Service.run"].reasons
    assert reasons == {"untyped-receiver": 1}


def test_function_parameters_with_in_project_call_sites(make_project: MakeProject) -> None:
    """모든 호출 지점이 프로젝트 인스턴스(기본값 None 포함)를 넘기는 함수 매개변수를 잇는다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "app.py": """
                from repos import MemRepo, Plain


                def store(repo=None, item=None):
                    if repo is not None:
                        return repo.save(item)
                    return None


                def main():
                    store(Plain(), 1)
                    store(repo=MemRepo())
                    store()
            """,
        },
    )
    assert bound_targets(graph, "app.py#store") == {"repos.py#Plain.save", "repos.py#MemRepo.save"}


def test_composition_root_di_through_factories(make_project: MakeProject) -> None:
    """합성 루트: 팩토리 반환 값, 모듈 수준 서비스, 서비스 속성의 속성(`svc.repo.save()`)을 잇는다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "container.py": """
                from repos import SqlRepo


                class Service:
                    def __init__(self, repo):
                        self.repo = repo


                def make_repo():
                    return SqlRepo()


                service = Service(make_repo())


                def handle(item):
                    return service.repo.save(item)


                def direct(item):
                    return make_repo().save(item)
            """,
        },
    )
    assert bound_targets(graph, "container.py#handle") == {"repos.py#SqlRepo.save"}
    # 반환 주석 없는 팩토리의 결과는 반환 값 흐름으로 잇는다.
    assert bound_targets(graph, "container.py#direct") == {"repos.py#SqlRepo.save"}


def test_annotated_receiver_narrows_overrides(make_project: MakeProject) -> None:
    """재정의 후보가 여럿인 주석 수신자는 관찰된 클래스의 구현으로만 bound를 둔다(나머지는 candidate로 남는다)."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "app.py": """
                from repos import Repo, SqlRepo


                def store(repo: Repo, item):
                    return repo.save(item)


                def main():
                    return store(SqlRepo(), 1)
            """,
        },
    )
    tiers = {(edge.target, edge.evidence) for edge in graph.edges if edge.source == "app.py#store"}
    assert ("repos.py#Repo.save", "direct") in tiers
    assert ("repos.py#SqlRepo.save", "bound") in tiers
    assert ("repos.py#MemRepo.save", "candidate") in tiers
    assert graph.node_map()["app.py#store"].unresolved == {"direct": 1, "bound": 0, "candidates": 0}


def test_inherited_method_uses_inherited_member_node(make_project: MakeProject) -> None:
    """물려받은 메서드는 정확한 클래스의 상속 멤버 정점으로 잇고, 그 정점이 정의로 inherit 간선을 둔다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS
            + """

class Cached(SqlRepo):
    pass
""",
            "app.py": """
                from repos import Cached


                def run(repo, item):
                    return repo.save(item)


                def main():
                    return run(Cached(), 1)
            """,
        },
    )
    assert bound_targets(graph, "app.py#run") == {"repos.py#Cached.save"}
    assert ("repos.py#Cached.save", "repos.py#SqlRepo.save", "direct") in {
        (edge.source, edge.target, edge.evidence) for edge in graph.edges
    }


def test_bound_mode_follows_bound_edges_in_reach(make_project: MakeProject) -> None:
    """`--dispatch bound` 순회는 bound 간선을 따라가고 도달 정점에 `evidence: bound`를 싣는다."""
    from pythograph.graph.traversal import TraversalRequest, traverse

    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "app.py": """
                from repos import SqlRepo


                def store(repo, item):
                    return repo.save(item)


                def main():
                    return store(SqlRepo(), 1)
            """,
        },
    )
    request = TraversalRequest(("app.py#main",), "dependencies", 128, 100_000, "bound")
    reached = {entry.id: entry.evidence for entry in traverse(graph, request).reached}
    assert reached["repos.py#SqlRepo.save"] == "bound"
    assert reached["app.py#store"] == "direct"
    direct = {
        entry.id
        for entry in traverse(graph, TraversalRequest(("app.py#main",), "dependencies", 128, 100_000, "direct")).reached
    }
    assert "repos.py#SqlRepo.save" not in direct


# --- 음성: 열린 자리 ---


def test_library_public_functions_are_open(make_project: MakeProject) -> None:
    """라이브러리(pyproject `[project]`)의 공개 함수 매개변수는 외부 호출자가 있을 수 있어 연다. 비공개는 닫힌다."""
    files = {
        "pyproject.toml": '[project]\nname = "demo"\n',
        "repos.py": REPOS,
        "api.py": """
            from repos import SqlRepo


            def store(repo, item):
                return repo.save(item)


            def _store(repo, item):
                return repo.save(item)


            def main():
                store(SqlRepo(), 1)
                _store(SqlRepo(), 1)
        """,
    }
    graph = graph_for(make_project, files)
    assert bound_targets(graph, "api.py#store") == set()
    assert bound_targets(graph, "api.py#_store") == {"repos.py#SqlRepo.save"}
    assert open_reasons(graph).get("library-public") == 1
    assert graph.statistics["boundDispatch"]["program"] == "library"  # type: ignore[index]


@pytest.mark.parametrize(
    ("decorator_module", "decorated"),
    [
        ("from celery import shared_task", "@shared_task"),
        (
            "from django.dispatch import receiver\nfrom django.db.models.signals import post_save",
            "@receiver(post_save)",
        ),
    ],
)
def test_framework_invoked_decorated_entry_points_are_open(
    make_project: MakeProject, decorator_module: str, decorated: str
) -> None:
    """Celery 작업·시그널 수신자(장식한 진입점)는 프레임워크가 부르므로 매개변수를 연다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "tasks.py": f"""
{decorator_module}
from repos import SqlRepo


{decorated}
def handle(repo, **kwargs):
    return repo.save(1)


def main():
    handle(SqlRepo())
""",
        },
    )
    assert bound_targets(graph, "tasks.py#handle") == set()
    assert open_reasons(graph) == {"decorated": 1}


def test_views_signals_and_commands_are_open(make_project: MakeProject) -> None:
    """URLconf에 값으로 넘긴 뷰 함수, `connect`로 등록한 수신자, 관리 명령·DRF 뷰(프레임워크 기반)의 속성은 연다.

    표에 없는 외부 기반(`BaseCommand`)의 인스턴스 속성 호출은 이미 외부 호출이라 후보가 아니다.
    """
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "views.py": """
                from django.core.management.base import BaseCommand
                from django.db.models.signals import post_save
                from django.urls import path
                from rest_framework.views import APIView

                from repos import SqlRepo


                def detail(request, repo):
                    return repo.save(request)


                def on_save(sender, repo):
                    return repo.save(sender)


                post_save.connect(on_save)
                urlpatterns = [path("x/", detail)]


                class Command(BaseCommand):
                    def __init__(self, repo):
                        self.repo = repo

                    def handle(self, *args, **options):
                        return self.repo.save(options)


                class ItemView(APIView):
                    def __init__(self, repo=None, **kwargs):
                        super().__init__(**kwargs)
                        self.repo = repo

                    def get(self, request):
                        return self.repo.save(request)


                def main():
                    detail(None, SqlRepo())
                    on_save(None, SqlRepo())
                    Command(SqlRepo())
                    ItemView(SqlRepo())
            """,
        },
    )
    for source in ("views.py#detail", "views.py#on_save", "views.py#Command.handle", "views.py#ItemView.get"):
        assert bound_targets(graph, source) == set(), source
    reasons = open_reasons(graph)
    assert reasons["referenced"] == 2
    assert reasons["framework-base"] == 1


def test_computed_getattr_and_setattr_open_slots(make_project: MakeProject) -> None:
    """계산된 `getattr`로 모듈 함수를 가져가면 그 모듈 함수가, 계산된 `setattr` 대상 클래스는 속성 자리가 열린다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "handlers.py": """
                def store(repo):
                    return repo.save(1)
            """,
            "app.py": """
                import handlers
                from repos import SqlRepo


                class Holder:
                    def __init__(self, repo):
                        self.repo = repo

                    def run(self):
                        return self.repo.save(2)


                def main(name, field, value):
                    handlers.store(SqlRepo())
                    getattr(handlers, name)(value)
                    holder = Holder(SqlRepo())
                    setattr(holder, field, value)
                    return holder.run()
            """,
        },
    )
    assert bound_targets(graph, "handlers.py#store") == set()
    assert bound_targets(graph, "app.py#Holder.run") == set()
    assert open_reasons(graph) == {"module-namespace": 1, "computed-attribute-write": 1}


def test_literal_setattr_and_monkeypatch_add_flows(make_project: MakeProject) -> None:
    """리터럴 `setattr`·다른 모듈의 몽키패치(`wiring.repo = …`)는 흐름에 더해지고, 값이 열리면 자리도 열린다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "wiring.py": """
                from repos import SqlRepo

                repo = SqlRepo()
                other = SqlRepo()


                class Holder:
                    def __init__(self):
                        self.repo = SqlRepo()

                    def run(self):
                        return self.repo.save(1)


                def use_repo():
                    return repo.save(1)


                def use_other():
                    return other.save(1)
            """,
            "patch.py": """
                import wiring
                from repos import MemRepo, Plain


                def install(value):
                    wiring.repo = MemRepo()
                    wiring.other = value
                    holder = wiring.Holder()
                    setattr(holder, "repo", Plain())
            """,
        },
    )
    # 다른 모듈이 쓰는 전역은 direct에서 정확한 값이 아니다 — 모든 쓰기를 합쳐 bound로 잇는다.
    assert bound_targets(graph, "wiring.py#use_repo") == {"repos.py#SqlRepo.save", "repos.py#MemRepo.save"}
    assert bound_targets(graph, "wiring.py#Holder.run") == {"repos.py#SqlRepo.save", "repos.py#Plain.save"}
    # `wiring.other = value`의 value는 호출 지점 없는 함수의 매개변수라 열린다.
    assert bound_targets(graph, "wiring.py#use_other") == set()
    assert open_reasons(graph) == {"no-call-site": 1}
    assert "wiring.py#use_repo" not in {
        edge.source for edge in graph.edges if edge.evidence == "direct" and edge.target.endswith(".save")
    }


def test_monkeypatched_module_global_is_open(make_project: MakeProject) -> None:
    """두 번 대입한 모듈 전역에 다른 모듈이 모르는 값을 쓰면 열린다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "wiring.py": """
                import os

                from repos import MemRepo, SqlRepo

                repo = SqlRepo() if os.environ.get("SQL") else MemRepo()
                if os.environ.get("RESET"):
                    repo = SqlRepo()


                def use_repo():
                    return repo.save(1)
            """,
            "patch.py": """
                import wiring


                def install(value):
                    wiring.repo = value
            """,
        },
    )
    assert bound_targets(graph, "wiring.py#use_repo") == set()
    assert open_reasons(graph) == {"no-call-site": 1}


def test_method_monkeypatch_shadows_bound_target(make_project: MakeProject) -> None:
    """인스턴스·클래스에 같은 이름을 쓰면(`repo.save = …`, `SqlRepo.save = …`) 메서드가 가려질 수 있어 잇지 않는다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "app.py": """
                from repos import MemRepo, SqlRepo


                def store(flag):
                    repo = SqlRepo()
                    if flag:
                        repo = MemRepo()
                    return repo.save(1)


                def patch(fake):
                    SqlRepo.save = fake
            """,
        },
    )
    assert bound_targets(graph, "app.py#store") == set()
    assert open_reasons(graph) == {"shadowed-method": 1}


def test_argument_spreads_open_parameters(make_project: MakeProject) -> None:
    """호출 지점의 `*args`·`**kwargs` 펼치기는 어느 매개변수든 채울 수 있어 연다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "app.py": """
                from repos import SqlRepo


                def store(repo):
                    return repo.save(1)


                def keep(repo):
                    return repo.save(2)


                def main(options):
                    store(SqlRepo())
                    store(**options)
                    keep(SqlRepo())
                    keep(*[SqlRepo()])
            """,
        },
    )
    assert bound_targets(graph, "app.py#store") == set()
    assert bound_targets(graph, "app.py#keep") == set()
    assert open_reasons(graph) == {"argument-spread": 2}


def test_wrapping_decorators_open_parameters_and_returns(make_project: MakeProject) -> None:
    """감싸는 장식자는 매개변수(래퍼가 무엇이든 넘길 수 있다)와 반환 값을 연다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "app.py": """
                import functools

                from repos import SqlRepo


                def logged(function):
                    @functools.wraps(function)
                    def wrapper(*args, **kwargs):
                        return function(*args, **kwargs)

                    return wrapper


                @logged
                def store(repo):
                    return repo.save(1)


                @logged
                def make():
                    return SqlRepo()


                def main():
                    store(SqlRepo())
                    return make().save(2)
            """,
        },
    )
    assert bound_targets(graph, "app.py#store") == set()
    assert bound_targets(graph, "app.py#main") == set()
    assert open_reasons(graph) == {"decorated": 2}


def test_test_sources_are_a_separate_program(make_project: MakeProject) -> None:
    """테스트 소스가 넘기는 목은 제품 흐름을 막지 않는다. 테스트 소스의 호출 지점은 잇지 않는다."""
    files = {
        "repos.py": REPOS,
        "app.py": """
            from repos import SqlRepo


            def store(repo):
                return repo.save(1)


            def main():
                return store(SqlRepo())
        """,
        "tests/test_app.py": """
            from unittest import mock

            from app import store


            def test_store():
                fake = mock.Mock()
                store(fake)
                fake.save(1)
        """,
    }
    graph = graph_for(make_project, files)
    assert bound_targets(graph, "app.py#store") == {"repos.py#SqlRepo.save"}
    included = graph_for(make_project, files, include_tests=True)
    assert bound_targets(included, "app.py#store") == {"repos.py#SqlRepo.save"}
    assert open_reasons(included).get("test-source") == 1


def test_non_test_import_of_tests_joins_the_program(make_project: MakeProject) -> None:
    """테스트가 아닌 모듈이 테스트 소스를 import하면 테스트 소스도 프로그램이다(목이 흐름을 연다)."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "app.py": """
                from repos import SqlRepo


                def store(repo):
                    return repo.save(1)


                def main():
                    return store(SqlRepo())
            """,
            "factories.py": """
                from tests import helpers
            """,
            "tests/__init__.py": "",
            "tests/helpers.py": """
                from unittest import mock

                from app import store

                store(mock.Mock())
            """,
        },
    )
    assert bound_targets(graph, "app.py#store") == set()
    assert graph.statistics["boundDispatch"]["wholeProgramTests"] is True  # type: ignore[index]


def test_self_receivers_and_escaped_functions_stay_open(make_project: MakeProject) -> None:
    """`self` 수신자, 값으로 넘긴 함수, 문자열로 적은 함수, 호출 지점 없는 함수는 잇지 않는다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "app.py": """
                from repos import SqlRepo

                HANDLERS = ["app.by_name"]


                class Base:
                    def run(self):
                        return self.step()

                    def step(self):
                        return 1


                class Child(Base):
                    def step(self):
                        return 2


                def escaped(repo):
                    return repo.save(1)


                def by_name(repo):
                    return repo.save(2)


                def orphan(repo):
                    return repo.save(3)


                def main():
                    escaped(SqlRepo())
                    by_name(SqlRepo())
                    return sorted([1], key=escaped)
            """,
        },
    )
    for source in ("app.py#Base.run", "app.py#escaped", "app.py#by_name", "app.py#orphan"):
        assert bound_targets(graph, source) == set(), source
    assert open_reasons(graph) == {
        "self-receiver": 1,
        "referenced": 1,
        "string-reference": 1,
        "no-call-site": 1,
    }


def test_dynamic_construction_and_code_execution_open_everything(make_project: MakeProject) -> None:
    """`type(x)(...)`(x 타입 모름)는 모든 생성자를, `exec`는 모든 함수·생성자를 연다."""
    base = {
        "repos.py": REPOS,
        "service.py": """
            from repos import SqlRepo


            class Service:
                def __init__(self, repo):
                    self.repo = repo

                def run(self):
                    return self.repo.save(1)


            def store(repo):
                return repo.save(2)


            def main():
                store(SqlRepo())
                return Service(SqlRepo()).run()
        """,
    }
    graph = graph_for(make_project, base)
    assert bound_targets(graph, "service.py#Service.run") == {"repos.py#SqlRepo.save"}
    assert bound_targets(graph, "service.py#store") == {"repos.py#SqlRepo.save"}
    clone = graph_for(make_project, {**base, "clone.py": "def clone(obj, value):\n    return type(obj)(value)\n"})
    assert bound_targets(clone, "service.py#Service.run") == set()
    assert bound_targets(clone, "service.py#store") == {"repos.py#SqlRepo.save"}
    run = graph_for(make_project, {**base, "run.py": "def run(code):\n    exec(code)\n"})
    assert bound_targets(run, "service.py#store") == set()


def test_class_opacity_rules(make_project: MakeProject) -> None:
    """장식한 클래스·메타클래스·외부 기반·프로젝트 `__new__`·속성 훅은 생성 결과나 속성 자리를 모른다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "kinds.py": """
                import dataclasses
                import enum

                from repos import SqlRepo


                @dataclasses.dataclass
                class Decorated:
                    def save(self, item):
                        return item


                class Meta(type):
                    pass


                class WithMeta(metaclass=Meta):
                    def save(self, item):
                        return item


                class External(enum.Enum):
                    A = 1

                    def save(self, item):
                        return item


                class Singleton:
                    def __new__(cls):
                        return SqlRepo()

                    def save(self, item):
                        return item


                class Hooked:
                    def __init__(self, repo):
                        self.repo = repo

                    def __setattr__(self, name, value):
                        object.__setattr__(self, name, value)

                    def run(self):
                        return self.repo.save(1)


                def pick(flag):
                    for maker in (Decorated, WithMeta, Singleton):
                        pass
                    value = Decorated()
                    if flag == 1:
                        value = WithMeta()
                    return value.save(1)


                def other(flag):
                    value = Singleton()
                    if flag:
                        value = External(1)
                    return value.save(1)


                def hooked():
                    return Hooked(SqlRepo()).run()
            """,
        },
    )
    for source in ("kinds.py#pick", "kinds.py#other", "kinds.py#Hooked.run"):
        assert bound_targets(graph, source) == set(), source
    reasons = open_reasons(graph)
    assert reasons["decorated-class"] == 1
    assert reasons["custom-new"] == 1
    assert reasons["attribute-hook"] == 1


def test_nonlocal_writers_open_locals(make_project: MakeProject) -> None:
    """안쪽 함수가 `nonlocal`로 다시 묶는 지역 이름은 연다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "app.py": """
                from repos import MemRepo, SqlRepo


                def outer(value):
                    repo = SqlRepo()
                    if value:
                        repo = MemRepo()

                    def reset():
                        nonlocal repo
                        repo = value

                    reset()
                    return repo.save(1)
            """,
        },
    )
    assert bound_targets(graph, "app.py#outer") == set()
    assert open_reasons(graph) == {"nonlocal-write": 1}


def test_untyped_receiver_calls_count_as_call_sites(make_project: MakeProject) -> None:
    """타입 모르는 수신자의 같은 이름 호출(`module_value.store(x)`)도 호출 지점이다(모듈 객체일 수 있다)."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "handlers.py": """
                def store(repo):
                    return repo.save(1)
            """,
            "app.py": """
                import importlib

                from handlers import store
                from repos import SqlRepo


                def main(name, value):
                    store(SqlRepo())
                    module = importlib.import_module(name)
                    module.store(value)
            """,
        },
    )
    assert bound_targets(graph, "handlers.py#store") == set()
    # `module.store(value)`의 value는 호출 지점 없는 main의 매개변수라 열리고, 그 호출 자체는 수신자가 호출 결과다.
    assert open_reasons(graph) == {"call-result": 1, "no-call-site": 1}


def test_bound_limitations_and_statistics(make_project: MakeProject) -> None:
    """`bound`·`candidates` 모드는 `bound-dispatch:`·`bound-assumptions:` 문구를 싣고 direct 모드는 싣지 않는다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "app.py": """
                from repos import SqlRepo


                def store(repo):
                    return repo.save(1)


                def main(target, name, value):
                    setattr(target, name, value)
                    return store(SqlRepo())
            """,
        },
    )
    assert not any(line.startswith("bound-") for line in graph.limitations["direct"])
    for mode in ("bound", "candidates"):
        lines = [line for line in graph.limitations[mode] if line.startswith("bound-")]
        assert lines[0].startswith("bound-dispatch: 1 calls")
        assert "treated as application" in lines[0]
        assert "(1 sites)" in lines[1]
    assert graph.statistics["boundDispatch"] == {
        "linked": {"untyped-receiver": 1},
        "open": {},
        "program": "application",
        "scanIncomplete": False,
        "unknownTargetWrites": 1,
        "wholeProgramTests": False,
    }


def test_incomplete_scan_opens_module_level_names(make_project: MakeProject) -> None:
    """읽지 못한 파일이 있으면 모듈 수준 함수가 모두 열리고, 함수 안에 중첩된 정의는 닫힌 채다."""
    graph = graph_for(
        make_project,
        {
            "repos.py": REPOS,
            "broken.py": "def (:\n",
            "app.py": """
                from repos import SqlRepo


                def store(repo):
                    return repo.save(1)


                def main():
                    def inner(repo):
                        return repo.save(2)

                    inner(SqlRepo())
                    return store(SqlRepo())
            """,
        },
    )
    assert bound_targets(graph, "app.py#store") == set()
    assert bound_targets(graph, "app.py#main.inner") == {"repos.py#SqlRepo.save"}
    assert graph.statistics["boundDispatch"]["scanIncomplete"] is True  # type: ignore[index]
    assert "incomplete scan" in next(line for line in graph.limitations["bound"] if line.startswith("bound-dispatch"))


# --- 프로그램 판정 ---


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({}, None),
        ({"setup.py": ""}, "setup.py at the project root"),
        ({"setup.cfg": ""}, "setup.cfg at the project root"),
        ({"pyproject.toml": "[tool.ruff]\nline-length = 1\n"}, None),
        ({"pyproject.toml": '[project]\nname = "x"\n'}, "pyproject.toml declares [project] or [tool.poetry]"),
        ({"pyproject.toml": '[tool.poetry]\nname = "x"\n'}, "pyproject.toml declares [project] or [tool.poetry]"),
        ({"pyproject.toml": "[tool.poetry]\npackage-mode = false # app\n"}, None),
        ({"pyproject.toml": '[project]\nname = "x"\n[tool.uv]\npackage = false\n'}, None),
        ({"pyproject.toml": '[ "project" ]\nname = "x"\n'}, "pyproject.toml declares [project] or [tool.poetry]"),
    ],
)
def test_detect_library(make_project: MakeProject, files: dict[str, str], expected: str | None) -> None:
    """배포 메타데이터가 있으면 라이브러리, 설치하지 않는 앱 표시는 애플리케이션이다."""
    root = make_project({"app.py": "", **files})
    assert detect_library(Project.open(root)) == expected


def test_detect_library_unreadable_pyproject(make_project: MakeProject) -> None:
    """읽지 못하는 pyproject.toml은 라이브러리로 본다(열린 쪽이 안전하다)."""
    root = make_project({"app.py": "", "pyproject.toml": ""})
    (root / "pyproject.toml").write_bytes(b"\xff\xfe[project]")
    assert detect_library(Project.open(root)) == "pyproject.toml could not be read"


def test_toml_tables_keeps_lines_per_table() -> None:
    """TOML 표 머리와 줄을 모은다(주석 붙은 머리, 배열 표는 표로 보지 않는다)."""
    tables = _toml_tables('a = 1\n[project] # meta\nname = "x"\n[[tool.x]]\ny = 2\n')
    assert tables[""] == ["a = 1"]
    assert tables["project"] == ['name = "x"', "[[tool.x]]", "y = 2"]


def test_public_and_importable_names(make_project: MakeProject) -> None:
    """공개 이름: 경로·점 경로에 밑줄 이름이 없고 함수 안에 중첩되지 않았다."""
    root = make_project(
        {
            "pkg/__init__.py": "",
            "pkg/api.py": """
                class Public:
                    def method(self):
                        return 1

                    class Inner:
                        pass


                class _Hidden:
                    pass


                def outer():
                    def inner():
                        return 1

                    return inner
            """,
            "pkg/_private.py": "def helper():\n    return 1\n",
        }
    )
    from pythograph.graph.index import DefinitionIndex
    from pythograph.source.symbols import SymbolTable

    project = Project.open(root)
    index = DefinitionIndex(project, SymbolTable(project), False)
    definitions = index.definitions
    assert is_public(definitions["pkg/api.py#Public"])
    assert is_public(definitions["pkg/api.py#Public.Inner"])
    assert is_public(definitions["pkg/api.py#Public.method"])
    assert is_public(definitions["pkg/api.py#<module>"])
    assert not is_public(definitions["pkg/api.py#_Hidden"])
    assert not is_public(definitions["pkg/api.py#outer.inner"])
    assert not is_public(definitions["pkg/_private.py#helper"])
    assert is_importable(definitions["pkg/_private.py#helper"])
    assert not is_importable(definitions["pkg/api.py#outer.inner"])
