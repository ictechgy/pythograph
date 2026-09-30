"""pythograph 명령행 진입점과 종료 코드 계약.

종료 코드: 0 성공(사실 0건도 성공이며 완전성의 증거가 아니다), 2 입력 오류(읽을 수 없는 프로젝트,
isthmus 상한을 넘는 출력, 예기치 못한 내부 오류), 64 사용법 오류. 1은 예약이다(파이썬의 잡히지 않은
예외가 1로 끝나므로, 모든 예외를 잡아 2로 바꾼다). 오류 문구에는 원인과 해결 방향을 담되 입력 원문과
절대 경로를 넣지 않는다.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from pythograph import __version__
from pythograph.cli.common import FORBIDDEN, InputError, UsageError, parse_timestamp, resolve_project
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
  --service <name>           Service identity recorded on the document and every fact
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

    Args:
        argv: 인자(없으면 `sys.argv[1:]`).
        stdout: 표준 출력(테스트 주입용).
        stderr: 표준 오류(테스트 주입용).
        stdin: 표준 입력(`--roots-from -`, 테스트 주입용).

    Returns:
        종료 코드.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        text = run(arguments, stdin)
    except RootNotFoundExit as partial:
        _report(err, f"{partial.missing} root id(s) are not graph nodes; the document lists them without symbol")
        return _write(out, err, partial.text, EXIT_USAGE)
    except UsageError as error:
        _report(err, str(error))
        return EXIT_USAGE
    except InputError as error:
        _report(err, str(error))
        return EXIT_INPUT
    except Exception as error:  # 1은 예약이므로 어떤 내부 오류도 2로 바꾼다. 원문은 싣지 않는다.
        _report(err, f"internal error ({type(error).__name__}); please report it with the command line")
        return EXIT_INPUT
    return _write(out, err, text, EXIT_OK)


def _write(out: TextIO, err: TextIO, text: str, code: int) -> int:
    """표준 출력에 결과를 쓴다. 닫힌 파이프·인코딩 실패도 1이 아니라 2다.

    Args:
        out: 표준 출력.
        err: 표준 오류.
        text: 출력 문자열.
        code: 쓰기에 성공했을 때의 종료 코드.

    Returns:
        종료 코드.
    """
    try:
        out.write(text)
        out.flush()
    except (OSError, UnicodeError) as error:
        _report(err, f"could not write the output ({type(error).__name__}); check the output destination")
        return EXIT_INPUT
    return code


def _report(err: TextIO, message: str) -> None:
    """표준 오류에 한 줄을 쓴다. 표준 오류 자체가 실패해도 종료 코드 계약을 지키도록 오류를 삼키지 않고 무시한다.

    Args:
        err: 표준 오류.
        message: 문구.
    """
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
    topics = {"graph": GRAPH_USAGE, "reach": REACH_USAGE, "impact": IMPACT_USAGE}
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
    project = Project.open(root)
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
    extraction = extract_client_calls(Project.open(root), ClientOptions(parsed.include_tests, tuple(wrappers)))
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
        UsageError: 선언 오류.
        InputError: 읽지 못할 때.
    """
    if argument is None:
        return []
    try:
        return load_wrappers(Path(argument))
    except WrapperDeclarationError as error:
        raise UsageError(f"invalid --wrappers declaration: {error}") from error
    except OSError as error:
        raise InputError("--wrappers does not name a readable file; pass an http-wrappers v1 JSON file.") from error


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
    extraction = extract_persistence(Project.open(root), SchemaOptions("--include-tests" in flags, settings_module))
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
    if value is not None and (len(value) > MAX_SERVICE_LENGTH or FORBIDDEN.search(value)):
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
