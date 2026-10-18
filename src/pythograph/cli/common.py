"""CLI 공통 도우미: 사용법·입력 오류 예외, 시각·프로젝트 경로 검증.

`main`과 `graph_commands`가 함께 쓴다(순환 import를 피하려고 따로 둔다).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pythograph.source.text import FORBIDDEN_CHARACTERS

#: `--generated-at` 형식이다.
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")


class UsageError(Exception):
    """사용법 오류(64). 메시지는 해결 방향을 담는다."""


class InputError(Exception):
    """입력 오류(2). 메시지는 원인과 해결 방향을 담는다."""


def parse_timestamp(value: str | None) -> datetime | None:
    """`--generated-at` 값을 해석한다.

    Args:
        value: 값.

    Returns:
        UTC 시각 또는 None.

    Raises:
        UsageError: 형식 위반.
    """
    if value is None:
        return None
    if TIMESTAMP.fullmatch(value) is None:
        raise UsageError("--generated-at takes a UTC timestamp such as 2026-01-01T00:00:00.000Z.")
    try:
        return datetime.strptime(value[:-1] + ("" if "." in value else ".0"), "%Y-%m-%dT%H:%M:%S.%f").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise UsageError("--generated-at takes a valid UTC timestamp such as 2026-01-01T00:00:00.000Z.") from error


def resolve_project(argument: str) -> Path:
    """프로젝트 경로를 realpath로 정규화하고 디렉터리인지 확인한다.

    Args:
        argument: `--project` 값.

    Returns:
        realpath.

    Raises:
        InputError: 디렉터리가 아니거나 계약이 금지하는 문자를 담을 때.
    """
    try:
        root = Path(os.path.realpath(argument))
        is_directory = root.is_dir()
    except (OSError, ValueError):
        is_directory = False
    if not is_directory:
        raise InputError("--project does not name a readable directory; pass the project root.")
    if FORBIDDEN_CHARACTERS.search(root.as_posix()):
        raise InputError("the project path contains characters the exchange format forbids; rename or move it.")
    return root
