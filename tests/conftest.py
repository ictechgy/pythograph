"""테스트 공용 도우미: CLI 실행, 합성 프로젝트 만들기, 문서 요약."""

from __future__ import annotations

import ast
import io
import json
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from pythograph.cli.main import main

#: 저장소 루트다.
REPOSITORY = Path(__file__).resolve().parent.parent

#: 합성 fixture 루트다.
FIXTURES = REPOSITORY / "fixtures"

#: 결정적 출력을 위한 고정 시각이다.
GENERATED_AT = "2026-01-01T00:00:00.000Z"

#: route-decl `method`에 올 수 있는 값이다(GRAPH-EXCHANGE: 계약 동사 여덟 개와 route-decl 전용 `ANY`). 구현 상수를
#: 가져오지 않고 계약에서 옮겨 적어, 구현이 집합을 넓히거나 거르지 않으면 테스트가 알린다.
ROUTE_DECL_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "ANY"})

#: 파싱은 되지만 구문 트리가 깊어 재귀 분석기가 파이썬 재귀 한도를 넘기던 모양별 모듈 소스다(파일 이름 → 내용).
#: 긴 `+` 사슬, 함수 안 메서드·첨자 사슬, 긴 elif 분기다.
DEEP_SOURCES = {
    "plus.py": "VALUE = " + " + ".join(["1"] * 500) + "\n",
    "method.py": "def build(query):\n    return query" + ".filter(1)" * 150 + "\n",
    "subscript.py": "def pick(data):\n    return data" + "[0]" * 250 + "\n",
    "elifs.py": "def dispatch(code):\n    if code == 0:\n        return 0\n"
    + "".join(f"    elif code == {index}:\n        return {index}\n" for index in range(1, 500)),
}


def class_lattice(depth: int, bottom_bases: str = "", bottom_body: str = "pass", back_edge: bool = False) -> str:
    """다이아몬드 상속 격자 모듈 소스를 만든다.

    바닥은 `L0a`·`L0b`이고, 단계 k의 두 클래스(`Lka`·`Lkb`)는 단계 k-1의 두 클래스를 모두 상속한다. 꼭대기 `Top`은 단계
    depth의 두 클래스를 상속하므로 바닥은 `Top`에서 상속 깊이 depth + 1에 있다. 경로마다 사슬을 다시 펴면 `Top` 하나에
    2^(depth+1)번 재귀한다.

    Args:
        depth: 바닥 위 단계 수.
        bottom_bases: 바닥 두 클래스의 기반 식(빈 문자열이면 없음).
        bottom_body: 바닥 `L0a`의 본문(한 줄).
        back_edge: 바닥 `L0a`가 꼭대기 `Top`도 상속하게 해 정적 상속 순환을 만든다.

    Returns:
        모듈 소스.
    """
    first = ", ".join(base for base in (bottom_bases, "Top" if back_edge else "") if base)
    lines = [f"class L0a({first}):" if first else "class L0a:", f"    {bottom_body}", ""]
    lines += [f"class L0b({bottom_bases}):" if bottom_bases else "class L0b:", "    pass", ""]
    for level in range(1, depth + 1):
        for side in "ab":
            lines += [f"class L{level}{side}(L{level - 1}a, L{level - 1}b):", "    pass", ""]
    lines += [f"class Top(L{depth}a, L{depth}b):", "    pass", ""]
    return "\n".join(lines)


class CountingValues(dict[Any, Any]):
    """`values()`로 돈 항목 수를 세는 사전. 모듈마다 카탈로그 전체를 다시 도는지 확인하는 테스트가 쓴다."""

    def __init__(self, items: dict[Any, Any]) -> None:
        """항목을 담고 계수를 0으로 둔다.

        Args:
            items: 감쌀 사전.
        """
        super().__init__(items)
        self.visits = 0

    def values(self) -> Iterator[Any]:  # type: ignore[override]
        """돈 항목 수를 더하며 값을 차례로 돌려준다."""
        for value in super().values():
            self.visits += 1
            yield value


def tree_depth(source: str) -> int:
    """구문 트리 최대 깊이를 명시적 스택으로 잰다(모듈 노드가 1, 문맥·연산자 잎 노드 포함).

    Args:
        source: 모듈 소스.

    Returns:
        깊이.
    """
    deepest = 0
    pending: list[tuple[ast.AST, int]] = [(ast.parse(source), 1)]
    while pending:
        node, depth = pending.pop()
        deepest = max(deepest, depth)
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return deepest


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


def schema_document(project: Path, *extra: str) -> dict[str, object]:
    """schema 명령을 실행해 문서를 돌려준다. 실패하면 테스트를 실패시킨다.

    Args:
        project: 프로젝트 경로.
        extra: 추가 인자.

    Returns:
        문서.
    """
    code, out, err = run_cli(["schema", "--project", str(project), "--generated-at", GENERATED_AT, *extra])
    assert code == 0, err
    document: dict[str, object] = json.loads(out)
    return document


def relation_rows(document: dict[str, object]) -> set[tuple[object, ...]]:
    """relation-use 사실을 (channel, method, dynamic, usr) 튜플 집합으로 요약한다.

    Args:
        document: 문서.

    Returns:
        요약 집합.
    """
    facts = document["facts"]
    assert isinstance(facts, list)
    return {(fact["channel"], fact.get("method"), fact["dynamic"], fact.get("symbol", {}).get("usr")) for fact in facts}


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


def scoped_limitations(document: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    """스코프 있는 limitation을 (문장, `limitationIndex`를 뺀 스코프) 목록으로 요약한다.

    Args:
        document: 문서.

    Returns:
        문서의 `limitationScopes` 순서대로 놓은 요약 목록.
    """
    limitations = document["limitations"]
    scopes = document.get("limitationScopes", [])
    assert isinstance(limitations, list) and isinstance(scopes, list)
    summary: list[tuple[str, dict[str, object]]] = []
    for scope in scopes:
        fields = {key: value for key, value in scope.items() if key != "limitationIndex"}
        summary.append((limitations[scope["limitationIndex"]], fields))
    return summary


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
