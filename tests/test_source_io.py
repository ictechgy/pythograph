"""자원 상한은 읽기 전에 적용하고 불완전한 순회는 근거 등급까지 전달한다."""

from __future__ import annotations

import ast
import io
import json
import os
import string
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

import pytest

from pythograph.graph.build import build_graph
from pythograph.source import inputs as inputs_module
from pythograph.source import project as project_module
from pythograph.source import syntax
from pythograph.source.project import Project, ReadFailure, SourceModule, source_budget_reason, syntax_nodes_reason
from pythograph.source.syntax import MAX_CHAIN_TOKENS
from tests.conftest import GENERATED_AT, REPOSITORY, SMALL_READ_PEAK_BYTES, allocation_peak, run_cli

MakeProject = Callable[[dict[str, str]], Path]

#: 파싱 총량 예산을 인자로 바꾼 뒤 네 명령을 차례로 실행해 (종료 코드, 출력, 오류)를 그대로 내는 별도 프로세스
#: 스크립트다(인자: 프로젝트, 시각, 예산 바이트).
_BUDGETED_COMMANDS = r"""
import io
import sys

from pythograph.cli.main import main
from pythograph.source import project

project.MAX_TOTAL_SOURCE_BYTES = int(sys.argv[3])
for command in (["graph"], ["schema"], ["routes", "--role", "server"], ["routes", "--role", "client"]):
    out, err = io.StringIO(), io.StringIO()
    code = main([*command, "--project", sys.argv[1], "--generated-at", sys.argv[2]], out, err)
    print(code, out.getvalue(), err.getvalue())
"""


class _CountingParser:
    """`source.project`가 부르는 파서(`parse_guarded`) 대역: 파서에 들어간 파일 이름만 기록하고 실제 파서로 넘긴다.

    전역 `ast.parse`를 바꾸면 pytest가 실패를 보고하며 부르는 파서까지 바뀌므로 그 모듈이 쓰는 이름 하나만 바꾼다.
    """

    def __init__(self) -> None:
        """기록을 비운다."""
        self.parsed: list[str] = []

    def __call__(
        self, text: str, filename: str, mode: Literal["exec", "eval"], bound: int, depth_limit: int, node_limit: int
    ) -> tuple[ast.AST | None, Literal["depth", "nodes"] | None]:
        """파일 이름을 기록하고 실제 파서로 파싱한다.

        Args:
            text: 소스.
            filename: 파일 이름.
            mode: 파싱 방식.
            bound: 사슬 토큰 수의 상한.
            depth_limit: 트리 깊이 상한.
            node_limit: 트리 노드 수 상한.

        Returns:
            (트리, 상한 초과 이유).
        """
        self.parsed.append(filename)
        return syntax.parse_guarded(text, filename, mode, bound, depth_limit, node_limit)


def _write_sources(root: Path, sources: dict[str, bytes]) -> None:
    """바이트 수가 중요한 소스를 그대로 쓴다(`make_project`는 들여쓰기와 앞 줄바꿈을 걷어 낸다).

    Args:
        root: 프로젝트 루트.
        sources: 상대 경로 → 내용.
    """
    for relative, content in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


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


@pytest.mark.parametrize("method", ["module", "read_text"])
def test_small_source_reads_reserve_no_budget_sized_buffer(make_project: MakeProject, method: str) -> None:
    """작은 소스 하나를 읽을 때 4 MiB 상한만큼 버퍼를 미리 잡지 않는다.

    `read(4 MiB + 1)`은 22바이트 파일에도 상한 크기 버퍼를 먼저 할당해(할당 최고점 약 4.2 MB, 지금 수십 KB), 빠듯한
    `RLIMIT_AS`(`ulimit -v`)에서 작은 프로젝트의 읽기도 MemoryError(내부 오류 2)로 끝났다.
    """
    content = "def f():\n    return 1\n"
    root = make_project({"pkg/__init__.py": "", "pkg/mod.py": content})
    project = Project.open(root)
    project.read_text("pkg/__init__.py")  # 처음 한 번만 드는 할당(지연 import 등)을 재기 전에 치른다.
    results: list[object] = []
    peak = allocation_peak(lambda: results.append(getattr(project, method)("pkg/mod.py")))
    assert peak < SMALL_READ_PEAK_BYTES
    # `make_project`는 Windows에서 텍스트를 CRLF로 쓰므로(체크아웃과 같은 입력) 기대값은 디스크의 바이트다.
    written = (root / "pkg" / "mod.py").read_bytes().decode("utf-8")
    assert isinstance(results[0], SourceModule) if method == "module" else results == [written]


class _RecordingBytes(io.BytesIO):
    """`read` 요청 크기를 기록하는 이진 핸들."""

    def __init__(self, data: bytes) -> None:
        """내용을 담고 기록을 비운다.

        Args:
            data: 내용.
        """
        super().__init__(data)
        self.requests: list[int | None] = []

    def read(self, size: int | None = -1, /) -> bytes:
        """요청 크기를 기록하고 읽는다."""
        self.requests.append(size)
        return super().read(size)


class _RecordingText(io.StringIO):
    """`read` 요청 크기를 기록하는 텍스트 핸들."""

    def __init__(self, data: str) -> None:
        """내용을 담고 기록을 비운다.

        Args:
            data: 내용.
        """
        super().__init__(data)
        self.requests: list[int | None] = []

    def read(self, size: int | None = -1, /) -> str:
        """요청 크기를 기록하고 읽는다."""
        self.requests.append(size)
        return super().read(size)


@pytest.mark.parametrize("kind", ["bytes", "text"])
@pytest.mark.parametrize(
    ("length", "limit", "size_hint", "requests", "read_length"),
    [
        pytest.param(10, 100, 10, [11], 10, id="known-size-reads-once"),
        pytest.param(10, 100, 0, [4, 4, 4], 10, id="unknown-size-in-chunks"),
        pytest.param(8, 100, 0, [4, 4, 4], 8, id="ends-on-a-chunk-boundary"),
        pytest.param(20, 9, 0, [4, 4, 2], 10, id="stops-one-past-the-limit"),
        pytest.param(10, 100, 5, [6, 4, 4], 10, id="file-grew-after-fstat"),
        pytest.param(20, 9, 50, [10], 10, id="known-size-over-the-limit"),
        pytest.param(10, -2, 0, [0], 0, id="negative-limit-never-reads-all"),
        pytest.param(0, 100, 0, [4], 0, id="empty"),
    ],
)
def test_bounded_reads_request_at_most_what_remains(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    length: int,
    limit: int,
    size_hint: int,
    requests: list[int],
    read_length: int,
) -> None:
    """`read_bounded`는 알려진 크기 + 1이나 조각 크기만 청하고, 상한 + 1개나 짧은 읽기(끝)에서 멈춘다.

    청하는 크기가 상한 + 1을 넘지 않으므로 큰 파일도 상한만큼만 읽고, 작은 입력에는 상한 크기 버퍼를 잡지 않는다.
    """
    monkeypatch.setattr(inputs_module, "READ_CHUNK", 4)
    text = (string.ascii_letters * 2)[:length]
    handle = _RecordingBytes(text.encode()) if kind == "bytes" else _RecordingText(text)
    result = inputs_module.read_bounded(handle, limit, size_hint)  # type: ignore[type-var]
    expected = text[:read_length]
    assert result == (expected.encode() if kind == "bytes" else expected)
    assert handle.requests == requests


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


def test_source_budget_skips_modules_that_would_exceed_it_before_parsing(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실행 하나의 파싱 총량 예산을 넘게 할 모듈은 파서를 부르지 않고 실패로 남기고, 뒤 모듈은 남은 예산으로 읽는다.

    파싱한 트리는 실행이 끝날 때까지 남으므로 예산은 파싱에 성공한 모듈의 바이트만 센다. 합이 예산과 같아지는 모듈과
    빈 모듈은 읽는다. 파서를 부르지 않는 것이 요점이다(조밀한 소스는 파싱 도중에 메모리를 쓴다).
    """
    sources = {
        "a.py": b"A = 1\n",  # 6바이트, 합 6
        "b.py": b"B = [1, 2, 3, 4, 5]\n",  # 20바이트, 합이 26이 되므로 건너뛴다
        "c.py": b"def broken(:\n",  # 13바이트, 파싱하지만 문법 오류라 합은 그대로 6
        "d.py": b"D = 12345678\n",  # 13바이트, 합 19
        "e.py": b"E = 1\n",  # 6바이트, 합이 25가 되므로 건너뛴다
        "f.py": b"\n",  # 1바이트, 합 20(예산과 같음)
        "g.py": b"",  # 0바이트, 합 20
    }
    root = make_project({})
    _write_sources(root, sources)
    monkeypatch.setattr(project_module, "MAX_TOTAL_SOURCE_BYTES", 20)
    counting = _CountingParser()
    monkeypatch.setattr(project_module, "parse_guarded", counting)
    project = Project.open(root)
    outcomes = {path: project.module(path) for path in project.python_files()}
    budget = source_budget_reason()
    assert {path: getattr(outcome, "reason", "parsed") for path, outcome in outcomes.items()} == {
        "a.py": "parsed",
        "b.py": budget,
        "c.py": "syntax error or syntax newer than the running Python",
        "d.py": "parsed",
        "e.py": budget,
        "f.py": "parsed",
        "g.py": "parsed",
    }
    assert counting.parsed == ["a.py", "c.py", "d.py", "f.py", "g.py"]
    assert (project.parsed_source_bytes, project.budget_skipped_modules) == (20, 2)


@pytest.mark.parametrize(
    ("failing", "reason"),
    [
        (b"def broken(:\n", "syntax error or syntax newer than the running Python"),
        (b"X = x" + b"[0]" * 200 + b"\n", "source nesting too deep to parse"),
        (b"X = '\xff'\n", "file is not valid UTF-8"),
    ],
)
def test_failed_modules_do_not_spend_the_source_budget(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch, failing: bytes, reason: str
) -> None:
    """트리를 남기지 않는 실패(문법 오류·너무 깊은 중첩·UTF-8 아님)는 예산을 쓰지 않아 뒤 모듈을 그대로 읽는다."""
    following = b"OK = 1\n"
    root = make_project({})
    _write_sources(root, {"a.py": failing, "b.py": following})
    monkeypatch.setattr(project_module, "MAX_TOTAL_SOURCE_BYTES", max(len(failing), len(following)))
    project = Project.open(root)
    first, second = project.module("a.py"), project.module("b.py")
    assert isinstance(first, ReadFailure) and first.reason == reason
    assert isinstance(second, SourceModule)
    assert (project.parsed_source_bytes, project.budget_skipped_modules) == (len(following), 0)


def test_source_budget_is_checked_before_the_chain_guard(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """예산 판정은 3.10에서만 하는 사슬 판정보다 먼저라, 예산을 넘는 모듈의 이유와 셈은 파이썬 버전과 무관하다.

    사슬 판정이 늘 걸리는 3.10을 흉내 내도(대역) 예산을 넘는 모듈은 예산 이유로 남고, 예산 안의 모듈만 사슬 이유다.
    """
    root = make_project({})
    # 6바이트(합 6), 7바이트(합 13 이하라 사슬 판정, 트리가 없어 합은 그대로), 11바이트(합 17이라 예산 초과)
    _write_sources(root, {"a.py": b"A = 1\n", "b.py": b"B = 22\n", "c.py": b"C = 333333\n"})
    monkeypatch.setattr(project_module, "MAX_TOTAL_SOURCE_BYTES", 13)
    monkeypatch.setattr(project_module, "chain_bound", lambda text: 0 if text.startswith("A") else MAX_CHAIN_TOKENS + 1)
    project = Project.open(root)
    outcomes = [project.module(path) for path in ("a.py", "b.py", "c.py")]
    assert [getattr(outcome, "reason", "parsed") for outcome in outcomes] == [
        "parsed",
        "source nesting too deep to parse",
        source_budget_reason(),
    ]
    assert (project.parsed_source_bytes, project.budget_skipped_modules) == (6, 1)


def test_module_over_the_syntax_node_limit_keeps_no_tree_and_spends_no_budget(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """구문 트리 노드가 상한보다 많은 모듈은 트리를 버리고 읽지 못한 소스로 남기며 예산을 쓰지 않는다. 상한과 같으면
    읽는다.

    파일당 바이트 상한(4 MiB)만으로는 리터럴만 빽빽한 모듈 하나가 약 280만 노드(약 0.9 GB)를 남겨, 그런 파일 몇 개로
    메모리가 고갈됐다.
    """
    sources = {"a.py": b"A = 1\n", "b.py": b"B = [1, 2, 3, 4, 5]\n", "c.py": b"C = [1, 2, 3, 4, 5, 6]\n"}
    nodes = {path: len(list(ast.walk(ast.parse(text)))) for path, text in sources.items()}
    root = make_project({})
    _write_sources(root, sources)
    monkeypatch.setattr(project_module, "MAX_MODULE_SYNTAX_NODES", nodes["b.py"])
    project = Project.open(root)
    outcomes = {path: project.module(path) for path in sources}
    assert isinstance(outcomes["a.py"], SourceModule) and isinstance(outcomes["b.py"], SourceModule)
    failure = outcomes["c.py"]
    assert isinstance(failure, ReadFailure)
    assert failure.reason == syntax_nodes_reason() == f"syntax tree larger than {nodes['b.py']:,} nodes"
    assert project.parsed_source_bytes == len(sources["a.py"]) + len(sources["b.py"])
    assert project.budget_skipped_modules == 0


def test_syntax_node_limit_reason_names_the_default_limit() -> None:
    """기본 상한은 README의 1,000,000 노드다(실측한 가장 큰 실제 모듈의 약 5배)."""
    assert project_module.MAX_MODULE_SYNTAX_NODES == 1_000_000
    assert syntax_nodes_reason() == "syntax tree larger than 1,000,000 nodes"


@pytest.mark.parametrize(
    ("command", "prefix"),
    [
        (["graph"], "unparsed-files:"),
        (["schema"], "unreadable-sources:"),
        (["routes", "--role", "server"], "route-coverage:"),
        (["routes", "--role", "client"], "route-call-coverage:"),
    ],
)
def test_syntax_node_limit_is_reported_by_every_producer(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch, command: list[str], prefix: str
) -> None:
    """노드 상한을 넘는 모듈은 명령마다 읽지 못한 소스 한계로 남고 명령은 성공한다. graph는 파싱하지 못한 파일 수에
    넣고 분모·원인을 함께 알리며 닫힌 세계 `bound` 증명을 연다. 경로·절대 경로는 싣지 않는다."""
    root = make_project(
        {
            "app.py": "def main():\n    return 1\n",
            "pkg/__init__.py": "",
            "pkg/data.py": "DATA = [" + "1, " * 100 + "]\n",
        }
    )
    monkeypatch.setattr(project_module, "MAX_MODULE_SYNTAX_NODES", 50)
    code, out, err = run_cli([*command, "--project", str(root), "--generated-at", GENERATED_AT])
    assert code == 0, err
    document = json.loads(out)
    limitations = document["limitations"]
    if command == ["graph"]:
        assert any(line.startswith(f"{prefix} 1 of 3 graph-eligible Python files") for line in limitations)
        assert document["statistics"]["sourceAnalysis"]["graphFiles"]["failureReasons"] == {syntax_nodes_reason(): 1}
        assert document["statistics"]["unparsedFiles"] == 1
        assert document["statistics"]["boundDispatch"]["scanIncomplete"] is True
    else:
        assert f"{prefix} 1 Python files could not be analyzed: {syntax_nodes_reason()}" in limitations
    text = json.dumps(limitations)
    assert str(root) not in text and "pkg/data.py" not in text


def test_source_budget_reason_names_the_budget_and_the_way_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """실패 이유는 원인(실행당 파싱 총량 상한)과 방향(`--project`를 좁힌다)을 담는다. 기본 상한은 README의 64 MiB다."""
    assert (
        source_budget_reason()
        == "the run's total parsed source would exceed its 64 MiB budget; pass a narrower --project"
    )
    monkeypatch.setattr(project_module, "MAX_TOTAL_SOURCE_BYTES", 1000)
    assert "1,000 bytes budget" in source_budget_reason()


@pytest.mark.parametrize(
    ("command", "prefix"),
    [
        (["graph"], "source-budget:"),
        (["schema"], "unreadable-sources:"),
        (["routes", "--role", "server"], "route-coverage:"),
        (["routes", "--role", "client"], "route-call-coverage:"),
    ],
)
def test_source_budget_is_reported_by_every_producer(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch, command: list[str], prefix: str
) -> None:
    """예산을 넘겨 파싱하지 않은 모듈은 명령마다 읽지 못한 소스 한계에 이유째 남고 명령은 성공한다(부분 결과).

    graph는 그 파일을 파싱하지 못한 파일로 세고 닫힌 세계 `bound` 증명을 연다. 경로·절대 경로는 싣지 않는다.
    """
    root = make_project(
        {
            "app.py": "def main():\n    return 1\n",
            "pkg/__init__.py": "",
            "pkg/data.py": "DATA = [" + "1, " * 100 + "]\n",
        }
    )
    monkeypatch.setattr(project_module, "MAX_TOTAL_SOURCE_BYTES", 64)
    code, out, err = run_cli([*command, "--project", str(root), "--generated-at", GENERATED_AT])
    assert code == 0, err
    document = json.loads(out)
    assert any(
        line.startswith(f"{prefix} 1 Python files could not be analyzed: {source_budget_reason()}")
        for line in document["limitations"]
    )
    limitations = json.dumps(document["limitations"])
    assert str(root) not in limitations and "pkg/data.py" not in limitations
    if command == ["graph"]:
        assert document["statistics"]["unparsedFiles"] == 1
        assert document["statistics"]["boundDispatch"]["scanIncomplete"] is True


def test_source_budget_skips_the_same_modules_under_any_hash_seed(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """예산이 빼는 모듈은 명령의 파싱 순서(경로 순, import로 먼저 닿은 모듈)로 정해지므로 해시 시드와 무관하다.

    문자열 해시 시드를 바꾼 별도 프로세스 셋에서 네 명령의 출력이 바이트 단위로 같고, 명령마다 예산 한계가 실제로
    실린다. import가 이어진 앱이라 순회 순서가 시드에 따라 달라지면(집합 순회 등) 빠지는 모듈과 출력이 달라진다.
    """
    root = make_project(
        {
            "app/__init__.py": """
                from flask import Flask

                from app.views import bp

                app = Flask(__name__)
                app.register_blueprint(bp, url_prefix="/api")
            """,
            "app/views.py": """
                import requests
                from flask import Blueprint

                from app.models import Item

                bp = Blueprint("items", __name__)


                @bp.route("/items")
                def items():
                    requests.get("https://example.com/stock")
                    return Item.query.all()
            """,
            "app/models.py": """
                from flask_sqlalchemy import SQLAlchemy

                db = SQLAlchemy()


                class Item(db.Model):
                    __tablename__ = "items"
                    id = db.Column(db.Integer, primary_key=True)
            """,
            "app/reports.py": """
                from app.models import db


                def totals():
                    return db.session.execute("SELECT count(*) FROM items")
            """,
            "tools/seed.py": "ROWS = [" + "1, " * 40 + "]\n",
        }
    )
    budget = 420
    monkeypatch.setattr(project_module, "MAX_TOTAL_SOURCE_BYTES", budget)
    reason = source_budget_reason()
    outputs: list[str] = []
    for seed in ("0", "1", "2"):
        result = subprocess.run(
            [sys.executable, "-c", _BUDGETED_COMMANDS, str(root), GENERATED_AT, str(budget)],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPOSITORY,
            timeout=60,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0].count(f"could not be analyzed: {reason}") == 4
