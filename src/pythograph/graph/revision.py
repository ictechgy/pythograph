"""분석한 소스의 revision: 프로젝트 루트 git 저장소의 HEAD, 작업 트리가 깨끗할 때만.

작업 트리에 커밋하지 않은 변경·추적하지 않는 파일이 있으면 HEAD는 분석한 소스가 아니므로 싣지 않는다(isthmus trace가
revision이 없는 분석을 `analysis-revision-unknown`으로 드러낸다). 그래프 내용의 신원은 `graphRevision`이 맡는다.

git은 프로젝트 루트에 `.git`이 있을 때만(부모 디렉터리는 찾지 않는다), 선택 잠금·fsmonitor 없이
(`GIT_OPTIONAL_LOCKS=0`, `core.fsmonitor=false` — isthmus capture와 같은 설정) 시간 제한을 두고 실행한다.
git 실행 파일은 PATH의 절대 경로 항목에서만 찾아 그 절대 경로로 실행하고, 실제 위치가 프로젝트 안이거나 현재
디렉터리에 바로 있는 것은 쓰지 않는다(상대 이름 `git`은 Windows에서는 현재 디렉터리의, POSIX에서는 PATH의 빈·상대
항목이 가리키는 프로젝트의 `git`을 먼저 실행할 수 있다). 분석 대상 프로젝트의 코드는 실행하지 않는다. git이 없거나
실패하면 revision을 싣지 않는다.

분석 대상 `.git`(과 사용자·시스템) 설정이 정한 명령도 실행하지 않는다(README "Security"). `.git`째 받은 압축본·사본은
`.git/config`가 정한 filter 드라이버를 git이 stat 캐시가 낡은 파일을 다시 읽을 때 실행하기 때문이다(git(1) SECURITY).
- git 호출마다 바로 앞에서 설정된 filter 드라이버(clean·smudge·process)를 모아 빈 명령과 `required=false`로 덮는다.
  빈 명령은 git이 실행하지 않는다. `--attr-source`는 `.git/info/attributes`를 막지 못해 쓰지 않는다. 서명 확인 표시
  (`log.showSignature`)도 끈다. diff·log 계열 호출은 없으므로 `--no-ext-diff`·`--no-textconv`는 해당이 없다.
- 서브모듈 안에서는 git을 띄우지 않는다. status는 `--ignore-submodules=all`로 돌고, 인덱스에 서브모듈(gitlink)이 있으면
  status를 실행하지 않는다. 서브모듈 작업 트리가 깨끗한지는 그 저장소 자신의 설정을 따라야만 알 수 있고, pythograph는
  서브모듈 디렉터리의 소스도 분석하므로 그때는 revision을 싣지 않는다.
- 덮을 수 없는 드라이버 이름(`=`·제어 문자·UTF-8 아님)이 있으면 다른 git 명령을 실행하지 않는다(fail-closed).
- 부분 clone(promisor 원격)의 빠진 객체를 받는 지연 fetch를 띄우지 않는다. 그 fetch는 저장소 설정의 원격 명령
  (`remote.<이름>.uploadpack`·`core.sshCommand` 등)을 실행한다. 모든 호출에 `GIT_NO_LAZY_FETCH=1`을 주고, 같은 설정
  조회에 promisor 키가 보이면 `--no-lazy-fetch`도 붙인다. 둘 다 git 2.45부터 있고, 옛 git은 환경 변수를 조용히
  무시하지만 모르는 옵션에는 명령을 시작하지 않고 실패한다(fail-closed). promisor 키가 없는 저장소에는 옵션을 붙이지
  않으므로 어느 git에서나 지금처럼 읽는다.
일부러 보지 않거나 막아서 revision을 싣지 못한 세 경우(서브모듈, 덮을 수 없는 드라이버 이름, 지연 fetch 없이 읽지 못한
부분 clone)는 `revision-unverified:` 한계 문구를 함께 돌려준다(막은 사실을 조용히 넘기지 않는다). git-lfs처럼 filter로
정규화되던 파일은 stat 캐시가 낡으면 바뀐 것으로 보여 revision을 싣지 않는다(fail-safe). 나머지 설정(`core.worktree`
등)은 git이 무엇을 보고할지를 정하고 위에서 닫은 경로 밖의 설정은 여전히 믿으므로, revision은 `.git`만큼만 믿을 수
있다.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from pythograph.source.text import FORBIDDEN_CHARACTERS

#: git 명령 시간 제한(초)이다.
GIT_TIMEOUT_SECONDS = 60

#: 커밋 id 형식(SHA-1 40자, SHA-256 64자)이다.
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

#: 모든 git 호출(드라이버 조회 포함)에 붙이는 설정이다. fsmonitor 훅과 untracked cache 기록을 끄고(isthmus capture와
#: 같은 설정), log 계열이 서명을 확인하려고 gpg를 실행하지 않게 한다(분석 대상 설정의 명령을 막는 계열 공통 설정).
_GIT_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "log.showSignature=false")

#: 분석용 git 호출마다 먼저 모으는 설정 키다. git이 작업 트리 파일을 읽고 쓸 때 실행하는 filter 드라이버 명령 키와,
#: promisor 원격(부분 clone)을 만드는 키다. 섹션·변수 이름은 소문자로 맞춘 키에 대어 본다.
_SETTING_KEYS = (
    r"^(filter\..*\.(clean|smudge|process)|extensions\.partialclone|remote\.(.*\.)?(promisor|partialclonefilter))$"
)

#: promisor 원격을 만드는 키다. git(promisor-remote.c)은 `extensions.partialClone`, 참인 `remote.<이름>.promisor`, 값과
#: 무관하게 `remote.<이름>.partialCloneFilter`가 있는 원격을 promisor로 쓰고 이름 없는 `remote.promisor`도 받는다. 값은
#: 보지 않는다. 거짓 양성(`promisor = false`)은 지연 fetch를 한 번 더 끌 뿐이다.
_PROMISOR_KEY = re.compile(rb"extensions\.partialclone|remote\.(?:.*\.)?(?:promisor|partialclonefilter)", re.DOTALL)

#: promisor 원격이 있는 저장소의 분석용 호출에 붙이는 전역 옵션이다(git 2.45+, `GIT_NO_LAZY_FETCH=1`과 같다). 이 옵션을
#: 모르는 옛 git은 하위 명령을 시작하기 전에 사용 오류(129)로 끝나므로 환경 변수를 무시하고 지연 fetch를 띄우지 못한다.
_NO_LAZY_FETCH = "--no-lazy-fetch"

#: 드라이버마다 덮는 키와 값이다. 빈 명령은 git이 실행하지 않고(filter 없이 원문을 쓴다), required=false라야
#: "filter 실패"로 끝나지 않는다.
_NEUTRALIZED_KEYS = (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false"))

#: 서브모듈(gitlink) 인덱스 항목의 mode다.
_GITLINK_MODE = b"160000"

#: 덮을 수 없는 드라이버 이름 때문에 git 명령을 실행하지 않았을 때의 한계 문구다. 이름 원문은 싣지 않는다(신뢰하지
#: 않는 설정 값이고 제어 문자가 섞일 수 있다).
FILTER_DRIVER_LIMITATION = (
    "revision-unverified: the git configuration defines a filter driver whose name contains '=', a control "
    "character, or bytes that are not UTF-8, which git cannot be told to skip, so pythograph ran no git command "
    "on the work tree (git config --get-regexp '^filter\\.' lists the drivers; rename or remove that one); "
    "pass --revision to record the analyzed revision"
)

#: promisor 원격이 있는 저장소에서 지연 fetch를 끈 git 명령이 실패했을 때의 한계 문구다(빠진 객체가 필요했거나 git이
#: `--no-lazy-fetch`를 모른다).
LAZY_FETCH_LIMITATION = (
    "revision-unverified: the repository has a promisor remote (a partial clone), so pythograph ran git with lazy "
    "fetching turned off (--no-lazy-fetch, git 2.45 or later), because fetching missing objects would run commands "
    "from the repository's configuration, and git could not read the work tree that way; pass --revision to record "
    "the analyzed revision"
)


@dataclass(frozen=True)
class GitRevision:
    """revision 판독 결과.

    Attributes:
        revision: 작업 트리가 깨끗할 때의 HEAD 커밋 id, 아니면 None.
        limitation: 작업 트리를 일부러 보지 않아 revision을 싣지 못한 이유(`revision-unverified:` 한계 문구).
            git이 없거나 실패했거나 더러운 트리는 None이다(revision이 없다는 것 자체가 소비자에게 가는 신호다).
    """

    revision: str | None
    limitation: str | None = None


@dataclass(frozen=True)
class _RepositorySettings:
    """분석용 git 호출 하나 바로 앞에서 모은 설정 요약.

    Attributes:
        drivers: 덮을 filter 드라이버 이름(정렬·중복 제거).
        promisor: promisor 원격을 만드는 키가 있는지(부분 clone).
    """

    drivers: tuple[str, ...]
    promisor: bool


class _UnneutralizableDriverError(Exception):
    """`-c`로 덮을 수 없는 filter 드라이버 이름이 설정에 있다(git 명령을 더 실행하지 않는다)."""


class _LazyFetchRefusedError(Exception):
    """promisor 원격이 있는 저장소에서 지연 fetch를 끈 git 명령이 실패했다(git 명령을 더 실행하지 않는다)."""


def git_revision(project: Path) -> str | None:
    """작업 트리가 깨끗하면 HEAD 커밋 id를 돌려준다(`read_git_revision`의 revision만).

    Args:
        project: 프로젝트 realpath.

    Returns:
        커밋 id, 저장소가 아니거나 믿을 수 있는 git이 없거나 더럽거나 읽지 못하거나 확인하지 않았으면 None.
    """
    return read_git_revision(project).revision


def read_git_revision(project: Path) -> GitRevision:
    """작업 트리가 깨끗하면 HEAD 커밋 id를, 일부러 확인하지 않았으면 그 이유를 돌려준다.

    Args:
        project: 프로젝트 realpath.

    Returns:
        판독 결과. 저장소가 아니거나 믿을 수 있는 git이 없거나 더럽거나 읽지 못하면 revision이 None이다.
    """
    if not os.path.lexists(project / ".git"):
        return GitRevision(None)
    executable = _git_executable(project)
    if executable is None:
        return GitRevision(None)
    try:
        return _clean_head(executable, project)
    except _UnneutralizableDriverError:
        return GitRevision(None, FILTER_DRIVER_LIMITATION)
    except _LazyFetchRefusedError:
        return GitRevision(None, LAZY_FETCH_LIMITATION)


def _clean_head(executable: str, project: Path) -> GitRevision:
    """서브모듈이 없으면 작업 트리가 깨끗할 때의 HEAD를 읽는다.

    서브모듈이 있으면 status를 실행하지 않는다. 그 작업 트리를 보려면 git이 서브모듈 저장소 자신의 설정(filter 등)을
    따라야 하는데, 그 설정은 상위 저장소의 드라이버 조회에 보이지 않아 덮을 수 없다. 서브모듈을 건너뛴 status는
    서브모듈 안의 변경을 못 본 채 깨끗하다고 주장하므로 쓰지 않는다(pythograph는 서브모듈 디렉터리도 분석한다).

    Args:
        executable: git 절대 경로.
        project: 프로젝트 realpath.

    Returns:
        판독 결과.

    Raises:
        _UnneutralizableDriverError: 덮을 수 없는 드라이버 이름이 있다.
        _LazyFetchRefusedError: 부분 clone에서 지연 fetch를 끈 git 명령이 실패했다.
    """
    index = _git(executable, project, "ls-files", "--stage", "-z")
    if index is None:
        return GitRevision(None)
    submodules = _count_gitlinks(index)
    if submodules:
        return GitRevision(None, _submodule_limitation(submodules))
    return GitRevision(_head_if_clean(executable, project))


def _head_if_clean(executable: str, project: Path) -> str | None:
    """작업 트리가 깨끗하면(서브모듈은 보지 않는다) HEAD 커밋 id를 읽는다.

    Args:
        executable: git 절대 경로.
        project: 프로젝트 realpath.

    Returns:
        커밋 id, 더럽거나 읽지 못하면 None.

    Raises:
        _UnneutralizableDriverError: 덮을 수 없는 드라이버 이름이 있다.
        _LazyFetchRefusedError: 부분 clone에서 지연 fetch를 끈 git 명령이 실패했다.
    """
    status = _git(
        executable, project, "status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all"
    )
    if status is None or status.strip():
        return None
    head = _git(executable, project, "rev-parse", "--verify", "HEAD")
    text = "" if head is None else head.decode("ascii", "replace").strip()
    return text if _OBJECT_ID.fullmatch(text) else None


def _count_gitlinks(index: bytes) -> int:
    """`git ls-files --stage -z` 출력에서 서브모듈(gitlink, mode 160000) 경로 수를 센다.

    항목은 `<mode> <object> <stage>\\t<경로>\\0`이다. 충돌 중인 gitlink는 stage마다 한 항목씩 나오므로 경로로 센다.

    Args:
        index: ls-files 출력.

    Returns:
        gitlink 경로 수.
    """
    entries = (entry.partition(b"\t") for entry in index.split(b"\0"))
    return len({path for meta, tab, path in entries if tab and meta.split(b" ", 1)[0] == _GITLINK_MODE})


def _submodule_limitation(count: int) -> str:
    """서브모듈 때문에 revision을 싣지 않은 이유다.

    Args:
        count: 서브모듈 수.

    Returns:
        한계 문구.
    """
    return (
        f"revision-unverified: the work tree has {count} git submodule(s); pythograph does not run git inside them "
        "(that would follow their repositories' configuration, such as filter commands), so it cannot tell whether "
        "the analyzed source is HEAD; pass --revision to record the analyzed revision"
    )


def _git_executable(project: Path) -> str | None:
    """실행할 git을 PATH의 절대 경로 항목에서 찾는다.

    상대 이름 `git`을 실행하면 Windows CreateProcess는 PATH보다 부모 프로세스의 현재 디렉터리를 먼저 찾고,
    POSIX는 PATH의 빈 항목·상대 항목을 작업 디렉터리(프로젝트) 기준으로 풀어 분석 대상 저장소에 든 `git`을 실행할 수
    있다. 그래서 절대 경로 항목만 보고 찾은 절대 경로로 실행한다. PATH가 없으면 subprocess처럼 `os.defpath`를 본다.
    `shutil.which`는 Windows에서 현재 디렉터리를 먼저 볼 수 있어 쓰지 않는다. Windows에서는 cmd.exe를 거치는
    `.cmd`·`.bat`을 빼고 `git.exe`만 찾는다.

    Args:
        project: 프로젝트 realpath.

    Returns:
        git 절대 경로, 찾지 못했거나 현재 디렉터리·프로젝트를 확인하지 못하면 None.
    """
    try:
        current_info = os.stat(os.getcwd())
        project_info = os.stat(project)
    except OSError:
        # 현재 디렉터리가 지워졌거나 프로젝트를 다시 확인하지 못했다. 걸러야 할 위치를 모르므로 git을 고르지 않는다
        # (revision 없이 계속한다).
        return None
    name = "git.exe" if os.name == "nt" else "git"
    for directory in os.get_exec_path():
        candidate = os.path.join(directory, name)
        if os.path.isabs(directory) and _is_trusted_git(candidate, project_info, current_info):
            return candidate
    return None


def _is_trusted_git(candidate: str, project_info: os.stat_result, current_info: os.stat_result) -> bool:
    """후보가 실행 가능한 일반 파일이고, 실제 위치가 프로젝트 밖이며 현재 디렉터리에 바로 있지 않은지 본다.

    링크를 풀어 실제 위치로 판정하고, 디렉터리는 경로 문자열이 아니라 파일 신원(장치·inode)으로 비교한다.
    대소문자·유니코드 정규화를 무시하는 파일 시스템(macOS 기본 APFS 등)에서는 철자만 다른 PATH 항목이
    같은 디렉터리를 가리켜 문자열 비교를 빠져나가기 때문이다. 현재 디렉터리는 그 디렉터리 자체만 거른다(Windows가
    암묵적으로 찾던 위치다). 하위 트리까지 거르면 현재 디렉터리가 `/`(컨테이너·서비스의 기본값)나 홈일 때
    시스템·사용자 git까지 버려 revision이 사라진다.

    Args:
        candidate: PATH 항목과 실행 파일 이름을 이은 경로.
        project_info: 프로젝트 디렉터리의 stat.
        current_info: 현재 디렉터리의 stat.

    Returns:
        실행해도 되면 True.
    """
    try:
        if not stat.S_ISREG(os.stat(candidate).st_mode) or not os.access(candidate, os.X_OK):
            return False
        directory = os.path.dirname(os.path.realpath(candidate))
        if os.path.samestat(os.stat(directory), current_info):
            return False
        return not _lies_within(directory, project_info)
    except (OSError, ValueError):
        # 후보가 없거나 읽을 수 없다(ValueError는 이름의 NUL). 실제 위치를 확인하지 못한 후보도 쓰지 않는다.
        # 다음 PATH 항목을 본다.
        return False


def _lies_within(directory: str, project_info: os.stat_result) -> bool:
    """디렉터리 자신이나 그 조상 중 하나가 프로젝트 디렉터리인지 파일 신원으로 본다.

    Args:
        directory: 링크를 푼 절대 경로.
        project_info: 프로젝트 디렉터리의 stat.

    Returns:
        프로젝트 안이면 True.

    Raises:
        OSError: 조상 디렉터리를 확인하지 못했다(호출자는 그 후보를 쓰지 않는다).
    """
    while not os.path.samestat(os.stat(directory), project_info):
        parent = os.path.dirname(directory)
        if parent == directory:
            return False
        directory = parent
    return True


def _git(executable: str, project: Path, *arguments: str, output_limit: int | None = None) -> bytes | None:
    """분석 대상 설정의 filter 드라이버를 덮고 지연 fetch를 끈 채 git 명령 하나를 실행한다.

    바로 앞에서 설정을 다시 모아(`_repository_settings`) 그때의 드라이버를 덮고, promisor 원격이 보이면
    `--no-lazy-fetch`를 붙인다. 모든 분석용 호출이 이 함수를 지난다. 호출마다 따로 막으면 한쪽만 설정의 명령을
    실행하는 어긋남이 생긴다. 설정을 모으지 못하면 그 명령은 실행하지 않는다(fail-closed). promisor 저장소에서 명령이
    실패하면 지연 fetch를 끈 탓일 수 있으므로(빠진 객체, 옵션을 모르는 옛 git) 조용히 넘기지 않고 알린다.

    Args:
        executable: `_git_executable`이 고른 git 절대 경로.
        project: 작업 디렉터리.
        arguments: git 하위 명령과 인자.
        output_limit: 지정하면 설정 조회와 본 명령의 stdout·stderr 합을 읽는 중 제한한다.

    Returns:
        표준 출력 바이트, 설정을 모으지 못했거나 git이 실패하면 None.

    Raises:
        _UnneutralizableDriverError: 덮을 수 없는 드라이버 이름이 있다.
        _LazyFetchRefusedError: promisor 원격이 있는 저장소에서 명령이 실패했다.
    """
    settings = (
        _repository_settings(executable, project)
        if output_limit is None
        else _repository_settings(executable, project, output_limit=output_limit)
    )
    if settings is None:
        return None
    isolated = (*_isolating_arguments(settings), *arguments)
    completed = (
        _run_git(executable, project, *isolated)
        if output_limit is None
        else _run_git(executable, project, *isolated, output_limit=output_limit)
    )
    if completed is None:
        return None
    if completed.returncode == 0:
        return completed.stdout
    if settings.promisor:
        raise _LazyFetchRefusedError
    return None


def _repository_settings(
    executable: str, project: Path, *, output_limit: int | None = None
) -> _RepositorySettings | None:
    """git이 이 저장소에서 읽는 설정 전체에서 filter 드라이버 이름과 promisor 원격 여부를 모은다.

    시스템·사용자·저장소·worktree 설정과 include를 모두 본다. 설정 조회는 명령을 실행하지 않고 객체도 읽지 않는다.
    `git config`의 종료 1과 빈 출력은 "일치 없음"이다.

    Args:
        executable: git 절대 경로.
        project: 작업 디렉터리.
        output_limit: 설정 원문도 읽는 중 제한할 합산 출력 예산이다.

    Returns:
        설정 요약, 조회가 실패했거나 출력 형식이 다르면 None.

    Raises:
        _UnneutralizableDriverError: 덮을 수 없는 이름이 있다.
    """
    arguments = ("config", "-z", "--includes", "--get-regexp", _SETTING_KEYS)
    completed = (
        _run_git(executable, project, *arguments)
        if output_limit is None
        else _run_git(executable, project, *arguments, output_limit=output_limit)
    )
    if completed is None:
        return None
    if completed.returncode == 1 and not completed.stdout:
        return _RepositorySettings((), promisor=False)
    if completed.returncode != 0:
        return None
    return _parse_settings(completed.stdout)


def _parse_settings(output: bytes) -> _RepositorySettings | None:
    """`git config -z --get-regexp` 출력에서 드라이버 이름과 promisor 키를 뽑는다.

    레코드는 `키\\n값\\0`(값 없는 키는 `키\\0`)이다. 키의 섹션·변수 이름은 소문자로, subsection(드라이버·원격 이름)은
    그대로 나온다. 드라이버 이름은 `filter.`과 마지막 `.<키>` 사이 전체다. 점이 들어갈 수 있고 빈 이름(`[filter ""]`)도
    git이 쓴다.

    Args:
        output: 조회 출력.

    Returns:
        설정 요약, 형식이 어긋나면 None(조회 정규식에 맞는 키만 올 수 있으므로 출력을 믿지 않는다).

    Raises:
        _UnneutralizableDriverError: 덮을 수 없는 이름이 있다.
    """
    names: set[str] = set()
    promisor = False
    for record in output.split(b"\0"):
        if not record:
            continue
        key = record.partition(b"\n")[0]
        if _PROMISOR_KEY.fullmatch(key):
            promisor = True
        elif key.startswith(b"filter.") and key.count(b".") >= 2:
            names.add(_neutralizable_name(key[len(b"filter.") : key.rindex(b".")]))
        else:
            return None
    return _RepositorySettings(tuple(sorted(names)), promisor)


def _neutralizable_name(raw: bytes) -> str:
    """드라이버 이름을 `-c filter.<이름>.<키>=`로 덮을 수 있으면 문자열로 돌려준다.

    git은 `-c` 값을 첫 `=`에서 키와 값으로 나누므로 `=`가 든 이름은 다른 키가 된다. UTF-8이 아닌 이름은 Windows에서
    인자가 UTF-16으로 바뀌며 다른 이름이 되고, 제어 문자 이름은 정상 설정에서 나오지 않으므로 함께 거부한다
    (gartograph와 같은 판단). `GIT_CONFIG_COUNT`·`GIT_CONFIG_KEY_<n>` 환경 변수(git 2.31+)로 넘기는 대안은 옛 git이
    조용히 무시해 드라이버가 실행되므로(fail-open) 쓰지 않는다.

    Args:
        raw: 드라이버 이름 바이트.

    Returns:
        이름.

    Raises:
        _UnneutralizableDriverError: 덮을 수 없는 이름.
    """
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _UnneutralizableDriverError from error
    if "=" in name or FORBIDDEN_CHARACTERS.search(name):
        raise _UnneutralizableDriverError
    return name


def _isolating_arguments(settings: _RepositorySettings) -> list[str]:
    """분석용 호출의 하위 명령 앞에 붙일 전역 인자다: 드라이버를 덮는 `-c`와, promisor 원격이 있으면 `--no-lazy-fetch`.

    Args:
        settings: 바로 앞에서 모은 설정 요약.

    Returns:
        git 인자.
    """
    lazy_fetch = [_NO_LAZY_FETCH] if settings.promisor else []
    return [*_neutralizing_arguments(settings.drivers), *lazy_fetch]


def _neutralizing_arguments(names: tuple[str, ...]) -> list[str]:
    """드라이버 이름마다 명령 세 키를 빈 값으로, required를 false로 덮는 `-c` 인자다.

    Args:
        names: 드라이버 이름.

    Returns:
        git 인자.
    """
    return [
        argument
        for name in names
        for key, value in _NEUTRALIZED_KEYS
        for argument in ("-c", f"filter.{name}.{key}={value}")
    ]


def _run_git(
    executable: str, project: Path, *arguments: str, output_limit: int | None = None
) -> subprocess.CompletedProcess[bytes] | None:
    """공통 설정과 줄인 환경으로 git 프로세스 하나를 실행한다.

    `GIT_NO_LAZY_FETCH=1`은 git 2.45부터 모든 호출(과 그 자식)의 지연 fetch를 끈다. promisor 키 판정이 놓친 원격이
    있어도 막기 위한 이중 장치이고, 옛 git은 이 변수를 무시하므로 혼자 쓰지 않는다(`_NO_LAZY_FETCH`).

    Args:
        executable: git 절대 경로.
        project: 작업 디렉터리.
        arguments: git 인자(덮는 `-c`·`--no-lazy-fetch`와 하위 명령).
        output_limit: 지정하면 stdout·stderr 합산 바이트를 읽는 중 제한한다(초과는 실패).

    Returns:
        끝난 프로세스, git을 실행하지 못했거나 시간이 지나면 None.
    """
    environment = {key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "SYSTEMROOT")}
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", "LC_ALL": "C"})
    command = [executable, *_GIT_CONFIG, *arguments]
    if output_limit is not None:
        return _run_git_bounded(command, project, environment, output_limit)
    try:
        return subprocess.run(
            command,
            cwd=project,
            env=environment,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        # git이 없거나 시간이 지났다. revision 없이 계속한다(없다는 것이 신호다).
        return None


def _run_git_bounded(
    command: list[str], project: Path, environment: dict[str, str], limit: int
) -> subprocess.CompletedProcess[bytes] | None:
    """작은 청크를 동시에 배수하며 출력 예산·시간을 지킨다. 직접 소유한 자식만 정리한다.

    reader는 프로세스에 신호를 보내거나 wait하지 않는다. 부모만 Popen의 아직 수거하지 않은 자식을 kill/wait하므로
    수거 뒤 재사용된 PID·프로세스 그룹에 신호를 보내지 않는다. stderr도 같은 예산에 포함하고 어느 실패도 성공 출력으로
    바꾸지 않는다. 기존 revision 조회 호출의 호환 경로와 별도로 change 캡처가 명시적으로 요청한다.
    """
    if limit < 0:
        return None
    chunks: list[list[bytes]] = [[], []]
    lock = threading.Lock()
    failed = threading.Event()
    drained = threading.Event()
    used = 0
    finished = 0

    def drain(stream: BinaryIO, index: int) -> None:
        """파이프 한 개를 읽되 합산 예산을 넘는 청크는 보관하지 않는다."""
        nonlocal used, finished
        try:
            with stream:
                while data := os.read(stream.fileno(), min(8192, limit + 1)):
                    with lock:
                        if failed.is_set() or used + len(data) > limit:
                            failed.set()
                            drained.set()
                            return
                        chunks[index].append(data)
                        used += len(data)
        except (OSError, ValueError, MemoryError):
            failed.set()
            drained.set()
        finally:
            with lock:
                finished += 1
                if finished == 2:
                    drained.set()

    try:
        process = subprocess.Popen(
            command,
            cwd=project,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, UnicodeError):
        return None
    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=drain, args=(stream, index), daemon=True)
        for index, stream in enumerate((process.stdout, process.stderr))
    ]
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    try:
        for reader in readers:
            reader.start()
        if not drained.wait(max(0, deadline - time.monotonic())) or failed.is_set():
            return None
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return subprocess.CompletedProcess(command, code, b"".join(chunks[0]), b"".join(chunks[1]))
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        # poll은 Popen이 소유한 자식만 수거한다. 살아 있거나 아직 수거 전인 같은 자식에만 kill한다.
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            failed.set()
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=1)
