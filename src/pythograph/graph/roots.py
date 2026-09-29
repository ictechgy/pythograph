"""reach·impact의 root id 입력: 위치 인자와 `--roots-from <file|->`.

`--roots-from`은 JSON 문자열 배열 또는 bridge-facts 문서(사실의 `symbol.usr`)를 받는다(cartograph·kartograph와 같은
입력). 위치 인자 다음에 파일의 id를 잇고, 중복은 처음 나온 순서로 하나만 남긴다(그 순서가 `reached[].roots` 인덱스의
뜻이다). 제어 문자가 든 id, 빈 id, 10,000개를 넘는 root는 사용법 오류다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TextIO

#: 계약의 root 상한이다.
MAX_ROOTS = 10_000

#: `--roots-from` 입력의 최대 크기(바이트·문자)다.
MAX_ROOTS_INPUT = 16 * 1024 * 1024

#: id에 올 수 없는 문자다(제어 문자, U+2028/2029, 짝 없는 서로게이트).
FORBIDDEN_CHARACTERS = re.compile("[\u0000-\u001f\u007f-\u009f  \ud800-\udfff]")


class RootsError(Exception):
    """root 입력 오류(사용법 오류 64). 메시지는 원인과 해결 방향을 담고 입력 원문은 싣지 않는다."""


def collect_roots(positional: list[str], roots_from: str | None, stdin: TextIO) -> tuple[str, ...]:
    """root id를 모아 검증한다.

    Args:
        positional: 위치 인자 id.
        roots_from: `--roots-from` 값(없으면 None).
        stdin: 표준 입력(`-`).

    Returns:
        중복 없는 id(처음 나온 순서).

    Raises:
        RootsError: 입력이 잘못됐을 때.
    """
    ids = list(positional)
    if roots_from is not None:
        ids.extend(parse_roots_text(_read(roots_from, stdin)))
    unique = tuple(dict.fromkeys(ids))
    if not unique:
        raise RootsError("give at least one symbol id as an argument or through --roots-from.")
    if any(not item or FORBIDDEN_CHARACTERS.search(item) for item in unique):
        raise RootsError("symbol ids must be non-empty and free of control characters.")
    if len(unique) > MAX_ROOTS:
        raise RootsError(f"at most {MAX_ROOTS} roots are allowed per run; split the roots into several runs.")
    return unique


def _read(argument: str, stdin: TextIO) -> str:
    """`--roots-from` 입력을 읽는다.

    Args:
        argument: 파일 경로 또는 `-`.
        stdin: 표준 입력.

    Returns:
        텍스트.

    Raises:
        RootsError: 읽지 못하거나 너무 클 때.
    """
    try:
        if argument == "-":
            text = stdin.read(MAX_ROOTS_INPUT + 1)
        else:
            with Path(argument).open(encoding="utf-8") as handle:
                text = handle.read(MAX_ROOTS_INPUT + 1)
    except (OSError, UnicodeError, ValueError) as error:
        raise RootsError(
            "--roots-from could not be read as UTF-8 text; pass a readable file or - for stdin."
        ) from error
    if len(text) > MAX_ROOTS_INPUT:
        raise RootsError("--roots-from is larger than 16 MiB; split the roots into several runs.")
    return text


def parse_roots_text(text: str) -> list[str]:
    """JSON 문자열 배열 또는 bridge-facts 문서에서 id를 꺼낸다.

    Args:
        text: JSON 텍스트.

    Returns:
        id 목록.

    Raises:
        RootsError: 형식이 맞지 않을 때.
    """
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise RootsError("--roots-from must be a JSON array of strings or a bridge-facts document.") from error
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    if isinstance(value, dict) and value.get("format") == "bridge-facts" and isinstance(value.get("facts"), list):
        return _fact_usrs(value["facts"])
    raise RootsError("--roots-from must be a JSON array of strings or a bridge-facts document.")


def _fact_usrs(facts: list[object]) -> list[str]:
    """bridge-facts 사실의 `symbol.usr`를 모은다(없는 사실은 건너뛴다).

    Args:
        facts: 사실 목록.

    Returns:
        usr 목록.
    """
    usrs: list[str] = []
    for fact in facts:
        symbol = fact.get("symbol") if isinstance(fact, dict) else None
        usr = symbol.get("usr") if isinstance(symbol, dict) else None
        if isinstance(usr, str):
            usrs.append(usr)
    return usrs
