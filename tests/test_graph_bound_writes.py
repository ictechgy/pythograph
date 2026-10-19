"""`bound` 값 흐름의 같은 이름 속성 쓰기 판정: 비용이 클래스·모듈 수에 선형이고, 수신자 종류별 판정을 지킨다.

평범한 클래스 N개가 `__init__`에서 `self.repo = repo`를 쓰고 메서드에서 `self.repo.save()`를 부르면 속성 자리 질의가
N개 생긴다. 질의마다 같은 이름 쓰기 N개를 모두 다시 판정하면 N²이다(수정 전 N=1,000에서 `write_reaches` 1,000,000회).
메서드 가림 판정과 모듈 전역 흐름도 같은 이름 쓰기를 질의마다 모두 훑었다. 비용은 시간 대신 판정·순회 횟수로 센다.
쓰기는 수신자 종류(정적 인스턴스·클래스, 모듈, 인스턴스가 아닌 값, 람다 뿌리, 타입 모르는 값)로 나눠 보므로 종류마다
닿는 자리도 확인한다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from pythograph.graph import build as build_module
from pythograph.graph.build import build_graph
from pythograph.graph.exposure import AttributeWrite, ProgramFacts
from pythograph.graph.flow import FlowAnalyzer
from pythograph.graph.model import CallGraph
from pythograph.source.project import Project

#: 합성 프로젝트 도우미 타입이다.
MakeProject = Callable[[dict[str, str]], Path]

#: 합성 클래스·모듈 수다(수정 전에는 판정·순회가 이 수의 제곱이었다).
COUNT = 300

#: 저장소 클래스다(모든 합성 프로젝트가 같이 쓴다).
REPOS = """
class Repo:
    def save(self):
        return 1


class OtherRepo:
    def save(self):
        return 2


class MemRepo:
    def save(self):
        return 3
"""


class CountedWrites(list[AttributeWrite]):
    """훑은 항목 수를 세는 쓰기 목록이다(같은 이름 쓰기를 질의마다 다시 훑는지 본다)."""

    def __init__(self, items: Iterable[AttributeWrite], counter: Counter[str]) -> None:
        """목록을 만든다.

        Args:
            items: 쓰기.
            counter: 훑은 항목 수를 더할 계수기.
        """
        super().__init__(items)
        self.counter = counter

    def __iter__(self) -> Iterator[AttributeWrite]:
        """항목을 낼 때마다 센다."""
        for item in super().__iter__():
            self.counter["scanned"] += 1
            yield item


def count_scanned_writes(monkeypatch: pytest.MonkeyPatch, counter: Counter[str]) -> None:
    """모은 프로그램 사실의 쓰기 목록을 훑은 항목 수를 세는 목록으로 바꾼다.

    Args:
        monkeypatch: pytest monkeypatch.
        counter: 계수기.
    """
    original = build_module.collect_program

    def collect(*args: Any, **kwargs: Any) -> ProgramFacts:
        """원래 수집 뒤 쓰기 목록을 감싼다."""
        facts = original(*args, **kwargs)
        facts.writes = {name: CountedWrites(writes, counter) for name, writes in facts.writes.items()}
        return facts

    monkeypatch.setattr(build_module, "collect_program", collect)


def count_write_reaches(monkeypatch: pytest.MonkeyPatch, counter: Counter[str]) -> None:
    """`FlowAnalyzer.write_reaches` 호출 수를 센다.

    Args:
        monkeypatch: pytest monkeypatch.
        counter: 계수기.
    """
    original = FlowAnalyzer.write_reaches

    def write_reaches(self: FlowAnalyzer, *args: Any, **kwargs: Any) -> bool:
        """원래 판정을 부르고 센다."""
        counter["write_reaches"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FlowAnalyzer, "write_reaches", write_reaches)


def service_files(count: int) -> dict[str, str]:
    """`__init__`에서 같은 이름 속성을 쓰는 서비스 클래스 N개와 생성 지점이다.

    클래스마다 `self.save`도 써서 메서드 가림 판정(`Repo.save`가 인스턴스 속성으로 가려지는지)이 같은 이름 쓰기 N개를
    본다. `configure`의 수신자는 타입을 몰라 흐름으로 판정하는 쓰기다(질의마다 한 번).

    Args:
        count: 클래스 수.

    Returns:
        상대 경로 → 내용.
    """
    classes = "".join(
        f"""

class Service{index}:
    def __init__(self, repo):
        self.repo = repo
        self.save = repo.save

    def run(self):
        return self.repo.save()
"""
        for index in range(count)
    )
    configure = "\n\ndef configure(target, repo):\n    target.repo = repo\n    return target\n"
    calls = "".join(f"    services.Service{index}(Repo()).run()\n" for index in range(count))
    main = (
        "import services\nfrom repos import Repo\n\n\ndef main():\n"
        f"{calls}    services.configure(services.Service0(Repo()), Repo())\n"
    )
    return {"repos.py": REPOS, "services.py": classes.lstrip("\n") + configure, "main.py": main}


def module_files(count: int) -> dict[str, str]:
    """모듈 전역 `repo`(조건식이라 정적 값 모름)를 부르는 모듈 N개다. 모듈마다 같은 이름 속성 쓰기도 하나 있다.

    Args:
        count: 모듈 수.

    Returns:
        상대 경로 → 내용.
    """
    files = {"repos.py": REPOS, "mods/__init__.py": ""}
    for index in range(count):
        files[f"mods/m{index}.py"] = f"""
import os

from repos import OtherRepo, Repo

repo = Repo() if os.environ.get("REPO") else OtherRepo()


class Holder{index}:
    def __init__(self, repo):
        self.repo = repo


def run():
    return repo.save()
"""
    imports = "".join(f"from mods.m{index} import Holder{index}\n" for index in range(count))
    calls = "".join(f"    Holder{index}(Repo())\n" for index in range(count))
    files["main.py"] = f"from repos import Repo\n{imports}\n\ndef main():\n{calls}"
    return files


def bound_sources(graph: CallGraph, target: str) -> set[str]:
    """대상 정점으로 들어오는 `bound` 간선의 출발 정점이다.

    Args:
        graph: 그래프.
        target: 도착 정점 id.

    Returns:
        출발 id 집합.
    """
    return {edge.source for edge in graph.edges if edge.evidence == "bound" and edge.target == target}


def test_attribute_slot_queries_judge_writes_linearly(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """속성 자리·메서드 가림 질의는 걸리는 쓰기만 본다: 흐름으로 판정할 쓰기만 질의마다 `write_reaches`로 본다."""
    counter: Counter[str] = Counter()
    count_write_reaches(monkeypatch, counter)
    count_scanned_writes(monkeypatch, counter)
    graph = build_graph(Project.open(make_project(service_files(COUNT))), False)
    assert bound_sources(graph, "repos.py#Repo.save") == {f"services.py#Service{index}.run" for index in range(COUNT)}
    assert counter["write_reaches"] <= 2 * COUNT
    assert counter["scanned"] <= 3 * COUNT


def test_module_global_queries_scan_writes_once(make_project: MakeProject, monkeypatch: pytest.MonkeyPatch) -> None:
    """모듈 전역 흐름은 그 모듈·타입 모르는 수신자 쓰기만 본다: 같은 이름 속성 쓰기 목록을 이름마다 한 번 훑는다."""
    counter: Counter[str] = Counter()
    count_scanned_writes(monkeypatch, counter)
    graph = build_graph(Project.open(make_project(module_files(COUNT))), False)
    expected = {f"mods/m{index}.py#run" for index in range(COUNT)}
    assert bound_sources(graph, "repos.py#Repo.save") == bound_sources(graph, "repos.py#OtherRepo.save") == expected
    assert counter["scanned"] <= 2 * COUNT


def bound_targets(graph: CallGraph, source: str) -> set[str]:
    """정점에서 나가는 `bound` 간선의 도착 정점이다.

    Args:
        graph: 그래프.
        source: 출발 정점 id.

    Returns:
        도착 id 집합.
    """
    return {edge.target for edge in graph.edges if edge.source == source and edge.evidence == "bound"}


def test_lambda_receiver_writes_reach_instance_slots_and_module_globals(make_project: MakeProject) -> None:
    """람다 매개변수가 수신자인 리터럴 `setattr`은 수신자를 풀 수 없어 모든 인스턴스 자리와 모듈 전역에 값을 더한다."""
    files = {
        "repos.py": REPOS,
        "wiring.py": """
            import os

            from repos import Repo

            repo = Repo() if os.environ.get("REPO") else Repo()
        """,
        "app.py": """
            import wiring
            from repos import MemRepo, OtherRepo, Repo


            class Holder:
                def __init__(self):
                    self.repo = Repo()

                def site(self):
                    return self.repo.save()


            def rewire(holders):
                list(map(lambda h: setattr(h, "repo", OtherRepo()), holders))
                list(map(lambda m: setattr(m, "repo", MemRepo()), [wiring]))


            def global_site():
                return wiring.repo.save()


            def main():
                rewire([Holder()])
        """,
    }
    graph = build_graph(Project.open(make_project(files)), False)
    every = {"repos.py#Repo.save", "repos.py#OtherRepo.save", "repos.py#MemRepo.save"}
    assert bound_targets(graph, "app.py#Holder.site") == every
    assert bound_targets(graph, "app.py#global_site") == every


def test_super_and_class_attribute_receivers_feed_instance_slots_only(make_project: MakeProject) -> None:
    """`super()`·클래스 본문 속성이 수신자인 쓰기는 흐름으로 인스턴스 자리만 판정하고 모듈 전역에는 더하지 않는다."""
    files = {
        "repos.py": REPOS,
        "wiring.py": """
            import os

            from repos import Repo

            repo = Repo() if os.environ.get("REPO") else Repo()
        """,
        "app.py": """
            import wiring
            from repos import MemRepo, OtherRepo, Repo


            class Holder:
                pass


            class Base:
                pass


            class Service(Base):
                child = Holder()

                def __init__(self):
                    self.repo = Repo()
                    super().repo = OtherRepo()
                    self.child.repo = MemRepo()

                def site(self):
                    return self.repo.save()


            def global_site():
                return wiring.repo.save()


            def main():
                return Service().site()
        """,
    }
    graph = build_graph(Project.open(make_project(files)), False)
    assert bound_targets(graph, "app.py#Service.site") == {"repos.py#Repo.save", "repos.py#OtherRepo.save"}
    assert bound_targets(graph, "app.py#global_site") == {"repos.py#Repo.save"}


def test_non_instance_receivers_never_feed_instance_slots(make_project: MakeProject) -> None:
    """함수·외부 모듈·프로젝트 모듈이 수신자인 같은 이름 쓰기는 인스턴스 자리에 닿지 않고, 클래스 객체 쓰기는 닿는다."""
    files = {
        "repos.py": REPOS,
        "wiring.py": "",
        "app.py": """
            import logging

            import wiring
            from repos import MemRepo, OtherRepo, Repo


            class Base:
                def __init__(self):
                    self.repo = Repo()

                def site(self):
                    return self.repo.save()


            class Child(Base):
                pass


            def helper():
                return 1


            def main():
                helper.repo = MemRepo()
                logging.repo = MemRepo()
                wiring.repo = MemRepo()
                Child.repo = OtherRepo()
                return Base().site()
        """,
    }
    graph = build_graph(Project.open(make_project(files)), False)
    assert bound_targets(graph, "app.py#Base.site") == {"repos.py#Repo.save", "repos.py#OtherRepo.save"}


def test_base_class_writes_reach_subclass_instance_slots(make_project: MakeProject) -> None:
    """기반 클래스 메서드의 `self` 쓰기와 기반 클래스 객체 쓰기는 하위 클래스 인스턴스만 보는 자리에도 닿는다."""
    files = {
        "repos.py": REPOS,
        "app.py": """
            from repos import MemRepo, OtherRepo


            class Base:
                def __init__(self, repo):
                    self.repo = repo


            class Child(Base):
                def site(self):
                    return self.repo.save()


            def main():
                Base.repo = MemRepo()
                return Child(OtherRepo()).site()
        """,
    }
    graph = build_graph(Project.open(make_project(files)), False)
    assert bound_targets(graph, "app.py#Child.site") == {"repos.py#OtherRepo.save", "repos.py#MemRepo.save"}


def test_slot_flow_joins_writes_in_collection_order(make_project: MakeProject) -> None:
    """속성 자리 흐름은 같은 이름 쓰기를 모은 순서(정의 id 순)대로 합치므로 처음 열린 이유가 앞선 쓰기의 것이다.

    수신자를 몰라 흐름으로 판정하는 `bind`의 쓰기(`app.py`)가 정적 수신자 `self` 쓰기(`models.py`)보다 앞선다. 앞 쓰기의
    값은 외부 호출 결과(`call-result`)이고 뒤 쓰기는 튜플 풀기라 값을 모른다(`unknown-write`). 정적 항목을 먼저 돌면
    열린 이유가 바뀐다.
    """
    files = {
        "repos.py": REPOS,
        "models.py": """
            from repos import Repo


            class Holder:
                def __init__(self):
                    self.repo, self.other = Repo(), 1

                def site(self):
                    return self.repo.save()
        """,
        "app.py": """
            import os

            from models import Holder
            from repos import Repo


            def make_repo():
                return os.environ.get("REPO")


            def bind(target, repo):
                target.repo = make_repo()
                return target


            def main():
                holder = Holder()
                bind(holder, Repo())
                return holder.site()
        """,
    }
    graph = build_graph(Project.open(make_project(files)), False)
    assert bound_targets(graph, "models.py#Holder.site") == set()
    assert graph.statistics["boundDispatch"]["open"] == {"call-result": 1}  # type: ignore[index]


def test_exact_and_subclass_receivers_of_one_class_stay_apart(make_project: MakeProject) -> None:
    """같은 클래스를 정확한 인스턴스(`base`)와 하위 클래스 포함 인스턴스(`self`)로 함께 물어도 두 판정이 섞이지 않는다.

    정확한 `Base` 인스턴스 쓰기는 `Child`만 보는 자리에 닿지 않고, 기반 클래스 메서드의 `self` 쓰기는 닿는다. `Base`
    자리를 정확한 값으로 묻는 `main`은 `Child` 인스턴스 쓰기를 보지 않는다. 클래스 집합 메모 키가 정확 여부를 잃으면
    먼저 계산한 쪽 집합을 다른 쪽이 받아 대상이 바뀐다.
    """
    files = {
        "repos.py": REPOS,
        "app.py": """
            from repos import MemRepo, OtherRepo, Repo


            class Base:
                def reset(self):
                    self.repo = MemRepo()

                def site(self):
                    return self.repo.save()


            class Child(Base):
                def own(self):
                    return self.repo.save()


            def main():
                base = Base()
                base.repo = Repo()
                child = Child()
                child.repo = OtherRepo()
                child.reset()
                base.repo.save()
                child.own()
                return child.site()
        """,
    }
    graph = build_graph(Project.open(make_project(files)), False)
    every = {"repos.py#Repo.save", "repos.py#OtherRepo.save", "repos.py#MemRepo.save"}
    assert bound_targets(graph, "app.py#Base.site") == every
    assert bound_targets(graph, "app.py#Child.own") == {"repos.py#OtherRepo.save", "repos.py#MemRepo.save"}
    assert bound_targets(graph, "app.py#main") == {"repos.py#Repo.save", "repos.py#MemRepo.save"}


def test_external_result_receivers_may_be_modules(make_project: MakeProject) -> None:
    """외부 타입(`types.ModuleType`)으로 주석한 매개변수가 수신자인 쓰기는 모듈일 수 있어 모듈 전역 흐름에 더한다.

    수신자 값이 외부 호출 결과라 어떤 모듈인지 모른다. 이 쓰기를 빼면 `patch(wiring)`이 바꾼 `wiring.repo`를 놓쳐
    `global_site`의 `bound` 대상이 실제로 부를 수 있는 `MemRepo.save`를 빠뜨린다.
    """
    files = {
        "repos.py": REPOS,
        "wiring.py": """
            import os

            from repos import Repo

            repo = Repo() if os.environ.get("REPO") else Repo()
        """,
        "app.py": """
            import types

            import wiring
            from repos import MemRepo


            def patch(target: types.ModuleType):
                target.repo = MemRepo()


            def global_site():
                return wiring.repo.save()


            def main():
                patch(wiring)
                return global_site()
        """,
    }
    graph = build_graph(Project.open(make_project(files)), False)
    assert bound_targets(graph, "app.py#global_site") == {"repos.py#Repo.save", "repos.py#MemRepo.save"}
