"""명령줄로 받은 입력 파일(`--wrappers`, `--roots-from`)을 일반 파일일 때만 연다.

분석 대상 소스(`Project`)와 달리 사용자가 경로를 직접 준 파일이라 마지막 링크는 따라간다(모노레포가 공유 선언을 링크로
둘 수 있다). 대신 일반 파일만 받는다. 장치(`/dev/zero`)는 끝이 없어 읽기 상한까지 메모리를 쓰고, 쓰는 쪽이 없는 FIFO는
열기에서 멈춘다. O_NONBLOCK으로 열어 FIFO 열기도 바로 돌아오게 하고, 경로를 먼저 stat하지 않고 연 핸들의 fstat으로
판정하므로 판정과 읽기 사이에 대상이 바뀔 틈이 없다. 읽는 양은 호출한 쪽이 `read(상한 + 1)`로 제한한다.
여러 계층(graph·routes)이 함께 쓰므로 다른 패키지를 import하지 않는 맨 아래 계층에 둔다.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

#: 입력 파일을 여는 플래그다. FIFO 열기가 쓰는 쪽을 기다리지 않게 O_NONBLOCK을(일반 파일 읽기에는 영향이 없다),
#: Windows에서 줄바꿈 변환을 끄도록 O_BINARY를 더한다.
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)


@contextmanager
def open_regular_file(path: Path) -> Iterator[BinaryIO]:
    """경로가 일반 파일일 때만 읽기용 이진 핸들을 연다. 블록을 벗어나면 닫는다.

    Args:
        path: 사용자가 준 파일 경로(마지막 링크는 따라간다).

    Yields:
        버퍼가 있는 이진 읽기 핸들.

    Raises:
        OSError: 열지 못하거나 일반 파일이 아닐 때(디렉터리·장치·FIFO).
    """
    with os.fdopen(os.open(path, _READ_FLAGS), "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        yield handle
