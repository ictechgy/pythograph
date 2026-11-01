"""그래프 정점 범위와 추가 테스트 흐름의 누락을 분모·원인·파서 환경별로 구분한다."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.graph.build import build_graph
from pythograph.source import project as project_module
from pythograph.source.project import Project, source_budget_reason
from tests.conftest import GENERATED_AT, run_cli

MakeProject = Callable[[dict[str, str]], Path]
SYNTAX_REASON = "syntax error or syntax newer than the running Python"


def counts(files: int, parsed: int, failures: dict[str, int], not_attempted: int = 0) -> dict[str, object]:
    """분모와 배타적인 성공·실패·미시도 수를 명시한다."""
    return {
        "files": files,
        "attempted": files - not_attempted,
        "parsed": parsed,
        "unparsed": sum(failures.values()),
        "notAttempted": not_attempted,
        "failureReasons": failures,
    }


@pytest.mark.parametrize("include_tests", [False, True])
def test_graph_and_supplemental_test_omissions_have_disjoint_denominators(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch, include_tests: bool
) -> None:
    """예산 실패와 파서 실패를 합산하되 정점 범위와 추가 테스트 범위는 중복 집계하지 않는다."""
    sources = {
        "app.py": "from tests import helpers\n\ndef target():\n    return 1\n",
        "broken.py": "def broken(:\n",
        "tests/__init__.py": "",
        "tests/helpers.py": "VALUE = 1\n",
        "tests/test_broken.py": "def broken(:\n",
        "tests/test_oversized.py": "DATA = [" + "0," * 100 + "]\n",
    }
    monkeypatch.setattr(project_module, "MAX_TOTAL_SOURCE_BYTES", 96)
    graph = build_graph(Project.open(make_project(sources)), include_tests)
    analysis = graph.statistics["sourceAnalysis"]
    assert isinstance(analysis, dict)
    budget = source_budget_reason()
    all_failures = {SYNTAX_REASON: 2, budget: 1}
    assert analysis["scannedPythonFiles"] == 6
    assert analysis["includeTests"] is include_tests
    assert analysis["graphFiles"] == (counts(6, 3, all_failures) if include_tests else counts(2, 1, {SYNTAX_REASON: 1}))
    assert analysis["supplementalTestFiles"] == (
        counts(0, 0, {}) if include_tests else counts(4, 2, {SYNTAX_REASON: 1, budget: 1})
    )
    assert analysis["outsideScanFiles"] == counts(0, 0, {})
    assert analysis["allParseAttempts"] == counts(6, 3, all_failures)
    assert analysis["sourceBudget"] == {
        "limitBytes": 96,
        "parsedBytes": len(sources["app.py"]) + len(sources["tests/helpers.py"]),
        "skippedFiles": 1,
    }
    assert graph.statistics["unparsedFiles"] == (3 if include_tests else 1)
    lines = graph.limitations["direct"]
    denominator = "3 of 6" if include_tests else "1 of 2"
    assert any(line.startswith(f"unparsed-files: {denominator} graph-eligible") for line in lines)
    assert any(line.startswith("source-budget: 1 ") for line in lines)
    if not include_tests:
        assert any(line.startswith("supplemental-test-files: 2 of 4 ") for line in lines)
    assert graph.statistics["boundDispatch"]["scanIncomplete"] is True  # type: ignore[index]


def test_tests_excluded_from_graph_are_not_counted_as_parse_failures(make_project: MakeProject) -> None:
    """요청하지 않은 테스트는 누락 실패가 아니라 미시도로 명시한다."""
    graph = build_graph(Project.open(make_project({"app.py": "", "tests/test_unused.py": "def broken(:\n"})), False)
    analysis = graph.statistics["sourceAnalysis"]
    assert isinstance(analysis, dict)
    assert analysis["graphFiles"] == counts(1, 1, {})
    assert analysis["supplementalTestFiles"] == counts(1, 0, {}, not_attempted=1)
    assert "parser" not in analysis
    assert not any(line.startswith("supplemental-test-files:") for line in graph.limitations["direct"])


#: 실행 런타임에 걸리는 `!=` 조건이다. 하한은 넘지만 실제 최소 지원 버전을 증명하지 못하는 경우를 런타임과 무관하게
#: 만든다.
_RUNTIME_EXCLUDED = f">=3.0,!={sys.version_info[0]}.{sys.version_info[1]}.*"


@pytest.mark.parametrize(
    ("metadata", "status", "requirement"),
    [
        (None, "not-declared", None),
        ('[project]\nrequires-python = ">=999.0.2"\n', "runtime-below-minimum", ">=999.0.2"),
        ('[project]\nrequires-python = ">=3.0"\n', "minimum-not-newer", ">=3.0"),
        (f'[project]\nrequires-python = "{_RUNTIME_EXCLUDED}"\n', "not-evaluated", _RUNTIME_EXCLUDED),
        ('[project]\nrequires-python = ">=999.0.2, <1000"\n', "runtime-below-minimum", ">=999.0.2, <1000"),
        ('[project]\nrequires-python = "~=3.0"\n', "minimum-not-newer", "~=3.0"),
        ('[project]\nrequires-python = ">=3.0,<3.1.dev0"\n', "not-evaluated", ">=3.0,<3.1.dev0"),
        ('[tool.example]\nrequires-python = ">=999.0"\n', "not-declared", None),
        ("[project]\nrequires-python = 42\n", "metadata-invalid", None),
        ('project = "not a table"\n', "metadata-invalid", None),
        ('[project]\nrequires-python = "bad\\nvalue"\n', "metadata-invalid", None),
        ('[project]\nrequires-python = "' + ">" * 300 + '"\n', "metadata-invalid", None),
        ('[project\nrequires-python = ">=999.0"\n', "metadata-invalid", None),
    ],
)
def test_parser_diagnostics_report_runtime_and_requirement_without_claiming_invalid_source(
    make_project: MakeProject, metadata: str | None, status: str, requirement: str | None
) -> None:
    """실제 파서 거부가 있을 때만 환경 차이를 알리고, 복잡한 버전 조건은 호환 판정으로 추측하지 않는다."""
    sources = {"good.py": "VALUE = 1\n", "broken.py": "def broken(:\n"}
    if metadata is not None:
        sources["pyproject.toml"] = metadata
    root = make_project(sources)
    code, text, err = run_cli(["graph", "--project", str(root), "--generated-at", GENERATED_AT])
    assert code == 0, err
    document = json.loads(text)
    parser = document["statistics"]["sourceAnalysis"]["parser"]
    assert parser["implementation"] == sys.implementation.name
    assert parser["version"] == ".".join(str(part) for part in sys.version_info[:3])
    assert parser["syntaxRejectedFiles"] == 1
    expected = status if sys.version_info >= (3, 11) or metadata is None else "metadata-parser-unavailable"
    assert parser["requiresPythonStatus"] == expected
    if requirement is not None and sys.version_info >= (3, 11):
        assert parser["requiresPython"] == requirement
    else:
        assert "requiresPython" not in parser
    warning = next(line for line in document["limitations"] if line.startswith("python-parser:"))
    assert "syntax errors or syntax newer than this runtime" in warning
    assert str(root) not in warning and "broken.py" not in warning
    if expected == "runtime-below-minimum":
        assert ">=999.0.2" in warning and "below the declared minimum" in warning


@pytest.mark.parametrize(
    ("requirement", "runtime", "status"),
    [
        (">=3.11", (3, 10, 14), "runtime-below-minimum"),
        (">=3.11", (3, 11, 0), "minimum-not-newer"),
        (">= 3.11.2", (3, 11, 1), "runtime-below-minimum"),
        ("~=3.11", (3, 10, 9), "runtime-below-minimum"),
        ("~=3.11", (3, 13, 1), "minimum-not-newer"),
        ("~=3.11.2", (3, 11, 1), "runtime-below-minimum"),
        (">=3.11,<4", (3, 10, 0), "runtime-below-minimum"),
        (">=3.11,<4", (3, 12, 0), "minimum-not-newer"),
        (" >=3.9 , <3.14 ", (3, 14, 0), "minimum-not-newer"),
        (">3.10", (3, 10, 0), "runtime-below-minimum"),
        (">3.10", (3, 10, 1), "minimum-not-newer"),
        ("==3.11.*", (3, 10, 5), "runtime-below-minimum"),
        ("==3.11.*", (3, 12, 0), "minimum-not-newer"),
        ("==3.11", (3, 10, 5), "runtime-below-minimum"),
        (">=3.8,>=3.10", (3, 9, 0), "runtime-below-minimum"),
        (">=3.10,!=3.13.*", (3, 12, 0), "minimum-not-newer"),
        (">=3.10,!=3.13.*", (3, 9, 0), "runtime-below-minimum"),
        # 실행 버전이 `!=`에 걸리면 실제 최소 지원 버전(`>=3.8,!=3.8.*`이면 3.9)이 실행 버전보다 새로울 수 있어
        # 판정하지 않는다.
        (">=3.10,!=3.13.*", (3, 13, 2), "not-evaluated"),
        (">=3.8,!=3.8.*", (3, 8, 10), "not-evaluated"),
        (">=3.8,!=3.8.10", (3, 8, 10), "not-evaluated"),
        (">=3.8,!=3.8.10", (3, 8, 11), "minimum-not-newer"),
        # 하한이 없거나 해석하지 않는 형식이면 판정하지 않는다.
        ("", (3, 13, 0), "not-evaluated"),
        ("<4", (3, 13, 0), "not-evaluated"),
        ("!=3.9.*", (3, 13, 0), "not-evaluated"),
        (">=3.11,", (3, 10, 0), "not-evaluated"),
        (">=3.x", (3, 10, 0), "not-evaluated"),
        ("~=3", (2, 7, 0), "not-evaluated"),
        (">=3.11.*", (3, 10, 0), "not-evaluated"),
        (">=3.11.0rc1", (3, 10, 0), "not-evaluated"),
        (">=1!3.11", (3, 10, 0), "not-evaluated"),
        ("===3.11", (3, 10, 0), "not-evaluated"),
        (">=3.11.0.0", (3, 10, 0), "not-evaluated"),
        (">=3.11 || <3", (3, 10, 0), "not-evaluated"),
        (">=3.11,<3.12+local", (3, 10, 0), "not-evaluated"),
    ],
)
def test_requirement_status_compares_only_provable_lower_bounds(
    requirement: str, runtime: tuple[int, int, int], status: str
) -> None:
    """쉼표로 이은 PEP 440 조건의 하한만 비교하고, 증명하지 못하는 형식은 판정하지 않는다."""
    from pythograph.graph.source_diagnostics import requirement_status

    assert requirement_status(requirement, runtime) == status


def test_project_requirement_alone_does_not_claim_parser_failure(make_project: MakeProject) -> None:
    """미지원 런타임을 추측해 성공한 파싱을 실패로 바꾸거나 지원 완료로 판정하지 않는다."""
    graph = build_graph(
        Project.open(make_project({"app.py": "", "pyproject.toml": '[project]\nrequires-python = ">=999.0"\n'})), False
    )
    analysis = graph.statistics["sourceAnalysis"]
    assert isinstance(analysis, dict) and "parser" not in analysis
    assert not any(line.startswith("python-parser:") for line in graph.limitations["direct"])


@pytest.mark.parametrize("metadata", ["\n" * (64 * 1024 + 1), "invalid-utf8"], ids=["oversized", "invalid-utf8"])
def test_parser_metadata_reading_stays_bounded(make_project: MakeProject, metadata: str) -> None:
    """메타데이터를 읽지 못해도 파싱 결과는 유지하고 요구 조건 판정을 생략한다."""
    root = make_project({"broken.py": "def broken(:\n", "pyproject.toml": metadata})
    if metadata == "invalid-utf8":
        (root / "pyproject.toml").write_bytes(b"\xff")
    else:
        (root / "pyproject.toml").write_text(metadata)
    graph = build_graph(Project.open(root), False)
    analysis = graph.statistics["sourceAnalysis"]
    assert isinstance(analysis, dict)
    assert analysis["parser"]["requiresPythonStatus"] == "metadata-unreadable"
    assert graph.statistics["unparsedFiles"] == 1


def test_python310_metadata_fallback_is_explicit(make_project: MakeProject, monkeypatch: pytest.MonkeyPatch) -> None:
    """표준 TOML 파서가 없는 3.10 환경은 요구 버전을 추측하지 않는다."""
    from pythograph.graph import source_diagnostics

    monkeypatch.setattr(source_diagnostics, "_TOML", None)
    root = make_project({"broken.py": "def broken(:\n", "pyproject.toml": '[project]\nrequires-python = ">=999.0"\n'})
    graph = build_graph(Project.open(root), False)
    analysis = graph.statistics["sourceAnalysis"]
    assert isinstance(analysis, dict)
    assert analysis["parser"]["requiresPythonStatus"] == "metadata-parser-unavailable"


def test_cached_import_outside_scan_has_a_separate_denominator(make_project: MakeProject) -> None:
    """순회 밖에서 이미 읽은 소스도 전체 시도에 보존하고 그래프·테스트 누락에 중복시키지 않는다."""
    project = Project.open(make_project({"app.py": "", ".hidden/extra.py": "def broken(:\n"}))
    project.module(".hidden/extra.py")
    graph = build_graph(project, False)
    analysis = graph.statistics["sourceAnalysis"]
    assert isinstance(analysis, dict)
    assert analysis["scannedPythonFiles"] == 1
    assert analysis["graphFiles"] == counts(1, 1, {})
    assert analysis["outsideScanFiles"] == counts(1, 0, {SYNTAX_REASON: 1})
    assert analysis["allParseAttempts"] == counts(2, 1, {SYNTAX_REASON: 1})
    assert any(line.startswith("outside-scan-files: 1 of 1 ") for line in graph.limitations["direct"])
