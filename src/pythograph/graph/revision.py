"""분석한 소스의 revision: 프로젝트 루트 git 저장소의 HEAD, 작업 트리가 깨끗할 때만.

작업 트리에 커밋하지 않은 변경·추적하지 않는 파일이 있으면 HEAD는 분석한 소스가 아니므로 싣지 않는다(isthmus trace가
revision이 없는 분석을 `analysis-revision-unknown`으로 드러낸다). 그래프 내용의 신원은 `graphRevision`이 맡는다.

git은 프로젝트 루트에 `.git`이 있을 때만(부모 디렉터리는 찾지 않는다), 선택 잠금·fsmonitor 없이
(`GIT_OPTIONAL_LOCKS=0`, `core.fsmonitor=false` — isthmus capture와 같은 설정) 시간 제한을 두고 실행한다.
git 실행 파일은 PATH의 절대 경로 항목에서만 찾아 그 절대 경로로 실행하고, 실제 위치가 프로젝트 안이거나 현재
디렉터리에 바로 있는 것은 쓰지 않는다(상대 이름 `git`은 Windows에서는 현재 디렉터리의, POSIX에서는 PATH의 빈·상대
항목이 가리키는 프로젝트의 `git`을 먼저 실행할 수 있다). 분석 대상 프로젝트의 코드는 실행하지 않는다. git이 없거나
실패하면 revision을 싣지 않는다.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

#: git 명령 시간 제한(초)이다.
GIT_TIMEOUT_SECONDS = 60

#: 커밋 id 형식(SHA-1 40자, SHA-256 64자)이다.
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

#: 모든 git 호출에 붙이는 설정이다.
_GIT_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false")


def git_revision(project: Path) -> str | None:
    """작업 트리가 깨끗하면 HEAD 커밋 id를 돌려준다.

    Args:
        project: 프로젝트 realpath.

    Returns:
        커밋 id, 저장소가 아니거나 믿을 수 있는 git이 없거나 더럽거나 읽지 못하면 None.
    """
    if not os.path.lexists(project / ".git"):
        return None
    executable = _git_executable(project)
    if executable is None:
        return None
    status = _git(
        executable, project, "status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=none"
    )
    if status is None or status.strip():
        return None
    head = _git(executable, project, "rev-parse", "--verify", "HEAD")
    if head is None or _OBJECT_ID.fullmatch(head.strip()) is None:
        return None
    return head.strip()


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
        git 절대 경로, 찾지 못했거나 현재 디렉터리를 확인하지 못하면 None.
    """
    try:
        current = Path(os.path.realpath(os.getcwd()))
    except OSError:
        # 현재 디렉터리가 지워졌다. 걸러야 할 위치를 모르므로 git을 고르지 않는다(revision 없이 계속한다).
        return None
    root = Path(os.path.realpath(project))
    name = "git.exe" if os.name == "nt" else "git"
    for directory in os.get_exec_path():
        candidate = os.path.join(directory, name)
        if os.path.isabs(directory) and _is_trusted_git(candidate, root, current):
            return candidate
    return None


def _is_trusted_git(candidate: str, root: Path, current: Path) -> bool:
    """후보가 실행 가능한 일반 파일이고, 실제 위치가 프로젝트 밖이며 현재 디렉터리에 바로 있지 않은지 본다.

    링크를 풀어 실제 위치로 판정한다. 현재 디렉터리는 그 디렉터리 자체만 거른다(Windows가 암묵적으로 찾던
    위치다). 하위 트리까지 거르면 현재 디렉터리가 `/`(컨테이너·서비스의 기본값)나 홈일 때 시스템·사용자 git까지 버려
    revision이 사라진다.

    Args:
        candidate: PATH 항목과 실행 파일 이름을 이은 경로.
        root: 프로젝트 realpath.
        current: 현재 디렉터리 realpath.

    Returns:
        실행해도 되면 True.
    """
    try:
        if not stat.S_ISREG(os.stat(candidate).st_mode) or not os.access(candidate, os.X_OK):
            return False
    except (OSError, ValueError):
        # 후보가 없거나 읽을 수 없다(ValueError는 이름의 NUL). 다음 PATH 항목을 본다.
        return False
    resolved = Path(os.path.realpath(candidate))
    return not resolved.is_relative_to(root) and resolved.parent != current


def _git(executable: str, project: Path, *arguments: str) -> str | None:
    """git 명령 하나를 실행한다.

    Args:
        executable: `_git_executable`이 고른 git 절대 경로.
        project: 작업 디렉터리.
        arguments: git 하위 명령과 인자.

    Returns:
        표준 출력, 실패하면 None.
    """
    environment = {key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "SYSTEMROOT")}
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    try:
        completed = subprocess.run(
            [executable, *_GIT_CONFIG, *arguments],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        # git이 없거나 시간이 지났다. revision 없이 계속한다(없다는 것이 신호다).
        return None
    return completed.stdout if completed.returncode == 0 else None
