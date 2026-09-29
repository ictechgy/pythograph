"""`graph`·`reach`·`impact` 명령: 인자 해석, 그래프 생성, 문서 조립.

`reach`는 root가 기대는 쪽(`dependencies`), `impact`는 root에 기대는 쪽(`dependents`)을 isthmus `language-traversal`
v1로 낸다. 그래프 정점이 아닌 root가 섞이면 나머지 root로 순회한 문서를 쓰고 64로 끝난다(tsograph·cartograph와 같은
root-not-found 규칙). 문서 없는 순수 사용법 오류도 64이며 표준 출력이 비어 있어 구별된다.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from pythograph import __version__
from pythograph.cli.common import InputError, UsageError, parse_timestamp, resolve_project
from pythograph.exchange.document import DocumentLimitError, encode_document
from pythograph.graph.build import build_graph
from pythograph.graph.document import (
    GraphHeader,
    TraversalInput,
    build_graph_document,
    build_traversal_document,
)
from pythograph.graph.model import DISPATCH_MODES, CallGraph
from pythograph.graph.revision import git_revision
from pythograph.graph.roots import FORBIDDEN_CHARACTERS, RootsError, collect_roots
from pythograph.graph.traversal import (
    MAX_REACHED,
    MAX_TRAVERSAL_DEPTH,
    TraversalRequest,
    TraversalResult,
    traverse,
)
from pythograph.source.project import Project

GRAPH_USAGE = """Usage: pythograph graph --project <root> [--include-tests] [--revision <id>]
                        [--generated-at <timestamp>] [--format json]

Build the project's Python call graph with the standard-library ast and write a pythograph-graph v1
snapshot (nodes, edges with evidence tiers, unresolved-call counts, limitations) to stdout.

Options:
  --project <root>           Project root; node ids and locations are relative to it
  --include-tests            Also make test sources graph nodes
  --revision <id>            Source revision to record (default: git HEAD when the work tree is clean)
  --generated-at <timestamp> Fixed generatedAt (YYYY-MM-DDTHH:MM:SS.sssZ) for byte-identical output
  --format json              Output format (json is the only format)

Exit codes: 0 success, 2 unreadable project or oversized output, 64 usage error.
"""

_TRAVERSAL_OPTIONS = """Options:
  --project <root>           Project root; ids are pythograph symbol ids (routes and schema symbol.usr)
  --dispatch <mode>          direct (default), bound (currently the direct graph), or candidates
                             (also subclass overrides as candidate edges)
  --max-depth <n>            Maximum edge count from a root, 1-128 (default 128)
  --max-reached <n>          Maximum reached symbols, 1-100000 (default 100000)
  --roots-from <file|->      More roots from a JSON string array or a bridge-facts document (- is stdin)
  --include-tests            Also make test sources graph nodes
  --revision <id>            Source revision to record (default: git HEAD when the work tree is clean)
  --generated-at <timestamp> Fixed generatedAt (YYYY-MM-DDTHH:MM:SS.sssZ) for byte-identical output
  --format json              Output format (json is the only format)

Ids that are not graph nodes are listed without symbol (root-not-found); the document is still
written and the command exits 64. Exit codes: 0 success, 2 unreadable project or oversized output,
64 usage error (empty stdout) or root-not-found (document written).
"""

REACH_USAGE = (
    """Usage: pythograph reach --project <root> [options] [--] <id>...

Write the symbols the roots depend on (direction "dependencies") as an isthmus language-traversal
v1 document.

"""
    + _TRAVERSAL_OPTIONS
)

IMPACT_USAGE = (
    """Usage: pythograph impact --project <root> [options] [--] <id>...

Write the symbols that depend on the roots (direction "dependents") as an isthmus
language-traversal v1 document.

"""
    + _TRAVERSAL_OPTIONS
)

#: graph 명령의 값 옵션이다.
_GRAPH_VALUES = ("--project", "--format", "--generated-at", "--revision")

#: reach·impact 명령의 값 옵션이다.
_TRAVERSAL_VALUES = (*_GRAPH_VALUES, "--dispatch", "--max-depth", "--max-reached", "--roots-from")

#: 불리언 옵션이다.
_FLAGS = ("--include-tests", "--help")

#: `--revision` 최대 길이다.
MAX_REVISION_LENGTH = 256

#: 양의 정수 인자 형식이다.
_INTEGER = re.compile(r"[1-9][0-9]{0,6}")


class RootNotFoundExit(Exception):
    """정점이 아닌 root가 있어 문서를 쓰고 64로 끝낸다.

    Attributes:
        text: 표준 출력에 쓸 문서.
        missing: 정점이 아닌 root 수.
    """

    def __init__(self, text: str, missing: int) -> None:
        """예외를 만든다.

        Args:
            text: 문서.
            missing: 정점이 아닌 root 수.
        """
        super().__init__("root-not-found")
        self.text = text
        self.missing = missing


@dataclass(frozen=True)
class CommandLine:
    """해석한 인자.

    Attributes:
        values: 값 옵션.
        flags: 불리언 옵션.
        positional: 위치 인자.
    """

    values: dict[str, str]
    flags: set[str]
    positional: list[str]


def parse_command_line(arguments: list[str], value_flags: tuple[str, ...], usage: str, positional: bool) -> CommandLine:
    """옵션과 위치 인자를 나눈다. `--` 뒤는 모두 위치 인자다.

    Args:
        arguments: 인자 목록.
        value_flags: 허용하는 값 옵션.
        usage: 오류 문구에 붙일 사용법.
        positional: 위치 인자를 받는지.

    Returns:
        해석한 인자.

    Raises:
        UsageError: 모르는·반복·값 없는 옵션, 받지 않는 위치 인자.
    """
    result = CommandLine({}, set(), [])
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--" and positional:
            result.positional.extend(arguments[index + 1 :])
            break
        if not token.startswith("--"):
            if not positional:
                raise UsageError("unexpected positional argument.\n" + usage)
            result.positional.append(token)
            index += 1
            continue
        index = _parse_option(arguments, index, value_flags, usage, result)
    return result


def _parse_option(
    arguments: list[str], index: int, value_flags: tuple[str, ...], usage: str, result: CommandLine
) -> int:
    """옵션 하나를 해석한다.

    Args:
        arguments: 인자 목록.
        index: 옵션 위치.
        value_flags: 허용하는 값 옵션.
        usage: 사용법.
        result: 채울 결과.

    Returns:
        다음 인자 위치.

    Raises:
        UsageError: 잘못된 옵션.
    """
    name, has_inline, inline = arguments[index].partition("=")
    if name in _FLAGS and not has_inline and name not in result.flags:
        result.flags.add(name)
        return index + 1
    if name not in value_flags or name in result.values:
        raise UsageError("unknown or repeated option.\n" + usage)
    value = inline if has_inline else (arguments[index + 1] if index + 1 < len(arguments) else "")
    if not value or (not has_inline and value.startswith("--")):
        raise UsageError(f"{name} needs a value.\n" + usage)
    result.values[name] = value
    return index + (1 if has_inline else 2)


def run_graph(arguments: list[str]) -> str:
    """graph 명령을 실행한다.

    Args:
        arguments: graph 뒤 인자.

    Returns:
        JSON 문서.

    Raises:
        UsageError: 사용법 오류.
        InputError: 입력 오류.
    """
    line = parse_command_line(arguments, _GRAPH_VALUES, GRAPH_USAGE, positional=False)
    if "--help" in line.flags:
        return GRAPH_USAGE
    root, header = _common(line, GRAPH_USAGE)
    graph = build_graph(Project.open(root), "--include-tests" in line.flags)
    return _encode(build_graph_document(header, graph))


def run_traversal(arguments: list[str], direction: str, stdin: TextIO | None = None) -> str:
    """reach·impact 명령을 실행한다.

    Args:
        arguments: 명령 뒤 인자.
        direction: `dependencies`(reach) 또는 `dependents`(impact).
        stdin: 표준 입력(`--roots-from -`, 테스트 주입용).

    Returns:
        JSON 문서.

    Raises:
        UsageError: 사용법 오류.
        InputError: 입력 오류.
        RootNotFoundExit: 정점이 아닌 root가 있을 때(문서를 싣는다).
    """
    usage = REACH_USAGE if direction == "dependencies" else IMPACT_USAGE
    line = parse_command_line(arguments, _TRAVERSAL_VALUES, usage, positional=True)
    if "--help" in line.flags:
        return usage
    dispatch = line.values.get("--dispatch", "direct")
    if dispatch not in DISPATCH_MODES:
        raise UsageError("--dispatch must be direct, bound, or candidates.")
    max_depth = _bounded(line.values.get("--max-depth"), MAX_TRAVERSAL_DEPTH, "--max-depth")
    max_reached = _bounded(line.values.get("--max-reached"), MAX_REACHED, "--max-reached")
    try:
        requested = collect_roots(line.positional, line.values.get("--roots-from"), stdin or sys.stdin)
    except RootsError as error:
        raise UsageError(str(error)) from error
    root, header = _common(line, usage)
    graph = build_graph(Project.open(root), "--include-tests" in line.flags)
    request = TraversalRequest(requested, direction, max_depth, max_reached, dispatch)
    document, missing = traversal_document(graph, header, request)
    text = _encode(document)
    if missing:
        raise RootNotFoundExit(text, missing)
    return text


def traversal_document(
    graph: CallGraph, header: GraphHeader, request: TraversalRequest
) -> tuple[dict[str, object], int]:
    """정점인 root로만 순회하고, root 인덱스를 요청 순서로 옮겨 문서를 만든다.

    Args:
        graph: 호출 그래프.
        header: 머리 필드.
        request: 요청 순서의 전체 root를 담은 순회 요청.

    Returns:
        (문서, 정점이 아닌 root 수).
    """
    nodes = graph.node_map()
    resolved = tuple(root for root in request.root_ids if root in nodes)
    missing = frozenset(root for root in request.root_ids if root not in nodes)
    result = TraversalResult((), (), False, False)
    if resolved:
        result = remap_roots(traverse(graph, replace(request, root_ids=resolved)), resolved, request.root_ids)
    data = TraversalInput(header, graph, request.direction, request.dispatch, request.root_ids, missing, result)
    return build_traversal_document(data), len(missing)


def remap_roots(result: TraversalResult, resolved: tuple[str, ...], requested: tuple[str, ...]) -> TraversalResult:
    """정점인 root 순서의 인덱스를 요청 순서의 인덱스로 옮긴다(순서를 보존하므로 규칙 결과가 그대로다).

    Args:
        result: 순회 결과.
        resolved: 정점인 root(요청 순서 유지).
        requested: 요청 순서의 전체 root.

    Returns:
        옮긴 순회 결과.
    """
    if resolved == requested:
        return result
    position = {root: index for index, root in enumerate(requested)}
    mapping = [position[root] for root in resolved]
    reached = tuple(replace(entry, roots=tuple(mapping[index] for index in entry.roots)) for entry in result.reached)
    return replace(result, reached=reached)


def _bounded(value: str | None, limit: int, name: str) -> int:
    """1~limit 정수 인자를 해석한다(없으면 limit).

    Args:
        value: 값.
        limit: 상한(기본값).
        name: 옵션 이름.

    Returns:
        정수.

    Raises:
        UsageError: 형식·범위 위반.
    """
    if value is None:
        return limit
    if _INTEGER.fullmatch(value) is None or int(value) > limit:
        raise UsageError(f"{name} takes an integer from 1 to {limit}.")
    return int(value)


def _common(line: CommandLine, usage: str) -> tuple[Path, GraphHeader]:
    """공통 인자(project·format·revision·generated-at)를 검증하고 머리 필드를 만든다.

    Args:
        line: 해석한 인자.
        usage: 사용법.

    Returns:
        (프로젝트 realpath, 머리 필드).

    Raises:
        UsageError: 사용법 오류.
    """
    if line.values.get("--format", "json") != "json":
        raise UsageError("--format supports only json.")
    project_argument = line.values.get("--project")
    if project_argument is None:
        raise UsageError("--project <root> is required.\n" + usage)
    revision = line.values.get("--revision")
    if revision is not None and (len(revision) > MAX_REVISION_LENGTH or FORBIDDEN_CHARACTERS.search(revision)):
        raise UsageError(f"--revision must be 1-{MAX_REVISION_LENGTH} characters without control characters.")
    generated_at = parse_timestamp(line.values.get("--generated-at"))
    root = resolve_project(project_argument)
    header = GraphHeader(
        tool_version=__version__,
        generated_at=generated_at or datetime.now(timezone.utc),
        project=root.as_posix(),
        revision=revision if revision is not None else git_revision(root),
    )
    return root, header


def _encode(document: dict[str, object]) -> str:
    """문서를 직렬화한다.

    Args:
        document: 문서.

    Returns:
        JSON 문자열.

    Raises:
        InputError: 출력이 상한을 넘을 때.
    """
    try:
        return encode_document(document)
    except DocumentLimitError as error:
        raise InputError(str(error)) from error
