"""테스트 공용 도우미: CLI 실행, 합성 프로젝트 만들기, 문서 요약."""

from __future__ import annotations

import io
import json
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.cli.main import main

#: 저장소 루트다.
REPOSITORY = Path(__file__).resolve().parent.parent

#: 합성 fixture 루트다.
FIXTURES = REPOSITORY / "fixtures"

#: 결정적 출력을 위한 고정 시각이다.
GENERATED_AT = "2026-01-01T00:00:00.000Z"


def run_cli(arguments: list[str]) -> tuple[int, str, str]:
    """CLI를 프로세스 안에서 실행한다.

    Args:
        arguments: 인자 목록.

    Returns:
        (종료 코드, 표준 출력, 표준 오류).
    """
    out, err = io.StringIO(), io.StringIO()
    code = main(arguments, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def routes_document(project: Path, *extra: str) -> dict[str, object]:
    """routes 명령을 실행해 문서를 돌려준다. 실패하면 테스트를 실패시킨다.

    Args:
        project: 프로젝트 경로.
        extra: 추가 인자.

    Returns:
        문서.
    """
    code, out, err = run_cli(
        ["routes", "--role", "server", "--project", str(project), "--generated-at", GENERATED_AT, *extra]
    )
    assert code == 0, err
    document: dict[str, object] = json.loads(out)
    return document


def fact_rows(document: dict[str, object]) -> set[tuple[object, ...]]:
    """사실을 (method, channel, dynamic, usr) 튜플 집합으로 요약한다.

    Args:
        document: 문서.

    Returns:
        요약 집합.
    """
    facts = document["facts"]
    assert isinstance(facts, list)
    return {(fact["method"], fact["channel"], fact["dynamic"], fact["symbol"].get("usr")) for fact in facts}


def facts_for(document: dict[str, object], channel: str) -> list[dict[str, object]]:
    """channel이 같은 사실을 돌려준다.

    Args:
        document: 문서.
        channel: channel.

    Returns:
        사실 목록.
    """
    facts = document["facts"]
    assert isinstance(facts, list)
    return [fact for fact in facts if fact["channel"] == channel]


@pytest.fixture
def make_project(tmp_path: Path) -> Callable[[dict[str, str]], Path]:
    """합성 프로젝트를 만드는 함수를 돌려준다.

    Args:
        tmp_path: pytest 임시 디렉터리.

    Returns:
        (상대 경로 → 내용) 사전을 받아 프로젝트 루트를 돌려주는 함수.
    """

    def build(files: dict[str, str]) -> Path:
        """파일을 쓴다(내용은 들여쓰기를 걷어 낸다)."""
        root = tmp_path / "project"
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        root.mkdir(exist_ok=True)
        return root

    return build
