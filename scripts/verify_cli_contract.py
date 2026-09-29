"""설치한 `pythograph` 콘솔 스크립트가 종료 코드 계약(0/2/64, 1은 예약)을 지키는지 실제 프로세스로 확인한다.

사용법: python scripts/verify_cli_contract.py [--executable <pythograph 경로>]
실행 파일을 주지 않으면 현재 가상 환경의 `pythograph`를 PATH에서 찾는다. CI는 빌드한 wheel을 새 가상 환경에
설치한 실행 파일로도 돌려 배포물(`uv tool install`·`pipx install`과 같은 콘솔 스크립트)을 검증한다.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: 저장소 루트다.
REPOSITORY = Path(__file__).resolve().parent.parent

#: 고정 generatedAt이다.
GENERATED_AT = "2026-01-01T00:00:00.000Z"

#: (fixture, 기대 dispatch) 목록이다.
FIXTURES = (
    (REPOSITORY / "fixtures" / "django" / "drf-shop", "registration-order"),
    (REPOSITORY / "fixtures" / "flask" / "blog-app", "specificity"),
)

#: graph·reach·impact 명령 fixture와 root다.
GRAPH_FIXTURE = REPOSITORY / "fixtures" / "e2e" / "shop-api"
GRAPH_ROOT = "store/views.py#OrderViewSet.retrieve"

#: schema 명령 fixture다.
SCHEMA_FIXTURES = (
    REPOSITORY / "fixtures" / "persistence" / "django-shop",
    REPOSITORY / "fixtures" / "persistence" / "flask-blog",
)


def run(executable: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """CLI를 실행한다.

    Args:
        executable: 실행 파일.
        arguments: 인자.

    Returns:
        실행 결과.
    """
    return subprocess.run([executable, *arguments], capture_output=True, text=True, check=False, timeout=120)


def verify(condition: bool, label: str) -> None:
    """조건이 거짓이면 이유를 쓰고 종료한다.

    Args:
        condition: 검사 결과.
        label: 검사 이름.
    """
    if not condition:
        sys.stderr.write(f"CLI contract violated: {label}\n")
        raise SystemExit(1)


def verify_basics(executable: str) -> None:
    """도움말·버전·사용법 오류를 확인한다.

    Args:
        executable: 실행 파일.
    """
    version = re.search(r'^version = "([^"]+)"', (REPOSITORY / "pyproject.toml").read_text(), re.MULTILINE)
    verify(version is not None, "pyproject version")
    assert version is not None
    result = run(executable, ["--version"])
    verify(result.returncode == 0 and result.stdout == f"{version.group(1)}\n", "version")
    verify(run(executable, ["--help"]).stdout.startswith("Usage: pythograph"), "help")
    verify(run(executable, ["help", "routes"]).stdout.startswith("Usage: pythograph routes"), "routes help")
    verify(run(executable, ["help", "schema"]).stdout.startswith("Usage: pythograph schema"), "schema help")
    for command in ("graph", "reach", "impact"):
        verify(run(executable, ["help", command]).stdout.startswith(f"Usage: pythograph {command}"), f"{command} help")
    for arguments in ([], ["no-such-command"], ["routes", "--project", "."], ["routes", "--role", "client"],
                      ["routes", "--role", "server"], ["routes", "--role", "server", "--project", ".", "--format",
                                                        "yaml"],
                      ["schema"], ["schema", "--project", ".", "--format", "yaml"], ["schema", "--project", ".", "x"],
                      ["graph"], ["graph", "--project", ".", "x"], ["reach", "--project", "."],
                      ["impact", "--project", ".", "a\u0001b"], ["reach", "--project", ".", "--dispatch", "x", "a#b"]):
        verify(run(executable, arguments).returncode == 64, f"usage error {arguments}")


def verify_routes(executable: str) -> None:
    """fixture 문서가 결정적이고 계약 모양인지 확인한다.

    Args:
        executable: 실행 파일.
    """
    for fixture, dispatch in FIXTURES:
        arguments = ["routes", "--role", "server", "--project", str(fixture), "--generated-at", GENERATED_AT,
                     "--format", "json"]
        first = run(executable, arguments)
        verify(first.returncode == 0, f"{fixture.name} exit code")
        document = json.loads(first.stdout)
        verify(document["format"] == "bridge-facts" and document["version"] == 1, f"{fixture.name} envelope")
        verify(document["platform"] == "python" and document["target"] == "http", f"{fixture.name} platform")
        verify(document["dispatch"] == dispatch, f"{fixture.name} dispatch")
        verify(document["facts"] and all(fact["kind"] == "route-decl" for fact in document["facts"]),
               f"{fixture.name} facts")
        verify(first.stdout == run(executable, arguments).stdout, f"{fixture.name} determinism")


def verify_schema(executable: str) -> None:
    """schema 문서가 결정적이고 persistence 계약 모양인지 확인한다.

    Args:
        executable: 실행 파일.
    """
    for fixture in SCHEMA_FIXTURES:
        arguments = ["schema", "--project", str(fixture), "--generated-at", GENERATED_AT, "--format", "json"]
        first = run(executable, arguments)
        verify(first.returncode == 0, f"{fixture.name} schema exit code")
        document = json.loads(first.stdout)
        verify(document["platform"] == "python" and document["target"] == "persistence", f"{fixture.name} target")
        verify(document["facts"] and all(fact["kind"] == "relation-use" for fact in document["facts"]),
               f"{fixture.name} relation-use facts")
        verify(first.stdout == run(executable, arguments).stdout, f"{fixture.name} schema determinism")


def verify_graph(executable: str) -> None:
    """graph·reach·impact 문서가 결정적이고, 정점이 아닌 root는 문서를 쓰고 64로 끝나는지 확인한다.

    Args:
        executable: 실행 파일.
    """
    common = ["--project", str(GRAPH_FIXTURE), "--generated-at", GENERATED_AT, "--revision", "contract"]
    graph = run(executable, ["graph", *common])
    verify(graph.returncode == 0 and json.loads(graph.stdout)["format"] == "pythograph-graph", "graph snapshot")
    for command, direction in (("reach", "dependencies"), ("impact", "dependents")):
        first = run(executable, [command, *common, GRAPH_ROOT])
        verify(first.returncode == 0, f"{command} exit code")
        document = json.loads(first.stdout)
        verify(document["format"] == "language-traversal" and document["direction"] == direction, f"{command} shape")
        verify(first.stdout == run(executable, [command, *common, GRAPH_ROOT]).stdout, f"{command} determinism")
    missing = run(executable, ["reach", *common, "nope#x", GRAPH_ROOT])
    verify(missing.returncode == 64 and json.loads(missing.stdout)["roots"][0] == {"id": "nope#x"}, "root-not-found")


def verify_input_errors(executable: str) -> None:
    """읽을 수 없는 프로젝트는 2로 끝나는지 확인한다.

    Args:
        executable: 실행 파일.
    """
    directory = Path(tempfile.mkdtemp(prefix="pythograph-cli-contract-"))
    try:
        missing = run(executable, ["routes", "--role", "server", "--project", str(directory / "missing")])
        verify(missing.returncode == 2 and str(directory) not in missing.stderr, "missing project")
        (directory / "file.txt").write_text("x")
        verify(run(executable, ["routes", "--role", "server", "--project", str(directory / "file.txt")])
               .returncode == 2, "project is a file")
        missing_schema = run(executable, ["schema", "--project", str(directory / "missing")])
        verify(missing_schema.returncode == 2 and str(directory) not in missing_schema.stderr, "schema missing project")
        missing_graph = run(executable, ["graph", "--project", str(directory / "missing")])
        verify(missing_graph.returncode == 2 and str(directory) not in missing_graph.stderr, "graph missing project")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def main(argv: list[str]) -> None:
    """계약을 확인한다.

    Args:
        argv: 명령줄 인자.
    """
    executable = argv[argv.index("--executable") + 1] if "--executable" in argv else shutil.which("pythograph")
    verify(executable is not None, "pythograph executable on PATH")
    assert executable is not None
    verify_basics(executable)
    verify_routes(executable)
    verify_schema(executable)
    verify_graph(executable)
    verify_input_errors(executable)
    sys.stdout.write("CLI contract verified: 0/2/64 (1 reserved)\n")


if __name__ == "__main__":
    main(sys.argv[1:])
