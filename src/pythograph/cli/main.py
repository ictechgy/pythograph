"""pythograph 명령행 진입점과 종료 코드 계약.

종료 코드: 0 성공(사실 0건도 성공이며 완전성의 증거가 아니다), 2 입력 오류(읽을 수 없는 프로젝트,
isthmus 상한을 넘는 출력, 예기치 못한 내부 오류), 64 사용법 오류. 1은 예약이다(파이썬의 잡히지 않은
예외가 1로 끝나므로, 모든 예외를 잡아 2로 바꾼다). 오류 문구에는 원인과 해결 방향을 담되 입력 원문과
절대 경로를 넣지 않는다.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from pythograph import __version__
from pythograph.exchange.document import DocumentHeader, DocumentLimitError, build_document, encode_document
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

#: 계약이 금지하는 식별자 문자다(제어 문자, U+2028/2029).
_FORBIDDEN = re.compile("[\u0000-\u001f\u007f-\u009f  ]")

#: `--generated-at` 형식이다.
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")

MAIN_USAGE = """Usage: pythograph <command> [options]

Static facts for Python services (Django, Django REST framework, Flask) in the isthmus
bridge-facts exchange format. The analyzed project is parsed with the standard-library
ast only; it is never imported or executed.

Commands:
  routes     Server route declarations (route-decl facts)
  help       Show help for a command

Options:
  --version  Print the version
  --help     Print this help

Exit codes: 0 success, 2 input error, 64 usage error (1 is reserved).
"""

ROUTES_USAGE = """Usage: pythograph routes --role server --project <root> [--service <name>] [--include-tests]
                         [--framework auto|django|flask] [--settings <module>]
                         [--dispatch specificity] [--generated-at <timestamp>] [--format json]

Scan a Django (+ Django REST framework) or Flask project and write an isthmus bridge-facts v1
document (platform "python", target "http", route-decl facts) to stdout.

Options:
  --role server              Declaration side to extract (server is the only role implemented)
  --project <root>           Project root; location paths are relative to it
  --service <name>           Service identity recorded on the document and every fact
  --include-tests            Also emit routes declared in test sources, with testSource: true
  --framework <name>         auto (default), django, or flask
  --settings <module>        Django settings module (default: the DJANGO_SETTINGS_MODULE default
                             in manage.py, wsgi.py, or asgi.py)
  --dispatch specificity     Declare specificity dispatch for a Django project and omit order
                             (an approximation for consumers without registration-order support)
  --generated-at <timestamp> Fixed generatedAt (YYYY-MM-DDTHH:MM:SS.sssZ) for byte-identical output
  --format json              Output format (json is the only format)

Exit codes: 0 success, 2 unreadable project or oversized output, 64 usage error.
"""

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
)

#: routes 명령의 불리언 옵션이다.
_BOOLEAN_FLAGS = ("--include-tests", "--help")


class UsageError(Exception):
    """사용법 오류(64). 메시지는 해결 방향을 담는다."""


class InputError(Exception):
    """입력 오류(2). 메시지는 원인과 해결 방향을 담는다."""


@dataclass(frozen=True)
class RoutesArguments:
    """검증한 routes 인자."""

    project: str
    service: str | None
    include_tests: bool
    framework: str
    settings_module: str | None
    dispatch: str | None
    generated_at: datetime | None


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """명령을 실행하고 종료 코드를 돌려준다.

    Args:
        argv: 인자(없으면 `sys.argv[1:]`).
        stdout: 표준 출력(테스트 주입용).
        stderr: 표준 오류(테스트 주입용).

    Returns:
        종료 코드.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        text = run(arguments)
    except UsageError as error:
        err.write(f"pythograph: {error}\n")
        return EXIT_USAGE
    except InputError as error:
        err.write(f"pythograph: {error}\n")
        return EXIT_INPUT
    except Exception as error:  # 1은 예약이므로 어떤 내부 오류도 2로 바꾼다. 원문은 싣지 않는다.
        err.write(f"pythograph: internal error ({type(error).__name__}); please report it with the command line\n")
        return EXIT_INPUT
    out.write(text)
    return EXIT_OK


def run(arguments: list[str]) -> str:
    """인자를 해석해 표준 출력에 쓸 문자열을 만든다.

    Args:
        arguments: 인자 목록.

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
    project = Project.open(root)
    options = RouteOptions(parsed.framework, parsed.settings_module, parsed.include_tests, parsed.dispatch)
    try:
        extraction = extract_routes(project, options)
    except FrameworkChoiceError as error:
        raise UsageError(str(error)) from error
    header = DocumentHeader(
        tool_version=__version__,
        generated_at=parsed.generated_at or datetime.now(timezone.utc),
        project=root.as_posix(),
        service=parsed.service,
        include_tests=parsed.include_tests,
    )
    try:
        return encode_document(build_document(header, extraction))
    except DocumentLimitError as error:
        raise InputError(str(error)) from error


def parse_flags(arguments: list[str]) -> tuple[dict[str, str], set[str]]:
    """옵션을 값 옵션과 불리언 옵션으로 나눈다. 모르는·반복·빈 옵션과 위치 인자는 사용법 오류다.

    `--flag=value` 형식도 받는다.

    Args:
        arguments: 인자 목록.

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
        if name not in _VALUE_FLAGS or name in values:
            raise UsageError("unknown, repeated, or positional argument.\n" + ROUTES_USAGE)
        value = inline if inline else (arguments[index + 1] if index + 1 < len(arguments) else "")
        if not value or (not inline and value.startswith("--")):
            raise UsageError(f"{name} needs a value.\n" + ROUTES_USAGE)
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
        raise UsageError("--role server is required.\n" + ROUTES_USAGE)
    if role != "server":
        raise UsageError("--role supports only server (client route-call extraction is not implemented yet).")
    if values.get("--format", "json") != "json":
        raise UsageError("--format supports only json.")
    project = values.get("--project")
    if project is None:
        raise UsageError("--project <root> is required.\n" + ROUTES_USAGE)
    framework = values.get("--framework", "auto")
    if framework not in ("auto", "django", "flask"):
        raise UsageError("--framework must be auto, django, or flask.")
    dispatch = values.get("--dispatch")
    if dispatch not in (None, "specificity"):
        raise UsageError("--dispatch supports only specificity (registration-order is the Django default).")
    return RoutesArguments(
        project=project,
        service=_service(values.get("--service")),
        include_tests="--include-tests" in flags,
        framework=framework,
        settings_module=_settings_module(values.get("--settings")),
        dispatch=dispatch,
        generated_at=_timestamp(values.get("--generated-at")),
    )


def _service(value: str | None) -> str | None:
    """`--service` 값을 검증한다.

    Args:
        value: 값.

    Returns:
        값 또는 None.

    Raises:
        UsageError: 길이·문자 위반.
    """
    if value is not None and (len(value) > MAX_SERVICE_LENGTH or _FORBIDDEN.search(value)):
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


def _timestamp(value: str | None) -> datetime | None:
    """`--generated-at` 값을 해석한다.

    Args:
        value: 값.

    Returns:
        UTC 시각 또는 None.

    Raises:
        UsageError: 형식 위반.
    """
    if value is None:
        return None
    if _TIMESTAMP.fullmatch(value) is None:
        raise UsageError("--generated-at takes a UTC timestamp such as 2026-01-01T00:00:00.000Z.")
    try:
        return datetime.strptime(value[:-1] + ("" if "." in value else ".0"), "%Y-%m-%dT%H:%M:%S.%f").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise UsageError("--generated-at takes a valid UTC timestamp such as 2026-01-01T00:00:00.000Z.") from error


def resolve_project(argument: str) -> Path:
    """프로젝트 경로를 realpath로 정규화하고 디렉터리인지 확인한다.

    Args:
        argument: `--project` 값.

    Returns:
        realpath.

    Raises:
        InputError: 디렉터리가 아니거나 계약이 금지하는 문자를 담을 때.
    """
    try:
        root = Path(os.path.realpath(argument))
        is_directory = root.is_dir()
    except (OSError, ValueError):
        is_directory = False
    if not is_directory:
        raise InputError("--project does not name a readable directory; pass the project root.")
    if _FORBIDDEN.search(root.as_posix()):
        raise InputError("the project path contains characters the exchange format forbids; rename or move it.")
    return root
