"""README 두 언어판의 빠른 시작 명령이 실제로 실행되고 약속한 사실을 내는지 본다."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from tests.conftest import REPOSITORY, run_cli

#: 언어판마다 빠른 시작 절의 제목이다.
QUICK_START_HEADINGS = {"README.md": "## Quick start", "README.ko.md": "## 빠른 시작"}


def quick_start_block(readme: str) -> str:
    """빠른 시작 절의 첫 `sh` 코드 블록을 돌려준다."""
    text = (REPOSITORY / readme).read_text(encoding="utf-8")
    section = text.split(QUICK_START_HEADINGS[readme] + "\n", 1)[1].split("\n## ", 1)[0]
    found = re.search(r"```sh\n(.*?)```", section, re.DOTALL)
    assert found is not None, f"{readme} quick start has no sh block"
    return found.group(1)


def materialize(block: str, workdir: Path) -> list[str]:
    """블록의 `mkdir`·heredoc을 작업 디렉터리에 재현하고 마지막 pythograph 명령의 인자를 돌려준다.

    셸을 띄우지 않는다. 블록이 이 형식(`mkdir -p`, `cat > 파일 <<'끝표시'`, `pythograph …`)을 벗어나면 실패해 README와
    테스트를 함께 고치게 한다.
    """
    lines = block.splitlines()
    command: list[str] | None = None
    index = 0
    while index < len(lines):
        words = shlex.split(lines[index])
        if words[:2] == ["mkdir", "-p"]:
            for directory in words[2:]:
                (workdir / directory).mkdir(parents=True, exist_ok=True)
        elif words[:2] == ["cat", ">"] and len(words) == 4 and words[3].startswith("<<"):
            marker = words[3][2:]
            end = lines.index(marker, index + 1)
            (workdir / words[2]).write_text("\n".join(lines[index + 1 : end]) + "\n", encoding="utf-8")
            index = end
        elif words[:1] == ["pythograph"]:
            command = words[1:]
        else:
            raise AssertionError(f"unexpected quick start line: {lines[index]!r}")
        index += 1
    assert command is not None, "quick start runs no pythograph command"
    return command


@pytest.mark.parametrize("readme", sorted(QUICK_START_HEADINGS))
def test_readme_quick_start_emits_the_declared_route(readme: str, tmp_path: Path) -> None:
    """설치 직후 따라 하는 명령이 사용 오류 없이 합성 Flask 경로 하나를 낸다."""
    arguments = materialize(quick_start_block(readme), tmp_path)
    project = arguments.index("--project") + 1
    arguments[project] = str(tmp_path / arguments[project])
    code, text, err = run_cli(arguments)
    assert code == 0, err
    document = json.loads(text)
    routes = [(fact["kind"], fact["method"], fact["channel"], fact["location"]["path"]) for fact in document["facts"]]
    assert routes == [("route-decl", "GET", "/items", "app.py")]


def test_readme_quick_starts_match_across_languages() -> None:
    """두 언어판이 같은 명령을 안내한다(설명 산문만 번역한다)."""
    assert quick_start_block("README.md") == quick_start_block("README.ko.md")
