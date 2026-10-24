"""Git 실제 변경을 별도 before/after 그래프에 연결한다. 두 시점의 간선을 합치지 않는다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from pythograph import __version__
from pythograph.cli.common import InputError
from pythograph.graph.build import build_graph
from pythograph.graph.document import GraphHeader, TraversalInput, build_traversal_document
from pythograph.graph.revision import _git, _git_executable, _LazyFetchRefusedError, _UnneutralizableDriverError
from pythograph.graph.traversal import TraversalRequest, traverse
from pythograph.source.project import (
    Project,
    SourceOmission,
    _is_link,
    _is_skipped_directory,
    _link_may_hide_sources,
    is_test_path,
)

MAX_CAPTURE_FILES = 20_000
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class Capture:
    """실제 읽은 파일 바이트와 그 신원이다. revision은 내용 해시를 대체하지 않는다."""

    files: dict[str, bytes]
    digest: str
    revision: str | None
    omissions: tuple[SourceOmission, ...]

    @property
    def omitted(self) -> int:
        """기존 개수 필드를 유지하면서 경로·이유는 별도로 보존한다."""
        return len(self.omissions)


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
        result = _git(executable, root, *args, output_limit=MAX_TOTAL_BYTES)
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
    """원래 Project와 같은 경로·링크·재분석 지점 검사로 정규 파일만 고른다."""
    try:
        return stat.S_ISREG(Project(root)._checked_stat(path).st_mode)
    except OSError:
        return False


def _omission(path: str, reason: str, boundary: str | None = None) -> SourceOmission:
    """대상에 접근하지 않고 소스 누락 가능성을 원래 순회 이름 규칙으로 분류한다."""
    parts = PurePosixPath(boundary or path).parts
    visible = not any(_is_skipped_directory(part) for part in parts[:-1])
    source = visible and (
        not _is_skipped_directory(parts[-1])
        if reason in ("submodule", "directory")
        else reason != "missing" and _link_may_hide_sources(parts[-1])
    )
    return SourceOmission(path, reason, source)


def _working_omission(root: Path, path: str) -> SourceOmission:
    """따르지 않은 경계의 종류만 lstat으로 읽고, 실제 삭제는 소스 미확인과 구분한다."""
    cursor = root
    parts = path.split("/")
    for index, part in enumerate(parts):
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            return _omission(path, "missing")
        if _is_link(info):
            return _omission(path, "symlink", "/".join(parts[: index + 1]))
    return _omission(path, "directory" if stat.S_ISDIR(info.st_mode) else "non-regular")


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
    omissions: list[SourceOmission] = []
    total = 0
    for row in rows:
        metadata, _, path = row.partition("\t") if commit else ("", "", row)
        if not _safe_path(path):
            raise InputError("capture contains unsupported path spelling")
        if (commit and metadata.split()[0] not in ("100644", "100755")) or (
            commit is None and not _regular_file(root, path)
        ):
            omissions.append(
                _omission(path, "submodule" if metadata.split()[0] == "160000" else "symlink")
                if commit
                else _working_omission(root, path)
            )
            continue
        if path in files:
            continue
        spelling = path.lower() if os.name == "nt" else path
        if spelling in spellings:
            raise InputError("capture contains colliding file paths")
        spellings.add(spelling)
        size = (
            Project(root)._checked_stat(path).st_size
            if commit is None
            else int(_read_git(root, "cat-file", "-s", metadata.split()[2]))
        )
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            raise InputError("capture byte budget exceeded")
        data = (
            _read_working_file(root, path)
            if commit is None
            else _read_git(root, "cat-file", "blob", metadata.split()[2])
        )
        if len(data) != size:
            raise InputError("source changed during capture; retry on stable input")
        total += len(data)
        files[path] = data
    return Capture(files, content_digest(files), commit, tuple(omissions))


def _read_working_file(root: Path, path: str) -> bytes:
    """Project의 nofollow·dir-fd·nonblock·핸들 신원 검사와 제한 읽기를 그대로 쓴다."""
    return Project(root)._read_bytes(path, MAX_FILE_BYTES)


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
        "omissions": [
            {"path": item.path, "reason": item.reason, "sourceMayBeHidden": item.may_hide_source}
            for item in value.omissions
        ],
    }


def _analyze(
    value: Capture, destination: Path, paths: set[str], request: TraversalRequest, project_identity: str
) -> dict[str, object]:
    """한 캡처만 materialize하고 파일 seed에서 기존 순회 엔진으로 경로를 만든다."""
    _materialize_capture(value, destination)
    graph = build_graph(Project.open(destination, source_omissions=value.omissions), include_tests=True)
    if value.omissions:
        reasons = ", ".join(
            f"{reason}={count}" for reason, count in sorted(Counter(item.reason for item in value.omissions).items())
        )
        hidden = sum(item.may_hide_source for item in value.omissions)
        for limitations in graph.limitations.values():
            limitations.append(
                f"capture-omissions: {value.omitted} paths omitted ({reasons}); {hidden} may hide sources; "
                "capture provenance lists their paths and reasons, and omitted sources keep program evidence open"
            )
    selected = [node for node in graph.nodes if node.location.path in paths]
    nodes = selected[:10_000]
    roots = tuple(node.id for node in nodes)
    traversal_request = TraversalRequest(roots, "dependents", request.max_depth, request.max_reached, request.dispatch)
    result = traverse(graph, traversal_request)
    header = GraphHeader(__version__, datetime.now(timezone.utc), project_identity, value.revision)
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
                "testCandidate": is_test_path(node_map[row.id].location.path),
            }
            for row in result.reached
        ],
    }


@contextmanager
def _materialized_directory(
    project: Project, relative: str, directories: dict[str, os.stat_result]
) -> Iterator[int | Path]:
    """등록한 실제 디렉터리 핸들만 사용하고 루트·부모의 링크 교체를 거부한다."""
    expected = directories[relative]
    if not os.path.samestat(directories["."], project.root.lstat()):
        raise InputError("capture destination changed during materialization")
    with project._open_directory(relative) as directory:
        actual = os.fstat(directory) if isinstance(directory, int) else directory.lstat()
        if not stat.S_ISDIR(actual.st_mode) or not os.path.samestat(expected, actual):
            raise InputError("capture destination changed during materialization")
        yield directory
        if not os.path.samestat(expected, project._checked_stat(relative)) or not os.path.samestat(
            directories["."], project.root.lstat()
        ):
            raise InputError("capture destination changed during materialization")


def _materialize_parents(
    project: Project,
    parts: list[str],
    directories: dict[str, os.stat_result],
    identities: dict[tuple[int, int], str],
) -> str:
    """동일 raw prefix만 재사용한다. 새 prefix의 EEXIST나 같은 inode 별칭은 입력 실패다."""
    parent = "."
    for index, part in enumerate(parts):
        relative = "/".join(parts[: index + 1])
        if relative in directories:
            if not os.path.samestat(directories[relative], project._checked_stat(relative)):
                raise InputError("capture destination changed during materialization")
        else:
            with _materialized_directory(project, parent, directories) as directory:
                if isinstance(directory, int):
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                else:
                    (directory / part).mkdir(mode=0o700)
            info = project._checked_stat(relative)
            identity = info.st_dev, info.st_ino
            if not stat.S_ISDIR(info.st_mode) or identity in identities:
                raise InputError("capture contains colliding physical directory paths")
            directories[relative], identities[identity] = info, relative
        parent = relative
    return parent


def _materialize_capture(value: Capture, destination: Path) -> None:
    """raw Git 경로를 실제 파일 시스템에 단사로 쓴다. 플랫폼 전체 casefold는 사용하지 않는다."""
    try:
        destination.mkdir()
        project = Project(destination)
        root = destination.lstat()
        directories = {".": root}
        identities = {(root.st_dev, root.st_ino): "."}
        for relative, data in sorted(value.files.items()):
            if not _safe_path(relative):
                raise InputError("capture contains unsupported path spelling")
            parts = relative.split("/")
            parent = _materialize_parents(project, parts[:-1], directories, identities)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            with _materialized_directory(project, parent, directories) as directory:
                descriptor = (
                    os.open(parts[-1], flags, 0o600, dir_fd=directory)
                    if isinstance(directory, int)
                    else os.open(directory / parts[-1], flags, 0o600)
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    actual = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(actual.st_mode)
                        or actual.st_size != len(data)
                        or not os.path.samestat(actual, project._checked_stat(relative))
                    ):
                        raise InputError("capture destination changed during materialization")
    except OSError as error:
        raise InputError("capture cannot preserve paths on destination filesystem") from error


def analyze_change(root: Path, base: str, head: str | None, request: TraversalRequest) -> dict[str, object]:
    """캡처 비용을 포함해 두 시점을 분석한다. 빈 결과도 항상 불완전한 검토 근거다."""
    started = time.perf_counter()
    project_identity = root.resolve().as_posix()
    before, after = capture(root, base), capture(root, head)
    if head is None and capture(root).digest != after.digest:
        raise InputError("working tree changed during capture; retry on stable input")
    changes = changed_files(before, after)
    preparation = (time.perf_counter() - started) * 1000
    with TemporaryDirectory(prefix="pythograph-change-") as temporary:
        old = _analyze(
            before,
            Path(temporary) / "before",
            {row["path"] for row in changes if row["status"] != "added"},
            request,
            project_identity,
        )
        current = _analyze(
            after,
            Path(temporary) / "after",
            {row["path"] for row in changes if row["status"] != "deleted"},
            request,
            project_identity,
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
