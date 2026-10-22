"""Git 실제 변경을 별도 before/after 그래프에 연결한다. 두 시점의 간선을 합치지 않는다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from pythograph import __version__
from pythograph.cli.common import InputError
from pythograph.graph.build import build_graph
from pythograph.graph.document import GraphHeader, TraversalInput, build_traversal_document
from pythograph.graph.revision import _git, _git_executable, _LazyFetchRefusedError, _UnneutralizableDriverError
from pythograph.graph.traversal import TraversalRequest, traverse
from pythograph.source.project import Project

MAX_CAPTURE_FILES = 20_000
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class Capture:
    """실제 읽은 파일 바이트와 그 신원이다. revision은 내용 해시를 대체하지 않는다."""

    files: dict[str, bytes]
    digest: str
    revision: str | None
    omitted: int


def content_digest(files: dict[str, bytes]) -> str:
    """경로·바이트 길이·바이트를 함께 해시해 본문만 바뀐 변경도 구별한다."""
    digest = hashlib.sha256()
    for path, data in sorted(files.items()):
        digest.update(json.dumps([path, len(data)], ensure_ascii=False).encode())
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def _read_git(root: Path, *args: str) -> bytes:
    """기존 안전한 git 경계로 객체/이름만 읽는다. hook·filter·지연 fetch를 실행하지 않는다."""
    executable = _git_executable(root)
    if executable is None:
        raise InputError("trusted Git executable unavailable")
    try:
        result = _git(executable, root, *args)
    except (_UnneutralizableDriverError, _LazyFetchRefusedError) as error:
        raise InputError("Git capture refused unsafe filter configuration or unavailable local objects") from error
    if result is None or len(result) > MAX_TOTAL_BYTES:
        raise InputError("Git capture failed; check local objects, revisions and capture budgets")
    return result


def _safe_path(path: str) -> bool:
    """경로 탈출, Windows 별칭과 제어 문자를 거부한다."""
    return (
        bool(path)
        and not re.search(r"[\\:\x00-\x1f\x7f]", path)
        and all(
            part not in ("", ".", "..")
            and part.lower() != ".git"
            and not part.endswith((".", " "))
            and not re.match(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", part, re.IGNORECASE)
            for part in path.split("/")
        )
    )


def _regular_file(root: Path, path: str) -> bool:
    """경로 구성 요소의 symlink를 따라가지 않는다."""
    cursor = root
    for part in path.split("/"):
        cursor /= part
        if cursor.is_symlink():
            return False
    return cursor.is_file()


def capture(root: Path, revision: str | None = None) -> Capture:
    """커밋 또는 추적·비무시 미추적 작업 파일을 고정한다. 예산 초과는 부분 분석 없이 실패한다."""
    if not (root / ".git").exists():
        raise InputError("--project must be a Git repository root")
    commit = None
    if revision is not None:
        commit = _read_git(root, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise InputError("invalid Git commit")
    listing = (
        _read_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        if commit is None
        else _read_git(root, "ls-tree", "-rz", commit)
    )
    rows = [row for row in listing.decode("utf-8").split("\0") if row]
    if len(rows) > MAX_CAPTURE_FILES:
        raise InputError("capture file-count budget exceeded")
    files: dict[str, bytes] = {}
    spellings: set[str] = set()
    omitted = total = 0
    for row in rows:
        metadata, _, path = row.partition("\t") if commit else ("", "", row)
        if not _safe_path(path):
            raise InputError("capture contains unsupported path spelling")
        if (commit and metadata.split()[0] not in ("100644", "100755")) or (
            commit is None and not _regular_file(root, path)
        ):
            omitted += 1
            continue
        if path in files:
            continue
        spelling = path.lower() if os.name == "nt" else path
        if spelling in spellings:
            raise InputError("capture contains colliding file paths")
        spellings.add(spelling)
        size = (
            (root / path).stat().st_size
            if commit is None
            else int(_read_git(root, "cat-file", "-s", metadata.split()[2]))
        )
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            raise InputError("capture byte budget exceeded")
        data = (
            _read_working_file(root / path)
            if commit is None
            else _read_git(root, "cat-file", "blob", metadata.split()[2])
        )
        if len(data) != size:
            raise InputError("source changed during capture; retry on stable input")
        total += len(data)
        files[path] = data
    return Capture(files, content_digest(files), commit, omitted)


def _read_working_file(path: Path) -> bytes:
    """stat 이후 파일이 커져도 예산+1 byte만 읽는다."""
    with path.open("rb") as stream:
        return stream.read(MAX_FILE_BYTES + 1)


def changed_files(before: Capture, after: Capture) -> list[dict[str, str]]:
    """삭제/추가 경로를 모두 남기고 동일 바이트 rename 후보만 표시한다."""
    rows: list[dict[str, str]] = []
    for path in sorted(before.files.keys() | after.files.keys()):
        old, current = before.files.get(path), after.files.get(path)
        if old == current:
            continue
        rows.append({"path": path, "status": "added" if old is None else "deleted" if current is None else "modified"})
    for row in rows:
        if row["status"] == "added":
            matches = [
                old["path"]
                for old in rows
                if old["status"] == "deleted" and before.files[old["path"]] == after.files[row["path"]]
            ]
            if len(matches) == 1:
                row["renameCandidateFrom"] = matches[0]
    return rows


def _provenance(value: Capture) -> dict[str, object]:
    """캡처 출처와 내용 신선도 근거다."""
    return {
        "kind": "git" if value.revision else "working-tree",
        "revision": value.revision,
        "contentDigest": value.digest,
        "freshness": "captured-content",
        "files": len(value.files),
        "omittedFiles": value.omitted,
    }


def _analyze(value: Capture, destination: Path, paths: set[str], request: TraversalRequest) -> dict[str, object]:
    """한 캡처만 materialize하고 파일 seed에서 기존 순회 엔진으로 경로를 만든다."""
    destination.mkdir()
    for path, data in value.files.items():
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    graph = build_graph(Project.open(destination), include_tests=True)
    selected = [node for node in graph.nodes if node.location.path in paths]
    nodes = selected[:10_000]
    roots = tuple(node.id for node in nodes)
    traversal_request = TraversalRequest(roots, "dependents", request.max_depth, request.max_reached, request.dispatch)
    result = traverse(graph, traversal_request)
    header = GraphHeader(__version__, datetime.now(timezone.utc), ".", value.revision)
    document = build_traversal_document(
        TraversalInput(header, graph, "dependents", request.dispatch, roots, frozenset(), result)
    )
    mapped = {node.location.path for node in graph.nodes}
    node_map = graph.node_map()
    return {
        "input": _provenance(value),
        "seedsTruncated": len(selected) > len(nodes),
        "seeds": [
            {"id": node.id, "path": node.location.path, "reason": "changed-file", "precision": "file"} for node in nodes
        ],
        "unmappedFiles": sorted(paths - mapped),
        "traversal": document,
        "reviewCandidates": [
            {
                "id": row.id,
                "via": row.via,
                "depth": row.depth,
                "evidence": row.evidence,
                "path": node_map[row.id].location.path,
                "testCandidate": any(part.startswith("test") for part in Path(node_map[row.id].location.path).parts),
            }
            for row in result.reached
        ],
    }


def analyze_change(root: Path, base: str, head: str | None, request: TraversalRequest) -> dict[str, object]:
    """캡처 비용을 포함해 두 시점을 분석한다. 빈 결과도 항상 불완전한 검토 근거다."""
    started = time.perf_counter()
    before, after = capture(root, base), capture(root, head)
    if head is None and capture(root).digest != after.digest:
        raise InputError("working tree changed during capture; retry on stable input")
    changes = changed_files(before, after)
    preparation = (time.perf_counter() - started) * 1000
    with TemporaryDirectory(prefix="pythograph-change-") as temporary:
        old = _analyze(
            before, Path(temporary) / "before", {row["path"] for row in changes if row["status"] != "added"}, request
        )
        current = _analyze(
            after, Path(temporary) / "after", {row["path"] for row in changes if row["status"] != "deleted"}, request
        )
    return {
        "format": "pythograph-change-impact",
        "version": 1,
        "mode": "before-after",
        "status": "incomplete",
        "tool": {"name": "pythograph", "version": __version__},
        "changes": changes,
        "before": old,
        "after": current,
        "scope": {"files": "tracked-and-nonignored-untracked", "includeTests": True, "sourceExecution": False},
        "limits": {
            "files": MAX_CAPTURE_FILES,
            "fileBytes": MAX_FILE_BYTES,
            "totalBytes": MAX_TOTAL_BYTES,
            "maxDepth": request.max_depth,
            "maxSeeds": 10_000,
            "maxReached": request.max_reached,
            "outputCharacters": 16 * 1024 * 1024,
        },
        "cost": {"preparationMilliseconds": preparation, "totalMilliseconds": (time.perf_counter() - started) * 1000},
        "limitations": [
            "file-seeds: all declarations in changed files are candidates; unchanged declarations may be over-selected",
            "static-candidates: connected test candidates are review evidence, not predicted failures or permission "
            "to skip tests; empty results do not establish safety",
            "capture-scope: ignored files, symlinks and submodules are not followed; "
            "both traversal limitation lists apply",
            "unmapped-files: runtime configuration, contracts and generated code without declarations are not assessed",
            "framework-unknown: signals, middleware, settings-named classes and framework-created "
            "serializer/permission callbacks may be unresolved; no runtime completeness claim",
            "snapshot-isolation: every witness belongs to one capture; before and after edges are never combined",
            "seed-budget: at most 10000 seeds per capture are traversed; seedsTruncated marks omitted declarations "
            "and their unknown propagation",
        ],
    }
