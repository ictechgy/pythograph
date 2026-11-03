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
from dataclasses import dataclass, field
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

#: 작업 트리의 비분석 파일 git blob id를 구하려고 흘려 읽는 바이트 합의 상한이다(메모리에 담지 않는다).
MAX_HASH_BYTES = 4 * 1024 * 1024 * 1024

#: 그래프가 읽는 파이썬 소스 밖의 파일 이름이다(패키지 판정·`requires-python`). 이 파일과 `.py`만 바이트를 캡처한다.
_ANALYZED_MANIFESTS = frozenset({"pyproject.toml", "setup.cfg"})

#: `git cat-file --batch` 출력의 객체 머리 형식이다.
_BATCH_HEADER = re.compile(rb"([0-9a-f]{40}|[0-9a-f]{64}) blob ([0-9]+)\n")


@dataclass(frozen=True)
class Capture:
    """실제 읽은 파일 바이트와 그 신원이다. revision은 내용 해시를 대체하지 않는다.

    Attributes:
        files: 그래프가 읽는 파일(`.py`, `pyproject.toml`, `setup.cfg`) 경로 → 바이트.
        digest: `files`와 `others`의 내용 해시.
        revision: 커밋 캡처면 커밋 id.
        omissions: 따르지 않은 경로와 이유.
        others: 그래프가 읽지 않는 추적·비무시 파일 경로 → git blob id(바뀜·rename 후보 비교용, 바이트는 담지 않는다).
    """

    files: dict[str, bytes]
    digest: str
    revision: str | None
    omissions: tuple[SourceOmission, ...]
    others: dict[str, str] = field(default_factory=dict)

    @property
    def omitted(self) -> int:
        """기존 개수 필드를 유지하면서 경로·이유는 별도로 보존한다."""
        return len(self.omissions)


def content_digest(files: dict[str, bytes], others: dict[str, str] | None = None) -> str:
    """경로·바이트 길이·바이트를 함께 해시해 본문만 바뀐 변경도 구별한다. 비분석 파일은 경로와 blob id를 더한다."""
    digest = hashlib.sha256()
    for path, data in sorted(files.items()):
        digest.update(json.dumps([path, len(data)], ensure_ascii=False).encode())
        digest.update(data)
    for path, blob in sorted((others or {}).items()):
        digest.update(json.dumps([path, "blob", blob], ensure_ascii=False).encode())
    return "sha256:" + digest.hexdigest()


def is_analyzed(path: str) -> bool:
    """change-impact 그래프가 읽는 파일인지 본다(바이트를 캡처한다).

    Args:
        path: 저장소 상대 경로.

    Returns:
        `.py`이거나 패키지 판정 매니페스트면 True.
    """
    name = PurePosixPath(path).name
    return name.endswith(".py") or name in _ANALYZED_MANIFESTS


def _read_git(root: Path, *args: str, input_data: bytes | None = None, output_limit: int | None = None) -> bytes:
    """기존 안전한 git 경계로 객체/이름만 읽는다. hook·filter·지연 fetch를 실행하지 않는다.

    Args:
        root: 저장소 루트.
        args: git 하위 명령과 인자.
        input_data: 표준 입력(`cat-file --batch`의 객체 id 목록).
        output_limit: 출력 바이트 상한(없으면 `MAX_TOTAL_BYTES`, 호출 시점 값).

    Returns:
        표준 출력.
    """
    limit = MAX_TOTAL_BYTES if output_limit is None else output_limit
    executable = _git_executable(root)
    if executable is None:
        raise InputError("trusted Git executable unavailable")
    try:
        result = (
            _git(executable, root, *args, output_limit=limit)
            if input_data is None
            else _git(executable, root, *args, output_limit=limit, input_data=input_data)
        )
    except (_UnneutralizableDriverError, _LazyFetchRefusedError) as error:
        raise InputError("Git capture refused unsafe filter configuration or unavailable local objects") from error
    if result is None or len(result) > limit:
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
    """커밋 또는 추적·비무시 미추적 작업 파일을 고정한다. 예산 초과는 부분 분석 없이 실패한다.

    그래프가 읽는 파일(`is_analyzed`)만 바이트를 캡처해 파일·합산 예산을 적용한다. 나머지 파일은 git blob id로만
    비교하므로 이미지·데이터 같은 큰 파일이 분석 예산을 넘기지 않는다. 커밋 캡처는 `ls-tree -l`의 크기와 한 번의
    `cat-file --batch`로 분석 파일을 읽어 파일 수만큼 git을 띄우지 않는다.
    """
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
        else _read_git(root, "ls-tree", "-rlz", commit)
    )
    rows = [row for row in listing.decode("utf-8").split("\0") if row]
    if len(rows) > MAX_CAPTURE_FILES:
        raise InputError("capture file-count budget exceeded")
    state = _CaptureState()
    for row in rows:
        metadata, _, path = row.partition("\t") if commit else ("", "", row)
        _capture_row(root, commit, metadata.split(), path, state)
    if commit is not None:
        state.files.update(_read_blobs(root, state.pending))
    for path, size in state.sizes.items():
        if len(state.files[path]) != size:
            raise InputError("source changed during capture; retry on stable input")
    return Capture(state.files, content_digest(state.files, state.others), commit, tuple(state.omissions), state.others)


@dataclass
class _CaptureState:
    """캡처 중인 파일·신원·예산 상태다."""

    files: dict[str, bytes] = field(default_factory=dict)
    others: dict[str, str] = field(default_factory=dict)
    omissions: list[SourceOmission] = field(default_factory=list)
    spellings: set[str] = field(default_factory=set)
    sizes: dict[str, int] = field(default_factory=dict)
    pending: dict[str, tuple[str, int]] = field(default_factory=dict)
    total: int = 0
    hashed: int = 0
    object_format: str | None = None


def _capture_row(root: Path, commit: str | None, metadata: list[str], path: str, state: _CaptureState) -> None:
    """목록 한 줄을 분석 파일·비분석 파일·누락으로 나눈다.

    Args:
        root: 저장소 루트.
        commit: 커밋 캡처면 커밋 id.
        metadata: `ls-tree -l` 줄의 모드·종류·객체·크기(작업 트리면 빈 목록).
        path: 경로.
        state: 캡처 상태.
    """
    if not _safe_path(path):
        raise InputError("capture contains unsupported path spelling")
    if (commit and metadata[0] not in ("100644", "100755")) or (commit is None and not _regular_file(root, path)):
        state.omissions.append(
            _omission(path, "submodule" if metadata[0] == "160000" else "symlink")
            if commit
            else _working_omission(root, path)
        )
        return
    if path in state.files or path in state.others or path in state.pending:
        return
    spelling = path.lower() if os.name == "nt" else path
    if spelling in state.spellings:
        raise InputError("capture contains colliding file paths")
    state.spellings.add(spelling)
    if commit is not None:
        _capture_blob_row(metadata, path, state)
    elif is_analyzed(path):
        size = Project(root)._checked_stat(path).st_size
        _charge(state, path, size)
        state.files[path] = _read_working_file(root, path)
    else:
        state.others[path] = _working_blob_id(root, path, state)


def _capture_blob_row(metadata: list[str], path: str, state: _CaptureState) -> None:
    """커밋 캡처의 blob 줄을 처리한다. 분석 파일은 한 번의 일괄 읽기에 넣고, 나머지는 blob id만 쓴다.

    Args:
        metadata: 모드·종류·객체·크기.
        path: 경로.
        state: 캡처 상태.
    """
    if len(metadata) != 4 or not metadata[3].isdigit():
        raise InputError("Git capture returned an unexpected tree listing")
    blob, size = metadata[2], int(metadata[3])
    if not is_analyzed(path):
        state.others[path] = blob
        return
    _charge(state, path, size)
    state.pending[path] = (blob, size)


def _charge(state: _CaptureState, path: str, size: int) -> None:
    """분석 파일 하나를 파일·합산 바이트 예산에 넣는다.

    Args:
        state: 캡처 상태.
        path: 경로.
        size: 바이트 수.
    """
    if size > MAX_FILE_BYTES or state.total + size > MAX_TOTAL_BYTES:
        raise InputError("capture byte budget exceeded")
    state.total += size
    state.sizes[path] = size


def _read_blobs(root: Path, pending: dict[str, tuple[str, int]]) -> dict[str, bytes]:
    """분석 파일 blob을 `git cat-file --batch` 한 번으로 읽는다(필터·textconv 없음).

    Args:
        root: 저장소 루트.
        pending: 경로 → (blob id, 크기).

    Returns:
        경로 → 바이트.
    """
    if not pending:
        return {}
    order = sorted(pending.items())
    request = b"".join(blob.encode() + b"\n" for _, (blob, _) in order)
    # 머리 줄(객체 id·종류·크기)과 내용 뒤 줄바꿈, 같은 예산을 쓰는 설정 조회 출력의 여유다.
    budget = sum(size for _, (_, size) in order) + 96 * len(order) + 64 * 1024
    output = _read_git(root, "cat-file", "--batch", input_data=request, output_limit=budget)
    result: dict[str, bytes] = {}
    offset = 0
    for path, (blob, size) in order:
        header = _BATCH_HEADER.match(output, offset)
        if header is None or header.group(1).decode() != blob or int(header.group(2)) != size:
            raise InputError("Git capture returned unexpected object data; check local objects")
        start = header.end()
        result[path] = output[start : start + size]
        offset = start + size + 1
    if offset != len(output):
        raise InputError("Git capture returned unexpected object data; check local objects")
    return result


def _working_blob_id(root: Path, path: str, state: _CaptureState) -> str:
    """작업 트리 파일의 git blob id를 흘려 읽어 구한다(`git hash-object --no-filters`와 같은 값, 메모리에 담지 않는다).

    Args:
        root: 저장소 루트.
        path: 경로.
        state: 캡처 상태(해시 예산·객체 형식).

    Returns:
        blob id(16진수).
    """
    if state.object_format is None:
        state.object_format = _object_format(root)
    project = Project(root)
    size = project._checked_stat(path).st_size
    if state.hashed + size > MAX_HASH_BYTES:
        raise InputError("capture hash budget exceeded")
    state.hashed += size
    digest = hashlib.new(state.object_format)
    digest.update(f"blob {size}\0".encode())
    read = 0
    try:
        with project.open_regular(path) as source:
            while chunk := source.read(min(1024 * 1024, size + 1 - read)):
                digest.update(chunk)
                read += len(chunk)
    except OSError as error:
        raise InputError("source changed during capture; retry on stable input") from error
    if read != size:
        raise InputError("source changed during capture; retry on stable input")
    return digest.hexdigest()


def _object_format(root: Path) -> str:
    """저장소 객체 id 형식(`sha1`·`sha256`)을 읽는다.

    Args:
        root: 저장소 루트.

    Returns:
        hashlib 알고리즘 이름.
    """
    value = _read_git(root, "rev-parse", "--show-object-format").decode().strip()
    if value not in ("sha1", "sha256"):
        raise InputError("Git capture returned an unknown object format")
    return value


def _read_working_file(root: Path, path: str) -> bytes:
    """Project의 nofollow·dir-fd·nonblock·핸들 신원 검사와 제한 읽기를 그대로 쓴다."""
    return Project(root)._read_bytes(path, MAX_FILE_BYTES)


def changed_files(before: Capture, after: Capture) -> list[dict[str, str]]:
    """삭제/추가 경로를 모두 남기고 동일 내용 rename 후보만 표시한다. 비분석 파일은 git blob id로 비교한다."""
    rows: list[dict[str, str]] = []
    paths = before.files.keys() | before.others.keys() | after.files.keys() | after.others.keys()
    for path in sorted(paths):
        old, current = _identity(before, path), _identity(after, path)
        if old == current:
            continue
        rows.append({"path": path, "status": "added" if old is None else "deleted" if current is None else "modified"})
    for row in rows:
        if row["status"] == "added":
            matches = [
                old["path"]
                for old in rows
                if old["status"] == "deleted" and _identity(before, old["path"]) == _identity(after, row["path"])
            ]
            if len(matches) == 1:
                row["renameCandidateFrom"] = matches[0]
    return rows


def _identity(value: Capture, path: str) -> tuple[str, bytes | str] | None:
    """경로의 내용 신원이다: 분석 파일은 바이트, 나머지는 blob id. 같은 경로는 양쪽에서 같은 종류다.

    Args:
        value: 캡처.
        path: 경로.

    Returns:
        (종류, 값), 없으면 None.
    """
    if path in value.files:
        return ("bytes", value.files[path])
    if path in value.others:
        return ("blob", value.others[path])
    return None


def _provenance(value: Capture) -> dict[str, object]:
    """캡처 출처와 내용 신선도 근거다."""
    return {
        "kind": "git" if value.revision else "working-tree",
        "revision": value.revision,
        "contentDigest": value.digest,
        "freshness": "captured-content",
        "files": len(value.files),
        "identityOnlyFiles": len(value.others),
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
            "hashBytes": MAX_HASH_BYTES,
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
