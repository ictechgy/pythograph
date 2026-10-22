"""기존 심볼 기반 impact와 별도인 실제 변경 CLI다."""

from pythograph.cli.common import InputError, UsageError, resolve_project
from pythograph.cli.graph_commands import parse_command_line
from pythograph.exchange.document import DocumentLimitError, encode_document
from pythograph.graph.change import analyze_change
from pythograph.graph.model import DISPATCH_MODES
from pythograph.graph.traversal import TraversalRequest

CHANGE_USAGE = """Usage: pythograph change-impact --project <repository-root> --base <commit> [--head <commit>]
                              [--max-depth <1-128>] [--max-reached <1-100000>] [--dispatch direct|bound|candidates]

Compare frozen Git base/head sources, or base against tracked and nonignored untracked working-tree
files when --head is omitted. Before/after paths stay separate and preserve deleted callers.
Content digests cover captured bytes, not just graph topology. Seeds cover whole changed files.
Test sources are included. Static candidates are incomplete review evidence, never a safety verdict
or permission to skip tests. Existing impact <id> remains a one-snapshot graph traversal.
Exit codes: 0 report produced, 2 input/capture/output failure, 64 usage error.
"""


def run_change(arguments: list[str]) -> str:
    """변경 인자를 검증하고 bounded JSON 문서를 출력한다."""
    line = parse_command_line(
        arguments,
        ("--project", "--base", "--head", "--max-depth", "--max-reached", "--dispatch"),
        CHANGE_USAGE,
        positional=False,
    )
    if "--help" in line.flags:
        return CHANGE_USAGE
    project, base = line.values.get("--project"), line.values.get("--base")
    dispatch = line.values.get("--dispatch", "direct")
    try:
        depth, reached = int(line.values.get("--max-depth", "128")), int(line.values.get("--max-reached", "100000"))
    except ValueError as error:
        raise UsageError(CHANGE_USAGE) from error
    if (
        project is None
        or base is None
        or dispatch not in DISPATCH_MODES
        or not 1 <= depth <= 128
        or not 1 <= reached <= 100_000
    ):
        raise UsageError(CHANGE_USAGE)
    try:
        result = analyze_change(
            resolve_project(project),
            base,
            line.values.get("--head"),
            TraversalRequest((), "dependents", depth, reached, dispatch),
        )
        return encode_document(result)
    except (OSError, UnicodeError, DocumentLimitError) as error:
        raise InputError("change capture/output failed; check readable sources and capture budgets") from error
