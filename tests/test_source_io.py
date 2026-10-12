"""자원 상한은 읽기 전에 적용하고 불완전한 순회는 근거 등급까지 전달한다."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

import pytest

from pythograph.graph.build import build_graph
from pythograph.source import project as project_module
from pythograph.source.project import Project, ReadFailure, SourceModule
from tests.conftest import run_cli

MakeProject = Callable[[dict[str, str]], Path]


@pytest.mark.parametrize("method", ["module", "read_text"])
@pytest.mark.parametrize("oversized", [False, True])
def test_reads_request_only_the_byte_budget(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch, method: str, oversized: bool
) -> None:
    """실제 파일 스트림에 읽기 크기를 기록해 read-all 뒤 자르기가 되살아나지 않게 한다."""
    content = b"x = 123\n" + (b" " * 100 if oversized else b"")
    root = make_project({"app.py": content.decode()})
    (root / "app.py").write_bytes(content)
    project = Project.open(root)
    monkeypatch.setattr(project_module, "MAX_SOURCE_BYTES", 8)
    sizes: list[int] = []

    class GuardedReader(io.BufferedReader):
        """상한보다 많은 바이트 요청은 테스트를 실패시킨다."""

        def read(self, size: int = -1) -> bytes:
            """요청 크기를 기록하고 제한된 읽기만 허용한다."""
            sizes.append(size)
            assert 0 <= size <= 9
            return super().read(size)

    sources: list[GuardedReader] = []
    original = os.fdopen

    def guarded_open(descriptor: int, mode: str) -> GuardedReader:
        """실제 핸들을 보존해 신원 검사와 제한된 읽기를 함께 확인한다."""
        source = GuardedReader(original(descriptor, mode))
        sources.append(source)
        return source

    monkeypatch.setattr(project_module.os, "fdopen", guarded_open)
    if method == "module":
        result = project.module("app.py")
        assert isinstance(result, ReadFailure if oversized else SourceModule)
    else:
        assert project.read_text("app.py", limit=8) == (None if oversized else content.decode())
    assert sizes == [9]
    assert len(sources) == 1 and sources[0].closed


def test_negative_text_budget_never_opens_a_file(make_project: MakeProject, monkeypatch: pytest.MonkeyPatch) -> None:
    """음수 read 크기를 통해 무제한 읽기를 요청할 수 없다."""
    root = make_project({"app.py": "x = 1\n"})
    project = Project.open(root)

    def unexpected_open(*_args: object, **_kwargs: object) -> BinaryIO:
        """검증에서 거부돼야 하는 경로다."""
        raise AssertionError("negative budget must not open a file")

    monkeypatch.setattr(project_module.os, "open", unexpected_open)
    assert project.read_text("app.py", limit=-2) is None


@pytest.mark.parametrize("reverse", [False, True])
def test_oversized_directory_is_bounded_and_not_partially_sampled(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    """열거 순서와 무관하게 초과 디렉터리를 제외하고 상한+1개만 꺼낸 뒤 스트림을 닫는다."""
    root = make_project({f"file{index}.py": "" for index in range(20)})
    with os.scandir(root) as iterator:
        entries = sorted(iterator, key=lambda item: item.name, reverse=reverse)
    monkeypatch.setattr(project_module, "MAX_SCANNED_ENTRIES", 3)
    reads = 0
    closed = False

    def counted() -> Iterator[os.DirEntry[str]]:
        """요청된 항목 수가 실제 상한을 넘는 즉시 실패한다."""
        nonlocal reads
        for entry in entries:
            reads += 1
            assert reads <= 4
            yield entry

    @contextmanager
    def scan(_directory: Path) -> Iterator[Iterator[os.DirEntry[str]]]:
        """scandir 자원이 일찍 반환돼도 닫히는지 기록한다."""
        nonlocal closed
        try:
            yield counted()
        finally:
            closed = True

    monkeypatch.setattr(project_module.os, "scandir", scan)
    project = Project.open(root)
    assert project.python_files() == []
    assert project.scan_capped
    assert reads == 4 and closed


def test_directory_budget_preserves_previously_completed_files(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """하위 디렉터리 초과 때문에 앞서 완전히 수확한 파일까지 버리지는 않는다."""
    root = make_project({"app.py": "", "large/a.py": "", "large/b.py": ""})
    monkeypatch.setattr(project_module, "MAX_SCANNED_ENTRIES", 3)
    project = Project.open(root)
    assert project.python_files() == ["app.py"]
    assert project.scan_capped


@pytest.mark.parametrize(
    "command", [["graph"], ["schema"], ["routes", "--role", "server"], ["routes", "--role", "client"]]
)
def test_unreadable_directory_is_reported_by_every_producer(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """권한 실패를 빈 성공으로 위장하지 않고 원문·절대 경로 없이 한계에 남긴다."""
    root = make_project({"app.py": "def main(): pass\n", "private/hidden.py": ""})
    original = os.scandir

    def scan(directory: int | Path) -> os.ScandirIterator[str]:
        """실제 권한에 의존하지 않고 해당 디렉터리의 읽기 실패를 주입한다."""
        info = os.fstat(directory) if isinstance(directory, int) else directory.stat()
        if os.path.samestat(info, (root / "private").stat()):
            raise PermissionError("private error detail")
        return original(directory)

    monkeypatch.setattr(project_module.os, "scandir", scan)
    code, out, err = run_cli([*command, "--project", str(root)])
    assert code == 0, err
    limitations = json.dumps(json.loads(out)["limitations"])
    assert "unreadable entries" in limitations or "directory entries could not be read" in limitations
    assert "private error detail" not in limitations and str(root) not in limitations


def test_unreadable_directory_prevents_closed_world_dispatch(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """보이지 않는 모듈이 다른 객체를 넘길 수 있으므로 모듈 함수의 bound 증명을 막는다."""
    root = make_project(
        {
            "app.py": """
                class Repo:
                    def save(self):
                        return 1

                def store(repo):
                    return repo.save()

                def main():
                    return store(Repo())
            """,
            "private/hidden.py": "",
        }
    )
    complete = build_graph(Project.open(root), False)
    assert any(edge.source == "app.py#store" and edge.evidence == "bound" for edge in complete.edges)
    original = os.scandir

    def scan(directory: int | Path) -> os.ScandirIterator[str]:
        """숨은 모듈 하나가 읽히지 않는 조건을 만든다."""
        info = os.fstat(directory) if isinstance(directory, int) else directory.stat()
        if os.path.samestat(info, (root / "private").stat()):
            raise PermissionError
        return original(directory)

    monkeypatch.setattr(project_module.os, "scandir", scan)
    project = Project.open(root)
    graph = build_graph(project, False)
    assert project.scan_errors == 1
    assert not any(edge.source == "app.py#store" and edge.evidence == "bound" for edge in graph.edges)
    assert graph.statistics["boundDispatch"]["scanIncomplete"] is True  # type: ignore[index]
