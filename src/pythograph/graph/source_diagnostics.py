"""파싱 결과를 바꾸지 않고 정점 범위·추가 테스트 흐름·실행 파서의 한계를 집계한다."""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Collection, Mapping
from importlib import import_module
from typing import TypedDict, cast

from pythograph.source import project as project_module
from pythograph.source.project import Project, ReadFailure, SourceModule, is_test_path, source_budget_reason
from pythograph.source.text import is_contract_safe

#: 3.10도 실행 계약에 들어가므로, 그 런타임에서는 TOML 요구 판정을 명시적으로 생략한다.
_TOML = import_module("tomllib") if sys.version_info >= (3, 11) else None

#: 파서의 거부는 소스 자체의 문법 오류와 새 Python 문법을 구분하지 못한다.
SYNTAX_REASON = "syntax error or syntax newer than the running Python"

#: 메타데이터는 소스 예산을 쓰지 않지만 별도의 작은 읽기 상한을 둔다.
MAX_METADATA_BYTES = 64 * 1024

#: 쉼표로 이은 PEP 440 비교 조건 하나다. 숫자 세 자리까지의 최종 릴리스만 읽고(사전·사후·개발 릴리스, epoch, 로컬
#: 버전, `===`는 읽지 않는다) 끝 `.*`는 `==`·`!=`에서만 유효하다. 전체 호환 판정 대신 하한만 비교하려고 쓴다.
_CLAUSE = re.compile(r"(~=|==|!=|<=|>=|<|>)\s*([0-9]{1,4}(?:\.[0-9]{1,4}){0,2})(\.\*)?")

#: 하한을 주는 연산자다(`>`만 배타적이다). `~=V`는 `>=V` 하한을, `==V`·`==V.*`는 V를 하한으로 갖는다.
_LOWER_BOUNDS = frozenset({">=", "~=", "==", ">"})


class SourceCounts(TypedDict):
    """한 범위의 분모와 배타적인 파싱 결과다. 실패 이유의 합은 unparsed와 같다."""

    files: int
    attempted: int
    parsed: int
    unparsed: int
    notAttempted: int
    failureReasons: dict[str, int]


def source_statistics(project: Project, include_tests: bool) -> dict[str, object]:
    """이미 캐시한 결과만 집계한다. 진단 때문에 파일을 새로 파싱하거나 예산을 소비하지 않는다."""
    outcomes = project.parsed_modules()
    scanned = set(project.python_files())
    graph = {path for path in scanned if include_tests or not is_test_path(path)}
    all_attempts = _counts(outcomes.keys(), outcomes)
    result: dict[str, object] = {
        "scannedPythonFiles": len(scanned),
        "includeTests": include_tests,
        "graphFiles": _counts(graph, outcomes),
        "supplementalTestFiles": _counts(scanned - graph, outcomes),
        "outsideScanFiles": _counts(outcomes.keys() - scanned, outcomes),
        "allParseAttempts": all_attempts,
        "sourceBudget": {
            "limitBytes": project_module.MAX_TOTAL_SOURCE_BYTES,
            "parsedBytes": project.parsed_source_bytes,
            "skippedFiles": project.budget_skipped_modules,
        },
    }
    rejected = all_attempts["failureReasons"].get(SYNTAX_REASON, 0)
    if rejected:
        result["parser"] = _parser(project, rejected)
    return result


def _counts(paths: Collection[str], outcomes: Mapping[str, SourceModule | ReadFailure]) -> SourceCounts:
    """지정 경로의 성공·실패·미시도를 센다. 범위끼리 겹치지 않아도 전체 합계와는 겹친다."""
    attempted = 0
    failures: Counter[str] = Counter()
    for path in paths:
        outcome = outcomes.get(path)
        if outcome is not None:
            attempted += 1
            if isinstance(outcome, ReadFailure):
                failures[outcome.reason] += 1
    unparsed = sum(failures.values())
    return {
        "files": len(paths),
        "attempted": attempted,
        "parsed": attempted - unparsed,
        "unparsed": unparsed,
        "notAttempted": len(paths) - attempted,
        "failureReasons": dict(sorted(failures.items())),
    }


def _parser(project: Project, rejected: int) -> dict[str, object]:
    """실제 문법 거부가 있을 때 실행 파서와 읽을 수 있는 프로젝트 요구를 함께 싣는다."""
    return {
        "implementation": sys.implementation.name,
        "version": ".".join(str(part) for part in sys.version_info[:3]),
        "syntaxRejectedFiles": rejected,
        **_requirement(project),
    }


def _requirement(project: Project) -> dict[str, object]:
    """작은 pyproject의 정식 project.requires-python만 읽는다. 분석 대상 코드를 실행하지 않는다."""
    if not project.is_file("pyproject.toml"):
        return {"requiresPythonStatus": "not-declared"}
    text = project.read_text("pyproject.toml", MAX_METADATA_BYTES)
    if text is None:
        return {"requiresPythonStatus": "metadata-unreadable"}
    if _TOML is None:
        return {"requiresPythonStatus": "metadata-parser-unavailable"}
    try:
        metadata = _TOML.loads(text)
    except (ValueError, RecursionError):
        return {"requiresPythonStatus": "metadata-invalid"}
    declaration = metadata.get("project", {})
    if not isinstance(declaration, dict):
        return {"requiresPythonStatus": "metadata-invalid"}
    requirement = declaration.get("requires-python")
    if requirement is None:
        return {"requiresPythonStatus": "not-declared"}
    if not isinstance(requirement, str) or len(requirement) > 256 or not is_contract_safe(requirement):
        return {"requiresPythonStatus": "metadata-invalid"}
    status = "not-evaluated"
    if sys.version_info.releaselevel == "final":
        major, minor, micro = sys.version_info[:3]
        status = requirement_status(requirement, (major, minor, micro))
    return {"requiresPython": requirement, "requiresPythonStatus": status}


def requirement_status(requirement: str, runtime: tuple[int, int, int]) -> str:
    """`requires-python`의 하한만 실행 버전과 비교한다. 증명할 수 없는 형식은 판정하지 않는다.

    다른 조건은 허용 집합을 좁히기만 하므로, 실행 버전이 어느 하한에도 못 미치면 실제 최소 지원 버전에도 못 미친다.
    반대로 하한을 모두 넘어도 `!=` 조건에 걸리면 실제 최소 지원 버전(`>=3.8,!=3.8.*`이면 3.9)이 실행 버전보다
    새로울 수 있다. 상한(`<`·`<=`)은 최소 버전과 무관하므로 비교하지 않는다(`minimum-not-newer`는 호환 판정이 아니다).

    Args:
        requirement: 정식 메타데이터의 `requires-python` 문자열.
        runtime: 실행 Python의 (major, minor, micro) 최종 릴리스 버전.

    Returns:
        `runtime-below-minimum`, `minimum-not-newer`, 또는 하한이 없거나 모르는 조건이 있으면 `not-evaluated`.
    """
    bounds: list[tuple[tuple[int, ...], bool]] = []
    excluded = False
    for text in requirement.split(","):
        clause = _CLAUSE.fullmatch(text.strip())
        if clause is None:
            return "not-evaluated"
        operator, version, wildcard = clause.groups()
        release = tuple(int(part) for part in version.split("."))
        if (wildcard and operator not in ("==", "!=")) or (operator == "~=" and len(release) < 2):
            return "not-evaluated"
        padded = release + (0,) * (3 - len(release))
        if operator == "!=":
            excluded = excluded or (runtime[: len(release)] == release if wildcard else runtime == padded)
        elif operator in _LOWER_BOUNDS:
            bounds.append((padded, operator != ">"))
    if not bounds:
        return "not-evaluated"
    if any(runtime < bound or (runtime == bound and not inclusive) for bound, inclusive in bounds):
        return "runtime-below-minimum"
    return "not-evaluated" if excluded else "minimum-not-newer"


def source_lines(project: Project, statistics: dict[str, object]) -> list[str]:
    """스냅샷과 순회에 같은 분모·원인 경계를 보존하는 한계를 만든다."""
    analysis = cast(dict[str, object], statistics["sourceAnalysis"])
    graph = cast(SourceCounts, analysis["graphFiles"])
    supplemental = cast(SourceCounts, analysis["supplementalTestFiles"])
    outside = cast(SourceCounts, analysis["outsideScanFiles"])
    lines: list[str] = []
    if graph["unparsed"]:
        lines.append(
            f"unparsed-files: {graph['unparsed']} of {graph['files']} graph-eligible Python files could not be "
            f"read or parsed and have no symbols ({_reasons(graph)}); see graph statistics.sourceAnalysis"
        )
    if supplemental["unparsed"]:
        lines.append(
            f"supplemental-test-files: {supplemental['unparsed']} of {supplemental['files']} tests excluded from "
            f"graph symbols could not be read or parsed for program-flow context ({_reasons(supplemental)}); "
            "these files do not overlap graph-eligible omissions"
        )
    if outside["unparsed"]:
        lines.append(
            f"outside-scan-files: {outside['unparsed']} of {outside['files']} additional imported files outside "
            f"the Python file walk could not be read or parsed ({_reasons(outside)})"
        )
    if project.budget_skipped_modules:
        lines.append(
            f"source-budget: {project.budget_skipped_modules} Python files could not be analyzed: "
            + source_budget_reason()
            + "; this run-wide total overlaps the scope failure counts in graph statistics.sourceAnalysis"
        )
    parser = cast(dict[str, object], analysis.get("parser", {}))
    if parser:
        lines.append(_parser_line(parser))
    return lines


def _reasons(counts: SourceCounts) -> str:
    """원인별 수를 안정된 순서로 표시한다."""
    return "; ".join(f"{reason}: {count}" for reason, count in counts["failureReasons"].items())


def _parser_line(parser: dict[str, object]) -> str:
    """파서 거부를 소스 문법 오류로 단정하지 않고 확인 가능한 환경 차이를 알린다."""
    line = (
        f"python-parser: {parser['syntaxRejectedFiles']} files were rejected by {parser['implementation']} "
        f"{parser['version']} (syntax errors or syntax newer than this runtime)"
    )
    if parser.get("requiresPythonStatus") == "runtime-below-minimum":
        line += (
            f"; this runtime is below the declared minimum in pyproject.toml [project].requires-python "
            f"{parser['requiresPython']}; rerun with a runtime satisfying the project requirement"
        )
    return line
