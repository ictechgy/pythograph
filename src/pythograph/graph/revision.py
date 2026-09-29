"""분석한 소스의 revision: 프로젝트 루트 git 저장소의 HEAD, 작업 트리가 깨끗할 때만.

작업 트리에 커밋하지 않은 변경·추적하지 않는 파일이 있으면 HEAD는 분석한 소스가 아니므로 싣지 않는다(isthmus trace가
revision이 없는 분석을 `analysis-revision-unknown`으로 드러낸다). 그래프 내용의 신원은 `graphRevision`이 맡는다.

git은 프로젝트 루트에 `.git`이 있을 때만(부모 디렉터리는 찾지 않는다), 선택 잠금·fsmonitor 없이
(`GIT_OPTIONAL_LOCKS=0`, `core.fsmonitor=false` — isthmus capture와 같은 설정) 시간 제한을 두고 실행한다. 분석 대상
프로젝트의 코드는 실행하지 않는다. git이 없거나 실패하면 revision을 싣지 않는다.
"""

from __future__ import annotations

import os
import re
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
        커밋 id, 저장소가 아니거나 더럽거나 읽지 못하면 None.
    """
    if not os.path.lexists(project / ".git"):
        return None
    status = _git(project, "status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=none")
    if status is None or status.strip():
        return None
    head = _git(project, "rev-parse", "--verify", "HEAD")
    if head is None or _OBJECT_ID.fullmatch(head.strip()) is None:
        return None
    return head.strip()


def _git(project: Path, *arguments: str) -> str | None:
    """git 명령 하나를 실행한다.

    Args:
        project: 작업 디렉터리.
        arguments: git 하위 명령과 인자.

    Returns:
        표준 출력, 실패하면 None.
    """
    environment = {key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "SYSTEMROOT")}
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    try:
        completed = subprocess.run(
            ["git", *_GIT_CONFIG, *arguments],
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
