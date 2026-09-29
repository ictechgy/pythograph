"""그래프의 모드별 한계 문구.

문구는 접두사(`unresolved-calls:` 등)로 시작하고 수를 싣는다. 경로·소스 원문은 싣지 않는다. 빈 결과나 0을 완전성의
증거로 삼지 않도록, 모델링하지 않은 것(외부 코드의 역호출, 시그널·미들웨어)을 수와 함께 밝힌다.
"""

from __future__ import annotations

from typing import cast

from pythograph.graph.model import DISPATCH_MODES
from pythograph.source.project import Project

#: 프레임워크 표를 만든 설치본 버전이다.
FRAMEWORK_VERSIONS = "Django 5.2.17, djangorestframework 3.18.1, Flask 3.1.3"


def graph_limitations(project: Project, statistics: dict[str, object]) -> dict[str, list[str]]:
    """모드별 한계 문구를 만든다.

    Args:
        project: 분석 대상 프로젝트.
        statistics: 그래프 집계.

    Returns:
        모드 → 문구 목록.
    """
    shared = _shared(project, statistics)
    return {mode: _mode_lines(mode, statistics) + shared for mode in DISPATCH_MODES}


def _counts(statistics: dict[str, object], key: str) -> dict[str, int]:
    """집계의 이유별 사전을 꺼낸다.

    Args:
        statistics: 집계.
        key: 키.

    Returns:
        이유 → 수.
    """
    return cast(dict[str, int], statistics.get(key, {}))


def _mode_lines(mode: str, statistics: dict[str, object]) -> list[str]:
    """모드에 따라 달라지는 문구다.

    Args:
        mode: 디스패치 모드.
        statistics: 집계.

    Returns:
        문구 목록.
    """
    unresolved = dict(_counts(statistics, "unresolvedCalls"))
    for reason, count in _counts(statistics, "frameworkUnresolved").items():
        unresolved[reason] = unresolved.get(reason, 0) + count
    partial = _counts(statistics, "partialCalls")
    if mode != "candidates":
        for reason, count in partial.items():
            unresolved[reason] = unresolved.get(reason, 0) + count
    lines: list[str] = []
    total = sum(unresolved.values())
    if total:
        detail = ", ".join(f"{reason} {count}" for reason, count in sorted(unresolved.items()))
        lines.append(
            f"unresolved-calls: {total} call sites have no complete project target in {mode} mode ({detail}); "
            "each symbol's unresolvedCalls counts its own"
        )
    partial_total = sum(partial.values())
    if partial_total and mode == "candidates":
        lines.append(
            f"candidate-dispatch: {partial_total} calls through self, cls, or annotated receivers are linked to "
            "methods of project subclasses by candidate edges (possible targets, not observed flows)"
        )
    elif partial_total:
        lines.append(
            f"overridden-methods: {partial_total} calls through self, cls, or annotated receivers may reach methods "
            "of project subclasses; they count as unresolved here and are linked with --dispatch candidates"
        )
    if mode == "bound":
        lines.append(
            "bound-dispatch: pythograph does not produce bound edges yet; --dispatch bound follows the direct graph"
        )
    return lines


def _shared(project: Project, statistics: dict[str, object]) -> list[str]:
    """모든 모드에 공통인 문구다.

    Args:
        project: 분석 대상 프로젝트.
        statistics: 집계.

    Returns:
        문구 목록.
    """
    lines: list[str] = []
    external = _counts(statistics, "callSites").get("external", 0)
    if external:
        lines.append(
            f"external-calls: {external} call sites resolve outside the project (standard library, installed "
            "packages, framework members, or attribute calls on untyped receivers whose name no project class, "
            "module, or attribute write defines); calls from external code back into the project are not followed"
        )
    handlers = cast(int, statistics.get("viewHandlers", 0))
    if handlers:
        lines.append(
            f"framework-dispatch: {handlers} class-based view handler symbols are linked to the project hooks their "
            f"framework dispatch path calls ({FRAMEWORK_VERSIONS} sources); objects the framework builds from class "
            "attributes (serializers, permissions, pagination) count as framework-callback unresolved calls, and "
            "signals, middleware, and classes named in settings are not modeled"
        )
    lines.extend(_scan_lines(project, statistics))
    return lines


def _scan_lines(project: Project, statistics: dict[str, object]) -> list[str]:
    """입력 범위·계층 근사에 관한 문구다.

    Args:
        project: 분석 대상 프로젝트.
        statistics: 집계.

    Returns:
        문구 목록.
    """
    lines: list[str] = []
    writes = cast(int, statistics.get("dynamicAttributeWrites", 0))
    if writes:
        lines.append(
            f"dynamic-attribute-writes: {writes} setattr calls use computed names; a function attached this way and "
            "called through an untyped receiver is counted as external"
        )
    approximated = cast(int, statistics.get("mroApproximated", 0))
    if approximated:
        lines.append(
            f"mro-approximated: {approximated} classes have an inconsistent or cyclic hierarchy; their method "
            "resolution order is approximated"
        )
    unparsed = cast(int, statistics.get("unparsedFiles", 0))
    if unparsed:
        lines.append(f"unparsed-files: {unparsed} Python files could not be read or parsed and have no symbols")
    if project.scan_capped or project.skipped_links or project.unencodable_names:
        lines.append(
            "scan-incomplete: the file walk stopped at a limit or skipped symbolic links or undecodable names; "
            "symbols in those files are missing"
        )
    return lines
