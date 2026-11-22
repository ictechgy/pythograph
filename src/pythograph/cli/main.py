"""pythograph 명령행 진입점과 종료 코드 계약.

종료 코드: 0 성공(사실 0건도 성공이며 완전성의 증거가 아니다), 2 입력 오류(읽을 수 없는 프로젝트,
isthmus 상한을 넘는 출력, 예기치 못한 내부 오류), 64 사용법 오류. 1은 예약이다(파이썬의 잡히지 않은
예외가 1로 끝나므로, 모든 예외를 잡아 2로 바꾼다). 오류 문구에는 원인과 해결 방향을 담되 입력 원문과
절대 경로를 넣지 않는다.
"""

from __future__ import annotations

import codecs
import contextlib
import errno
import io
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from pythograph import __version__
from pythograph.cli.change_command import CHANGE_USAGE, run_change
from pythograph.cli.common import InputError, UsageError, parse_timestamp, resolve_project
from pythograph.cli.graph_commands import (
    GRAPH_USAGE,
    IMPACT_USAGE,
    REACH_USAGE,
    RootNotFoundExit,
    run_graph,
    run_traversal,
)
from pythograph.exchange.client_document import build_client_document
from pythograph.exchange.document import DocumentHeader, DocumentLimitError, build_document, encode_document
from pythograph.exchange.persistence import build_persistence_document
from pythograph.persistence.command import SchemaOptions, extract_persistence
from pythograph.routes.client.extract import ClientOptions, extract_client_calls
from pythograph.routes.client.wrappers import WrapperDecl, WrapperDeclarationError, load_wrappers
from pythograph.routes.command import FrameworkChoiceError, RouteOptions, extract_routes
from pythograph.source.project import Project
from pythograph.source.syntax import MAX_EXTRACTION_AST_DEPTH, run_on_parse_stack
from pythograph.source.text import FORBIDDEN_CHARACTERS

#: 성공 종료 코드다.
EXIT_OK = 0
#: 입력 오류 종료 코드다.
EXIT_INPUT = 2
#: 사용법 오류 종료 코드다(BSD `EX_USAGE`).
EXIT_USAGE = 64

#: `--service` 최대 길이다.
MAX_SERVICE_LENGTH = 256

MAIN_USAGE = """Usage: pythograph <command> [options]

Static facts for Python services (Django, Django REST framework, Flask, SQLAlchemy) in the isthmus
bridge-facts exchange format. The analyzed project is parsed with the standard-library
ast only; it is never imported or executed.

Commands:
  routes     Server route declarations or client HTTP calls (route-decl / route-call facts)
  schema     Relation and column references (persistence relation-use facts)
  graph      Python call graph snapshot (pythograph-graph v1)
  reach      Symbols the roots depend on (isthmus language-traversal v1)
  impact     Symbols that depend on the roots (isthmus language-traversal v1)
  change-impact Git base/head or working-tree changes with separate before/after paths
  help       Show help for a command

Options:
  --version  Print the version
  --help     Print this help

Exit codes: 0 success, 2 input error, 64 usage error (1 is reserved).
"""

ROUTES_USAGE = """Usage: pythograph routes --role server --project <root> [--service <name>] [--include-tests]
                         [--framework auto|django|flask] [--settings <module>]
                         [--dispatch specificity] [--generated-at <timestamp>] [--format json]
       pythograph routes --role client --project <root> [--wrappers <file>] [--service <name>]
                         [--include-tests] [--generated-at <timestamp>] [--format json]

Scan a Python project and write an isthmus bridge-facts v1 document (platform "python", target
"http") to stdout: route-decl facts for a Django (+ Django REST framework) or Flask server
(--role server), or route-call facts for requests, httpx, aiohttp, and urllib clients and
declared HTTP wrappers (--role client).

Options:
  --role server|client       Declaration side (server) or calling side (client)
  --project <root>           Project root; location paths are relative to it
  --service <name>           Service identity recorded on the document and every fact (client:
                             a wrapper's own service wins on its facts, and the document omits
                             the field when that service differs)
  --include-tests            Also scan test sources; their facts carry testSource: true
  --framework <name>         (server) auto (default), django, or flask
  --settings <module>        (server) Django settings module (default: the DJANGO_SETTINGS_MODULE
                             default in manage.py, wsgi.py, or asgi.py)
  --dispatch specificity     (server) Declare specificity dispatch for a Django project and omit
                             order (an approximation for consumers without registration-order support)
  --wrappers <file>          (client) isthmus http-wrappers v1 declarations; "python" entries apply
  --generated-at <timestamp> Fixed generatedAt (YYYY-MM-DDTHH:MM:SS.sssZ) for byte-identical output
  --format json              Output format (json is the only format)

Exit codes: 0 success, 2 unreadable project or wrappers file, or oversized output, 64 usage error
(including an invalid wrappers declaration).
"""

SCHEMA_USAGE = """Usage: pythograph schema --project <root> [--include-tests] [--settings <module>]
                         [--generated-at <timestamp>] [--format json]

Scan Django models and QuerySets, SQLAlchemy / Flask-SQLAlchemy mappings and queries, and SQL text,
and write an isthmus bridge-facts v1 document (platform "python", target "persistence",
relation-use facts) to stdout. isthmus joins it with a schemagraph catalog document.

Options:
  --project <root>           Project root; location paths are relative to it
  --include-tests            Also scan test sources
  --settings <module>        Django settings module (default: the DJANGO_SETTINGS_MODULE default
                             in manage.py, wsgi.py, or asgi.py)
  --generated-at <timestamp> Fixed generatedAt (YYYY-MM-DDTHH:MM:SS.sssZ) for byte-identical output
  --format json              Output format (json is the only format)

Exit codes: 0 success, 2 unreadable project or oversized output, 64 usage error.
"""

#: schema 명령의 값 옵션이다.
_SCHEMA_VALUE_FLAGS = ("--project", "--format", "--settings", "--generated-at")

#: routes 명령의 값 옵션이다.
_VALUE_FLAGS = (
    "--role",
    "--project",
    "--service",
    "--format",
    "--framework",
    "--settings",
    "--dispatch",
    "--generated-at",
    "--wrappers",
)

#: routes 명령의 불리언 옵션이다.
_BOOLEAN_FLAGS = ("--include-tests", "--help")


@dataclass(frozen=True)
class RoutesArguments:
    """검증한 routes 인자."""

    role: str
    project: str
    service: str | None
    include_tests: bool
    framework: str
    settings_module: str | None
    dispatch: str | None
    generated_at: datetime | None
    wrappers: str | None = None


def main(
    argv: Sequence[str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    """명령을 실행하고 종료 코드를 돌려준다.

    명령의 계산(`run`)은 3.13까지 어떤 허용 소스라도 그 자리에서 파싱할 만큼 스택이 큰 스레드 하나에서 돌고
    (`run_on_parse_stack`), 표준 출력·오류 쓰기는 부른 스레드에서 한다. Ctrl+C는 계산 중이면 계산 스레드를 멈추게 한 뒤,
    쓰는 중이면 쓰기를 끊고 부른 스레드에서 KeyboardInterrupt로 나간다. 어떤 내부 오류도(오류를 알리다 난 MemoryError
    포함) 예약된 1이 아니라 2다.

    Args:
        argv: 인자(없으면 `sys.argv[1:]`).
        stdout: 표준 출력(테스트 주입용).
        stderr: 표준 오류(테스트 주입용).
        stdin: 표준 입력(`--roots-from -`, 테스트 주입용).

    Returns:
        종료 코드.
    """
    try:
        return _main(argv, stdout, stderr, stdin)
    except Exception:  # 마지막 방어선: 오류를 알리는 중에 난 오류(MemoryError 등)도 예약된 1로 새지 않게 한다.
        with contextlib.suppress(Exception):
            (stderr or sys.stderr).write(_LAST_RESORT_MESSAGE)
        return EXIT_INPUT


#: 오류를 알리다 다시 실패했을 때 쓰는 고정 문구다(서식을 만들지 않아 메모리가 모자라도 쓸 수 있게 미리 만든다).
_LAST_RESORT_MESSAGE = "pythograph: internal error; please report it with the command line\n"


def _main(argv: Sequence[str] | None, stdout: TextIO | None, stderr: TextIO | None, stdin: TextIO | None) -> int:
    """`main`의 본문이다. 계산만 전용 스택 스레드에서 돌리고 결과·오류는 이 스레드에서 쓴다.

    Args:
        argv: 인자(없으면 `sys.argv[1:]`).
        stdout: 표준 출력(테스트 주입용).
        stderr: 표준 오류(테스트 주입용).
        stdin: 표준 입력(`--roots-from -`, 테스트 주입용).

    Returns:
        종료 코드.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    out = stdout or _exchange_stdout(sys.stdout)
    # 주입한 스트림은 호출자의 쓰기 방식(줄바꿈 변환 포함)을 따르고, 프로세스 표준 출력만 원시 스트림에 끝까지 쓴다.
    raw = None if stdout is not None else _stdout_raw(out)
    err = stderr or sys.stderr
    source = stdin or sys.stdin
    try:
        text = run_on_parse_stack(lambda: run(arguments, source))
    except RootNotFoundExit as partial:
        _report(err, f"{partial.missing} root id(s) are not graph nodes; the document lists them without symbol")
        return _write(out, err, partial.text, EXIT_USAGE, raw)
    except UsageError as error:
        _report(err, str(error))
        return EXIT_USAGE
    except InputError as error:
        _report(err, str(error))
        return EXIT_INPUT
    except Exception as error:  # 1은 예약이므로 어떤 내부 오류도 2로 바꾼다. 원문은 싣지 않는다.
        _report(err, f"internal error ({type(error).__name__}); please report it with the command line")
        return EXIT_INPUT
    return _write(out, err, text, EXIT_OK, raw)


def _exchange_stdout(stream: TextIO) -> TextIO:
    """프로세스 표준 출력을 교환 형식의 바이트(UTF-8·LF)로 맞춘다.

    문서는 UTF-8 JSON이다(isthmus는 UTF-8로 읽는다). 파이썬은 파이프·파일로 돌린 표준 출력을 로캘 인코딩으로 연다.
    Windows의 ANSI 코드 페이지(예: cp1252)나 UTF-8이 아닌 로캘에서는 그 코드 페이지에 없는 문자(한글 경로·식별자)를
    쓰다 실패해 종료 2였고, 있는 문자는 UTF-8이 아닌 바이트가 됐다. Windows 텍스트 모드는 `\\n`을 CRLF로 바꿔 같은
    입력의 출력 바이트가 플랫폼마다 달랐다.

    오류 처리는 strict로 바꾼다. C·C.UTF-8 로캘과 UTF-8 모드(`PYTHONUTF8`)에서 파이썬이 고른 surrogateescape도 strict가
    되지만, 모든 문서는 쓰기 전에 짝 없는 서러게이트를 거부하므로(`DocumentEncodingError`) 그런 로캘에서 쓰는 바이트는
    같다. 표준 입력은 `--roots-from -`이 읽을 때만 다시 연다(`graph.roots`). `reconfigure`가 없는 대역이나 바꿀 수
    없는 스트림(닫힌 스트림)은 그대로 둔다.

    Args:
        stream: 프로세스 표준 출력(없을 수 있다: 출력을 닫고 실행한 경우).

    Returns:
        같은 스트림.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        with contextlib.suppress(OSError, ValueError):
            reconfigure(encoding="utf-8", errors="strict", newline="\n")
    return stream


def _stdout_raw(stream: TextIO | None) -> io.RawIOBase | None:
    """프로세스 표준 출력 텍스트 래퍼 아래의 원시 바이트 스트림을 돌려준다(문서를 그곳에 직접 끝까지 쓴다).

    `TextIOWrapper`로 문서를 쓰면 두 가지가 틀렸다.

    - `python -u`·`PYTHONUNBUFFERED`(컨테이너 이미지에 흔하다)에서 표준 출력은 `BufferedWriter` 없이 원시 스트림
      (`FileIO`, Windows 콘솔은 `_WindowsConsoleIO`) 위의 래퍼이고, 그 `write`는 원시 스트림이 일부만 쓴 개수(짧은
      쓰기)나 쓰지 못했다는 None을 버린다. 읽는 쪽이 중간에 닫은 파이프, 파일 크기 상한(`RLIMIT_FSIZE`)·디스크
      공간에 닿은 파일, 블로킹하지 않는 파이프에서 잘린 문서를 쓰고도 0으로 끝났다.
    - 버퍼 있는 표준 출력은 블로킹하지 않는 파이프에서 BlockingIOError(2)를 알린 뒤에도 못 쓴 바이트를
      `BufferedWriter`에 남겨, 인터프리터가 끝날 때 다시 비우다 실패해 종료 상태가 계약 밖의 120이 됐다.

    원시 스트림에 직접 쓰면(`_write_all`) 짧은 쓰기를 다시 쓰고, 실패하면 남은 바이트 없이 2로 끝난다.

    Args:
        stream: 표준 출력(없을 수 있다).

    Returns:
        원시 바이트 스트림. 텍스트 래퍼가 아니거나(떼어 낸 래퍼 포함) 그 아래가 원시 스트림·`BufferedWriter`가
        아니면 None.
    """
    if not isinstance(stream, io.TextIOWrapper):
        return None
    binary = stream.buffer  # `detach()`한 래퍼는 None이다.
    if isinstance(binary, io.BufferedWriter):
        binary = binary.raw  # `detach()`한 버퍼는 None이다.
    return binary if isinstance(binary, io.RawIOBase) else None


def _write(out: TextIO | None, err: TextIO | None, text: str, code: int, raw: io.RawIOBase | None = None) -> int:
    """표준 출력에 결과를 쓴다. 닫힌 파이프·닫힌 표준 출력·인코딩 실패·끝까지 쓰지 못함도 1이 아니라 2다.

    Args:
        out: 표준 출력.
        err: 표준 오류.
        text: 출력 문자열.
        code: 쓰기에 성공했을 때의 종료 코드.
        raw: `out`이 프로세스 표준 출력이면 그 아래 원시 바이트 스트림(`_stdout_raw`), 아니면 None.

    Returns:
        종료 코드.
    """
    if out is None:  # 표준 출력을 닫고 실행하면 `sys.stdout`이 None이다.
        _report(err, "could not write the output (standard output is closed); check the output destination")
        return EXIT_INPUT
    try:
        if raw is None:
            out.write(text)
            out.flush()
        else:
            _write_all(out, raw, text)
    except (OSError, UnicodeError) as error:
        _report(err, f"could not write the output ({type(error).__name__}); check the output destination")
        return EXIT_INPUT
    return code


#: 원시 스트림에 쓸 때 한 번에 인코딩하는 문자 수다. 문서 전체를 한꺼번에 인코딩하면 문서만큼 메모리가 더 든다.
_WRITE_CHUNK_CHARACTERS = 1 << 20


def _write_all(out: TextIO, raw: io.RawIOBase, text: str) -> None:
    """텍스트 래퍼 아래 원시 스트림에 문자열을 끝까지 쓴다.

    래퍼(와 그 `BufferedWriter`)를 먼저 비운 뒤 `out`의 인코딩·오류 처리로 조각마다 인코딩하고(상태 있는 인코딩도
    이어지게 증분 인코더를 쓴다), 원시 스트림의 짧은 쓰기는 남은 바이트로 다시 쓴다. 줄바꿈은 바꾸지 않는다: 프로세스
    표준 출력은 `_exchange_stdout`이 LF로 맞춘다.

    Args:
        out: 텍스트 래퍼(먼저 비운다).
        raw: 그 아래 원시 바이트 스트림.
        text: 쓸 문자열.

    Raises:
        BlockingIOError: 블로킹하지 않는 스트림이 더 받지 못한다(`BufferedWriter`와 같은 오류).
        OSError: 쓰기가 실패했거나 원시 스트림이 한 바이트도 받지 않았다(같은 쓰기를 끝없이 되풀이하지 않는다).
        UnicodeError: 인코딩하지 못하는 문자가 있다.
    """
    out.flush()
    encoder = codecs.getincrementalencoder(out.encoding)(out.errors or "strict")
    for start in range(0, len(text), _WRITE_CHUNK_CHARACTERS):
        chunk = text[start : start + _WRITE_CHUNK_CHARACTERS]
        _write_bytes(raw, encoder.encode(chunk, final=start + _WRITE_CHUNK_CHARACTERS >= len(text)))


def _write_bytes(raw: io.RawIOBase, data: bytes) -> None:
    """원시 바이트 스트림에 바이트를 짧은 쓰기 없이 끝까지 쓴다.

    Args:
        raw: 원시 바이트 스트림.
        data: 쓸 바이트.

    Raises:
        BlockingIOError: 블로킹하지 않는 스트림이 더 받지 못한다.
        OSError: 쓰기가 실패했거나 한 바이트도 받지 않았다(또는 받은 바이트 수가 맞지 않는다).
    """
    view = memoryview(data)
    while view:
        written = raw.write(view)
        if written is None:
            raise BlockingIOError(errno.EAGAIN, "the output would block")
        if not 0 < written <= len(view):
            raise OSError(errno.EIO, "the output accepted no bytes or reported an invalid count")
        view = view[written:]


def _report(err: TextIO | None, message: str) -> None:
    """표준 오류에 한 줄을 쓴다. 표준 오류 자체가 실패해도 종료 코드 계약을 지키도록 오류를 삼키지 않고 무시한다.

    Args:
        err: 표준 오류(닫고 실행하면 None).
        message: 문구.
    """
    if err is None:  # 표준 오류를 닫고 실행했다. 종료 코드가 원인을 전한다.
        return
    try:
        err.write(f"pythograph: {message}\n")
    except (OSError, UnicodeError):
        # 표준 오류가 닫혔으면 알릴 곳이 없다. 종료 코드가 원인을 전한다.
        return


def run(arguments: list[str], stdin: TextIO | None = None) -> str:
    """인자를 해석해 표준 출력에 쓸 문자열을 만든다.

    Args:
        arguments: 인자 목록.
        stdin: 표준 입력(없으면 `sys.stdin`).

    Returns:
        출력 문자열.

    Raises:
        UsageError: 사용법 오류.
        InputError: 입력 오류.
    """
    if not arguments:
        raise UsageError("missing command.\n" + MAIN_USAGE)
    command, rest = arguments[0], arguments[1:]
    if command in ("--help", "-h"):
        return MAIN_USAGE
    if command == "--version":
        return f"{__version__}\n"
    if command == "help":
        return _help(rest)
    if command == "routes":
        return _routes(rest)
    if command == "schema":
        return _schema(rest)
    if command == "graph":
        return run_graph(rest)
    if command == "change-impact":
        return run_change(rest)
    if command in ("reach", "impact"):
        return run_traversal(rest, "dependencies" if command == "reach" else "dependents", stdin)
    raise UsageError("unknown command.\n" + MAIN_USAGE)


def _help(rest: list[str]) -> str:
    """`help [command]`를 처리한다.

    Args:
        rest: 명령 이름.

    Returns:
        도움말.

    Raises:
        UsageError: 모르는 명령.
    """
    if not rest:
        return MAIN_USAGE
    if rest == ["routes"]:
        return ROUTES_USAGE
    if rest == ["schema"]:
        return SCHEMA_USAGE
    topics = {"graph": GRAPH_USAGE, "reach": REACH_USAGE, "impact": IMPACT_USAGE, "change-impact": CHANGE_USAGE}
    if len(rest) == 1 and rest[0] in topics:
        return topics[rest[0]]
    raise UsageError("unknown command for help.\n" + MAIN_USAGE)


def _routes(rest: list[str]) -> str:
    """routes 명령을 실행한다.

    Args:
        rest: routes 뒤 인자.

    Returns:
        JSON 문서.

    Raises:
        UsageError: 사용법 오류.
        InputError: 입력 오류.
    """
    values, flags = parse_flags(rest)
    if "--help" in flags:
        return ROUTES_USAGE
    parsed = validate_routes(values, flags)
    root = resolve_project(parsed.project)
    header = DocumentHeader(
        tool_version=__version__,
        generated_at=parsed.generated_at or datetime.now(timezone.utc),
        project=root.as_posix(),
        service=parsed.service,
        include_tests=parsed.include_tests,
    )
    if parsed.role == "client":
        return _client_routes(parsed, root, header)
    project = Project.open(root, MAX_EXTRACTION_AST_DEPTH)
    options = RouteOptions(parsed.framework, parsed.settings_module, parsed.include_tests, parsed.dispatch)
    try:
        extraction = extract_routes(project, options)
    except FrameworkChoiceError as error:
        raise UsageError(str(error)) from error
    try:
        return encode_document(build_document(header, extraction))
    except DocumentLimitError as error:
        raise InputError(str(error)) from error


def _client_routes(parsed: RoutesArguments, root: Path, header: DocumentHeader) -> str:
    """`routes --role client`를 실행한다.

    Args:
        parsed: 검증한 인자.
        root: 프로젝트 realpath.
        header: 문서 머리 필드.

    Returns:
        JSON 문서.

    Raises:
        UsageError: 래퍼 선언 오류.
        InputError: 래퍼 파일을 읽지 못하거나 출력이 상한을 넘을 때.
    """
    wrappers = _wrappers(parsed.wrappers)
    extraction = extract_client_calls(
        Project.open(root, MAX_EXTRACTION_AST_DEPTH), ClientOptions(parsed.include_tests, tuple(wrappers))
    )
    try:
        return encode_document(build_client_document(header, extraction))
    except DocumentLimitError as error:
        raise InputError(str(error)) from error


def _wrappers(argument: str | None) -> list[WrapperDecl]:
    """`--wrappers` 파일을 읽는다.

    Args:
        argument: 파일 경로(없으면 None).

    Returns:
        파이썬 래퍼 선언.

    Raises:
        UsageError: 선언 오류(4 MiB 초과 포함).
        InputError: 읽지 못하거나 일반 파일·파이프가 아닐 때.
    """
    if argument is None:
        return []
    try:
        return load_wrappers(Path(argument))
    except WrapperDeclarationError as error:
        raise UsageError(f"invalid --wrappers declaration: {error}") from error
    except OSError as error:
        raise InputError(
            "--wrappers does not name a readable regular file or pipe; pass an http-wrappers v1 JSON file."
        ) from error


def _schema(rest: list[str]) -> str:
    """schema 명령을 실행한다.

    Args:
        rest: schema 뒤 인자.

    Returns:
        JSON 문서.

    Raises:
        UsageError: 사용법 오류.
        InputError: 입력 오류.
    """
    values, flags = parse_flags(rest, _SCHEMA_VALUE_FLAGS, SCHEMA_USAGE)
    if "--help" in flags:
        return SCHEMA_USAGE
    if values.get("--format", "json") != "json":
        raise UsageError("--format supports only json.")
    project_argument = values.get("--project")
    if project_argument is None:
        raise UsageError("--project <root> is required.\n" + SCHEMA_USAGE)
    settings_module = _settings_module(values.get("--settings"))
    generated_at = parse_timestamp(values.get("--generated-at"))
    root = resolve_project(project_argument)
    extraction = extract_persistence(
        Project.open(root, MAX_EXTRACTION_AST_DEPTH), SchemaOptions("--include-tests" in flags, settings_module)
    )
    header = DocumentHeader(
        tool_version=__version__,
        generated_at=generated_at or datetime.now(timezone.utc),
        project=root.as_posix(),
        service=None,
        include_tests="--include-tests" in flags,
    )
    try:
        return encode_document(build_persistence_document(header, extraction))
    except DocumentLimitError as error:
        raise InputError(str(error)) from error


def parse_flags(
    arguments: list[str], value_flags: tuple[str, ...] = _VALUE_FLAGS, usage: str = ROUTES_USAGE
) -> tuple[dict[str, str], set[str]]:
    """옵션을 값 옵션과 불리언 옵션으로 나눈다. 모르는·반복·빈 옵션과 위치 인자는 사용법 오류다.

    `--flag=value` 형식도 받는다.

    Args:
        arguments: 인자 목록.
        value_flags: 허용하는 값 옵션.
        usage: 오류 문구에 붙일 사용법.

    Returns:
        (값 옵션, 불리언 옵션).

    Raises:
        UsageError: 잘못된 인자.
    """
    values: dict[str, str] = {}
    flags: set[str] = set()
    index = 0
    while index < len(arguments):
        name, _, inline = arguments[index].partition("=")
        if name in _BOOLEAN_FLAGS and not inline and name not in flags:
            flags.add(name)
            index += 1
            continue
        if name not in value_flags or name in values:
            raise UsageError("unknown, repeated, or positional argument.\n" + usage)
        value = inline if inline else (arguments[index + 1] if index + 1 < len(arguments) else "")
        if not value or (not inline and value.startswith("--")):
            raise UsageError(f"{name} needs a value.\n" + usage)
        values[name] = value
        index += 1 if inline else 2
    return values, flags


def validate_routes(values: dict[str, str], flags: set[str]) -> RoutesArguments:
    """routes 인자를 검증한다.

    Args:
        values: 값 옵션.
        flags: 불리언 옵션.

    Returns:
        검증한 인자.

    Raises:
        UsageError: 잘못된 인자.
    """
    role = values.get("--role")
    if role is None:
        raise UsageError("--role server or --role client is required.\n" + ROUTES_USAGE)
    if role not in ("server", "client"):
        raise UsageError("--role must be server or client.")
    if values.get("--format", "json") != "json":
        raise UsageError("--format supports only json.")
    project = values.get("--project")
    if project is None:
        raise UsageError("--project <root> is required.\n" + ROUTES_USAGE)
    _check_role_options(role, values)
    framework = values.get("--framework", "auto")
    if framework not in ("auto", "django", "flask"):
        raise UsageError("--framework must be auto, django, or flask.")
    dispatch = values.get("--dispatch")
    if dispatch not in (None, "specificity"):
        raise UsageError("--dispatch supports only specificity (registration-order is the Django default).")
    return RoutesArguments(
        role=role,
        project=project,
        service=_service(values.get("--service")),
        include_tests="--include-tests" in flags,
        framework=framework,
        settings_module=_settings_module(values.get("--settings")),
        dispatch=dispatch,
        generated_at=parse_timestamp(values.get("--generated-at")),
        wrappers=values.get("--wrappers"),
    )


def _check_role_options(role: str, values: dict[str, str]) -> None:
    """역할에 맞지 않는 옵션을 거부한다.

    Args:
        role: `server` 또는 `client`.
        values: 값 옵션.

    Raises:
        UsageError: 다른 역할 전용 옵션을 줬을 때.
    """
    server_only = ("--framework", "--settings", "--dispatch")
    if role == "client" and any(name in values for name in server_only):
        raise UsageError("--framework, --settings, and --dispatch apply only to --role server.")
    if role == "server" and "--wrappers" in values:
        raise UsageError("--wrappers applies only to --role client.")


def _service(value: str | None) -> str | None:
    """`--service` 값을 검증한다.

    Args:
        value: 값.

    Returns:
        값 또는 None.

    Raises:
        UsageError: 길이·문자 위반.
    """
    if value is not None and (len(value) > MAX_SERVICE_LENGTH or FORBIDDEN_CHARACTERS.search(value)):
        raise UsageError(f"--service must be 1-{MAX_SERVICE_LENGTH} characters without control characters.")
    return value


def _settings_module(value: str | None) -> str | None:
    """`--settings` 값을 검증한다.

    Args:
        value: 값.

    Returns:
        값 또는 None.

    Raises:
        UsageError: 점으로 이은 파이썬 식별자가 아닐 때.
    """
    if value is not None and not all(part.isidentifier() for part in value.split(".")):
        raise UsageError("--settings must be a dotted Python module name such as mysite.settings.")
    return value
