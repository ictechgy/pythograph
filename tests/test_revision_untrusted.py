"""revision 판독 git이 분석 대상 `.git`(과 사용자·시스템) 설정의 명령을 실행하지 않는지 본다(README "Security").

앞쪽 테스트는 실제 git으로 악성 filter 드라이버가 표식 파일에 한 줄을 남기는 저장소를 만든다. 설정은 커밋한 **뒤에**
넣는다(먼저 넣으면 준비용 `git add`가 그 명령을 실행한다). 추적 파일의 stat을 2001년으로 돌려 git이 내용을 다시 읽게
한다(같은 초의 touch는 racy 재해시라 결정적이지 않다). 그다음 graph·reach·impact(`--revision` 없음)와 `git_revision`을
불러 표식이 없는지와 revision 정책(덮으면 원문 그대로라 깨끗하면 HEAD, 정규화가 풀려 바뀐 것으로 보이면 생략, 덮을 수
없는 이름·서브모듈이면 생략과 `revision-unverified:` 한계)을 확인한다. 뒤쪽 테스트는 `subprocess.run` 대역으로 인자
계약과 fail-closed 경로를 고정한다.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pythograph.graph import revision as revision_module
from tests.conftest import GENERATED_AT, run_cli

#: 표식 filter가 POSIX 셸 스크립트라 실제 git 테스트는 POSIX에서만 돈다.
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="표식 filter가 POSIX 셸 스크립트다.")

#: 낡은 stat으로 돌릴 시각(2001-09-09)이다.
STALE_TIME = 1_000_000_000

#: 일부러 확인하지 않아 revision을 싣지 못했을 때의 한계 접두사다.
UNVERIFIED = "revision-unverified:"

#: 저장소의 파이썬 소스다.
APP = "def handler():\n    return 1\n"

#: 대역 git이 돌려줄 커밋 id다.
OBJECT_ID = "0123456789abcdef0123456789abcdef01234567"

#: 모든 git 호출 맨 앞에 붙는 공통 설정이다.
BASE_CONFIG = ["core.fsmonitor=false", "core.untrackedCache=false", "log.showSignature=false"]

#: 드라이버 이름을 모으는 설정 조회 인자다.
DRIVER_QUERY = ["config", "-z", "--includes", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"]


@dataclass(frozen=True)
class Sandbox:
    """테스트 하나의 작업 공간이다.

    Attributes:
        project: 분석할 저장소.
        tools: 표식 스크립트·표식 파일·서브모듈 원본을 두는 프로젝트 밖 디렉터리.
        home: 준비용·분석용 git이 보는 HOME(사용자 전역 설정을 떼어 놓는다).
    """

    project: Path
    tools: Path
    home: Path

    @property
    def marker(self) -> Path:
        """filter 명령이 실행되면 생기는 표식 파일이다."""
        return self.tools / "filter-ran"


@dataclass(frozen=True)
class Expected:
    """변형의 revision 정책이다.

    Attributes:
        revision: 기대하는 revision(None이면 싣지 않는다).
        unverified: `revision-unverified:` 한계를 기대하는지.
    """

    revision: str | None
    unverified: bool = False


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    """작업 공간을 만들고, 분석용 git(HOME을 넘긴다)이 사용자 전역 설정을 읽지 않게 HOME을 바꾼다.

    Args:
        tmp_path: pytest 임시 디렉터리.
        monkeypatch: 환경 변수 교체.

    Returns:
        작업 공간.
    """
    box = Sandbox(tmp_path / "project", tmp_path / "tools", tmp_path / "home")
    for directory in (box.project, box.tools, box.home):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(box.home))
    return box


def setup_git(box: Sandbox, directory: Path, *arguments: str) -> str:
    """준비용 git을 실행한다(사용자·시스템 설정을 읽지 않고 작성자를 고정한다).

    Args:
        box: 작업 공간.
        directory: 작업 디렉터리.
        arguments: git 인자.

    Returns:
        표준 출력.
    """
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(box.home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "LC_ALL": "C",
    }
    completed = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *arguments],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def commit_tree(box: Sandbox, directory: Path, files: dict[str, str], add_config: Sequence[str] = ()) -> str:
    """파일을 쓰고 커밋 하나짜리 저장소를 만든다.

    Args:
        box: 작업 공간.
        directory: 저장소 디렉터리.
        files: 상대 경로 → 내용.
        add_config: `git add`에만 줄 `-c` 인자(정규화한 blob을 만들 때).

    Returns:
        HEAD 커밋 id.
    """
    for relative, text in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    setup_git(box, directory, "init", "-q")
    setup_git(box, directory, *add_config, "add", "-A")
    setup_git(box, directory, "commit", "-q", "-m", "init")
    return setup_git(box, directory, "rev-parse", "HEAD").strip()


def marker_command(box: Sandbox, transform: str = "cat") -> str:
    """표식 파일에 한 줄을 더하고 입력을 `transform`으로 내보내는 filter 명령 스크립트를 만든다.

    Args:
        box: 작업 공간.
        transform: 내용을 내보낼 명령.

    Returns:
        설정 값으로 쓸 스크립트 절대 경로.
    """
    script = box.tools / "filter.sh"
    script.write_text(f'#!/bin/sh\necho ran >> "{box.marker}"\nexec {transform}\n', encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def append_config(box: Sandbox, text: str) -> None:
    """저장소 `.git/config`에 원문 그대로 절을 더한다(`git config`로 쓸 수 없는 이름·대소문자용).

    Args:
        box: 작업 공간.
        text: 더할 설정 원문.
    """
    with (box.project / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write(text)


def make_stat_stale(directory: Path) -> None:
    """작업 트리 파일의 시각을 먼 과거로 돌려 git의 stat 캐시를 낡게 한다(git은 내용을 다시 읽는다).

    Args:
        directory: 저장소 디렉터리(`.git` 아래와 서브모듈의 `.git` 파일은 건드리지 않는다).
    """
    for path in directory.rglob("*"):
        if ".git" not in path.relative_to(directory).parts and path.is_file():
            os.utime(path, (STALE_TIME, STALE_TIME))


def plain(box: Sandbox) -> Expected:
    """드라이버가 없는 대조군이다(낡은 stat만으로는 바뀌지 않는다)."""
    return Expected(commit_tree(box, box.project, {"app.py": APP}))


def tree_attributes(box: Sandbox) -> Expected:
    """추적하는 `.gitattributes`가 clean 드라이버를 붙인다(덮으면 원문 그대로라 깨끗하다)."""
    head = commit_tree(box, box.project, {"app.py": APP, ".gitattributes": "* filter=evil\n"})
    setup_git(box, box.project, "config", "filter.evil.clean", marker_command(box))
    return Expected(head)


def info_attributes(box: Sandbox) -> Expected:
    """추적하지 않는 `.git/info/attributes`만 드라이버를 붙인다(페이로드 전체가 `.git` 안에 있다)."""
    head = commit_tree(box, box.project, {"app.py": APP})
    (box.project / ".git" / "info").mkdir(exist_ok=True)
    (box.project / ".git" / "info" / "attributes").write_text("* filter=evil\n", encoding="utf-8")
    setup_git(box, box.project, "config", "filter.evil.clean", marker_command(box))
    return Expected(head)


def required_process(box: Sandbox) -> Expected:
    """long-running `process` 드라이버에 `required=true`를 둔다(덮지 않으면 한 번 실행되고 status가 실패한다)."""
    head = commit_tree(box, box.project, {"app.py": APP, ".gitattributes": "* filter=evil\n"})
    setup_git(box, box.project, "config", "filter.evil.process", marker_command(box))
    setup_git(box, box.project, "config", "filter.evil.required", "true")
    return Expected(head)


def dotted_name(box: Sandbox) -> Expected:
    """점이 든 드라이버 이름(`a.b`)이다. 이름은 `filter.`과 마지막 `.<키>` 사이 전체다."""
    head = commit_tree(box, box.project, {"app.py": APP, ".gitattributes": "* filter=a.b\n"})
    setup_git(box, box.project, "config", "filter.a.b.clean", marker_command(box))
    return Expected(head)


def included_config(box: Sandbox) -> Expected:
    """`include.path`로 들인 설정 파일이 드라이버를 정의한다."""
    head = commit_tree(box, box.project, {"app.py": APP, ".gitattributes": "* filter=inc\n"})
    included = box.project / ".git" / "extra.cfg"
    included.write_text(f'[filter "inc"]\n\tclean = {marker_command(box)}\n', encoding="utf-8")
    setup_git(box, box.project, "config", "include.path", "extra.cfg")
    return Expected(head)


def mixed_case_name(box: Sandbox) -> Expected:
    """대문자 섹션·키와 대문자 이름(`[Filter "Evil"] Clean`)이다. 이름의 대소문자를 그대로 덮어야 한다."""
    head = commit_tree(box, box.project, {"app.py": APP, ".gitattributes": "* filter=Evil\n"})
    append_config(box, f'[Filter "Evil"]\n\tClean = {marker_command(box)}\n')
    return Expected(head)


def empty_name(box: Sandbox) -> Expected:
    """빈 드라이버 이름(`[filter ""]`, 속성 `filter=`)도 git이 쓰며 `-c filter..clean=`으로 덮인다."""
    head = commit_tree(box, box.project, {"app.py": APP, ".gitattributes": "* filter=\n"})
    append_config(box, f'[filter ""]\n\tclean = {marker_command(box)}\n')
    return Expected(head)


def equals_name(box: Sandbox) -> Expected:
    """`=`가 든 이름은 `-c`가 첫 `=`에서 잘라 덮을 수 없으므로 git 명령을 더 실행하지 않는다."""
    commit_tree(box, box.project, {"app.py": APP, ".gitattributes": "* filter=a=b\n"})
    append_config(box, f'[filter "a=b"]\n\tclean = {marker_command(box)}\n')
    return Expected(None, unverified=True)


def normalizing_filter(box: Sandbox) -> Expected:
    """git-lfs처럼 내용을 바꾸는 clean 드라이버다. 덮으면 원문이 기록된 blob과 달라 바뀐 것으로 보인다(fail-safe)."""
    files = {"app.py": APP, "data.txt": "hello\n", ".gitattributes": "*.txt filter=up\n"}
    commit_tree(box, box.project, files, add_config=("-c", "filter.up.clean=tr a-z A-Z"))
    setup_git(box, box.project, "config", "filter.up.clean", marker_command(box, "tr a-z A-Z"))
    return Expected(None)


def submodule(box: Sandbox) -> Expected:
    """서브모듈 저장소 자신의 설정이 정의한 드라이버다(상위 설정 조회에는 보이지 않는다)."""
    child = box.tools / "child"
    commit_tree(box, child, {"lib.py": "VALUE = 2\n", ".gitattributes": "* filter=evil\n"})
    commit_tree(box, box.project, {"app.py": APP})
    setup_git(box, box.project, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "sub")
    setup_git(box, box.project, "commit", "-q", "-m", "sub")
    gitdir = Path(setup_git(box, box.project / "sub", "rev-parse", "--absolute-git-dir").strip())
    setup_git(box, box.project, "config", "--file", str(gitdir / "config"), "filter.evil.clean", marker_command(box))
    return Expected(None, unverified=True)


#: 실제 git 변형이다(이름 → 저장소를 만들고 정책을 돌려주는 함수).
VARIANTS: dict[str, Callable[[Sandbox], Expected]] = {
    "plain": plain,
    "tree-attributes": tree_attributes,
    "info-attributes": info_attributes,
    "required-process": required_process,
    "dotted-name": dotted_name,
    "include-path": included_config,
    "mixed-case-name": mixed_case_name,
    "empty-name": empty_name,
    "equals-name": equals_name,
    "normalizing-filter": normalizing_filter,
    "submodule": submodule,
}


def documents_without_revision_flag(project: Path) -> dict[str, dict[str, object]]:
    """graph·reach·impact를 `--revision` 없이 실행해 문서를 돌려준다(모두 종료 0이어야 한다).

    Args:
        project: 저장소.

    Returns:
        명령 이름 → 문서.
    """
    common = ["--project", str(project), "--generated-at", GENERATED_AT]
    code, out, err = run_cli(["graph", *common])
    assert code == 0, err
    snapshot: dict[str, object] = json.loads(out)
    nodes = snapshot["nodes"]
    assert isinstance(nodes, list)
    documents = {"graph": snapshot}
    for command in ("reach", "impact"):
        code, out, err = run_cli([command, *common, "--", nodes[0]["id"]])
        assert code == 0, err
        documents[command] = json.loads(out)
    return documents


def unverified_lines(document: dict[str, object]) -> list[str]:
    """문서의 `revision-unverified:` 한계 문구다.

    Args:
        document: 스냅샷 또는 순회 문서.

    Returns:
        문구 목록.
    """
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    return [line for line in limitations if line.startswith(UNVERIFIED)]


@POSIX_ONLY
@pytest.mark.parametrize("build", list(VARIANTS.values()), ids=list(VARIANTS))
def test_revision_reading_runs_no_configured_filter(sandbox: Sandbox, build: Callable[[Sandbox], Expected]) -> None:
    """graph·reach·impact와 git_revision이 분석 대상 설정의 filter 명령을 실행하지 않고 revision 정책을 따른다."""
    expected = build(sandbox)
    make_stat_stale(sandbox.project)
    documents = documents_without_revision_flag(sandbox.project)
    direct = revision_module.git_revision(sandbox.project)
    assert not sandbox.marker.exists(), "a filter command from the analyzed repository's git configuration ran"
    assert direct == expected.revision
    for document in documents.values():
        assert document.get("revision") == expected.revision
        assert len(unverified_lines(document)) == (1 if expected.unverified else 0)


@POSIX_ONLY
def test_submodule_limitation_counts_submodules(sandbox: Sandbox) -> None:
    """서브모듈 한계는 센 서브모듈 수와 `--revision` 안내를 싣는다."""
    submodule(sandbox)
    (line,) = unverified_lines(documents_without_revision_flag(sandbox.project)["reach"])
    assert "1 git submodule(s)" in line
    assert "--revision" in line


@POSIX_ONLY
def test_explicit_revision_runs_no_git(sandbox: Sandbox) -> None:
    """`--revision`을 주면 git을 실행하지 않으므로 서브모듈이 있어도 그 값을 싣고 한계를 달지 않는다."""
    submodule(sandbox)
    make_stat_stale(sandbox.project)
    arguments = ["--project", str(sandbox.project), "--generated-at", GENERATED_AT, "--revision", "given"]
    code, out, err = run_cli(["graph", *arguments])
    assert code == 0, err
    document = json.loads(out)
    assert document["revision"] == "given"
    assert not unverified_lines(document)
    assert not sandbox.marker.exists()


@dataclass
class FakeGit:
    """`subprocess.run` 대역: git 하위 명령마다 정한 (종료 코드, 표준 출력)으로 답하고 받은 인자를 모은다.

    Attributes:
        answers: 하위 명령 → (종료 코드, 표준 출력 바이트).
        calls: 받은 인자 목록(호출 순서).
    """

    answers: dict[str, tuple[int, bytes]]
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, command: Sequence[str], **_keywords: object) -> subprocess.CompletedProcess[bytes]:
        """git 실행을 흉내 낸다.

        Args:
            command: 실행할 인자.
            _keywords: subprocess.run 키워드(쓰지 않는다).

        Returns:
            정한 결과.
        """
        arguments = [str(argument) for argument in command]
        self.calls.append(arguments)
        code, stdout = self.answers[subcommand(arguments)]
        return subprocess.CompletedProcess(arguments, code, stdout=stdout, stderr=b"")

    def subcommands(self) -> list[str]:
        """받은 호출의 하위 명령 이름이다.

        Returns:
            호출 순서의 이름.
        """
        return [subcommand(call) for call in self.calls]


def subcommand(arguments: Sequence[str]) -> str:
    """git 인자에서 실행 파일과 `-c 키=값` 쌍을 건너뛴 하위 명령 이름이다.

    Args:
        arguments: git 인자(실행 파일 포함).

    Returns:
        하위 명령 이름.
    """
    index = 1
    while arguments[index] == "-c":
        index += 2
    return arguments[index]


def config_values(arguments: Sequence[str]) -> list[str]:
    """git 인자 앞쪽의 `-c` 값 목록이다.

    Args:
        arguments: git 인자(실행 파일 포함).

    Returns:
        `키=값` 목록.
    """
    values, index = [], 1
    while arguments[index] == "-c":
        values.append(arguments[index + 1])
        index += 2
    return values


def install_fake_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: dict[str, tuple[int, bytes]]
) -> tuple[Path, FakeGit]:
    """`.git`이 있는 프로젝트를 만들고 revision 판독의 git 실행을 대역으로 바꾼다.

    Args:
        tmp_path: pytest 임시 디렉터리.
        monkeypatch: 대역 설치.
        answers: 대역이 답할 결과.

    Returns:
        (프로젝트, 대역).
    """
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "app.py").write_text(APP, encoding="utf-8")
    fake = FakeGit(answers)
    monkeypatch.setattr(revision_module.subprocess, "run", fake)
    return project, fake


#: 깨끗한 저장소에 대한 정상 답이다(드라이버 없음).
CLEAN_ANSWERS = {
    "config": (1, b""),
    "ls-files": (0, b"100644 " + b"1" * 40 + b" 0\tapp.py\0"),
    "status": (0, b""),
    "rev-parse": (0, OBJECT_ID.encode() + b"\n"),
}


def test_every_git_call_first_lists_and_overrides_filter_drivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """호출마다 바로 앞에서 드라이버를 모아 이름마다 clean·smudge·process를 비우고 required를 끈다(정렬·중복 제거)."""
    listing = (
        b"filter.lfs.clean\ngit-lfs clean -- %f\0filter.a.b.process\nx\0filter.Evil.smudge\ny\0filter.lfs.smudge\nz\0"
    )
    project, fake = install_fake_git(tmp_path, monkeypatch, {**CLEAN_ANSWERS, "config": (0, listing)})
    assert revision_module.git_revision(project) == OBJECT_ID
    assert fake.subcommands() == ["config", "ls-files", "config", "status", "config", "rev-parse"]
    overrides = [
        f"filter.{name}.{key}"
        for name in ("Evil", "a.b", "lfs")
        for key in ("clean=", "smudge=", "process=", "required=false")
    ]
    for call in fake.calls:
        if subcommand(call) == "config":
            assert config_values(call) == BASE_CONFIG
            assert call[1 + 2 * len(BASE_CONFIG) :] == DRIVER_QUERY
        else:
            assert config_values(call) == [*BASE_CONFIG, *overrides]
    assert fake.calls[3][-3:] == ["--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all"]


def test_no_configured_driver_adds_no_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """설정 조회의 종료 1(출력 없음)은 드라이버 없음이다. 덮는 인자 없이 판독한다."""
    project, fake = install_fake_git(tmp_path, monkeypatch, CLEAN_ANSWERS)
    assert revision_module.read_git_revision(project) == revision_module.GitRevision(OBJECT_ID)
    assert all(config_values(call) == BASE_CONFIG for call in fake.calls)


@pytest.mark.parametrize(
    "answer",
    [(128, b""), (1, b"filter.x.clean\nv\0"), (0, b"filterx.clean\nv\0"), (0, b"filter.clean\0")],
    ids=["query-fails", "exit-1-with-output", "foreign-key", "no-driver-name"],
)
def test_unlisted_drivers_stop_before_any_other_git_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: tuple[int, bytes]
) -> None:
    """드라이버를 모으지 못하면(실패, 형식이 다른 출력) 다른 git 명령을 실행하지 않고 revision만 생략한다."""
    project, fake = install_fake_git(tmp_path, monkeypatch, {**CLEAN_ANSWERS, "config": answer})
    assert revision_module.read_git_revision(project) == revision_module.GitRevision(None)
    assert fake.subcommands() == ["config"]


@pytest.mark.parametrize(
    "name",
    [b"a=b", b"caf\xe9", b"t\tx", b"d\x7fl", "n\u0085l".encode()],
    ids=["equals", "not-utf8", "tab", "delete", "next-line"],
)
def test_unneutralizable_driver_name_runs_no_other_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: bytes
) -> None:
    """`=`·제어 문자·UTF-8 아닌 이름은 `-c`로 덮을 수 없어 다른 git 명령을 실행하지 않고 한계로 알린다."""
    listing = b"filter.ok.clean\nx\0filter." + name + b".clean\ny\0"
    project, fake = install_fake_git(tmp_path, monkeypatch, {**CLEAN_ANSWERS, "config": (0, listing)})
    result = revision_module.read_git_revision(project)
    assert result.revision is None
    assert result.limitation is not None
    assert result.limitation.startswith(UNVERIFIED)
    assert name.decode("utf-8", "replace") not in result.limitation
    assert fake.subcommands() == ["config"]


def test_gitlinks_stop_before_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """인덱스에 gitlink가 있으면 status를 실행하지 않는다. 충돌 stage가 여럿인 gitlink는 경로 하나로 센다."""
    gitlink = "160000 " + "2" * 40
    index = (
        f"100644 {'1' * 40} 0\tapp.py\0{gitlink} 0\tsub\0{gitlink} 1\tother\0{gitlink} 2\tother\0{gitlink} 3\tother\0"
    )
    project, fake = install_fake_git(tmp_path, monkeypatch, {**CLEAN_ANSWERS, "ls-files": (0, index.encode())})
    result = revision_module.read_git_revision(project)
    assert result.revision is None
    assert result.limitation is not None
    assert result.limitation.startswith(UNVERIFIED)
    assert "2 git submodule(s)" in result.limitation
    assert fake.subcommands() == ["config", "ls-files"]


@pytest.mark.parametrize(
    ("command", "answer", "subcommands"),
    [
        ("ls-files", (128, b""), ["config", "ls-files"]),
        ("status", (0, b"?? new.py\0"), ["config", "ls-files", "config", "status"]),
        ("rev-parse", (0, b"\xff\n"), ["config", "ls-files", "config", "status", "config", "rev-parse"]),
    ],
    ids=["index-unreadable", "dirty", "not-an-object-id"],
)
def test_failures_and_dirty_trees_omit_revision_quietly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    answer: tuple[int, bytes],
    subcommands: list[str],
) -> None:
    """읽지 못한 인덱스·더러운 트리·커밋 id가 아닌 HEAD는 지금처럼 한계 없이 revision만 생략한다."""
    project, fake = install_fake_git(tmp_path, monkeypatch, {**CLEAN_ANSWERS, command: answer})
    assert revision_module.read_git_revision(project) == revision_module.GitRevision(None)
    assert fake.subcommands() == subcommands


def test_unverified_revision_limitation_reaches_every_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """graph 스냅샷과 reach·impact 문서가 revision을 생략하고 같은 한계를 마지막 문구로 싣는다."""
    listing = b"filter.a=b.clean\nx\0"
    project, _ = install_fake_git(tmp_path, monkeypatch, {**CLEAN_ANSWERS, "config": (0, listing)})
    documents = documents_without_revision_flag(project)
    expected = revision_module.read_git_revision(project).limitation
    for document in documents.values():
        assert "revision" not in document
        limitations = document["limitations"]
        assert isinstance(limitations, list)
        assert limitations[-1] == expected
        assert len(unverified_lines(document)) == 1
