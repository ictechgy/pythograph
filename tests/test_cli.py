"""CLI 종료 코드 계약(0/2/64, 1 예약)과 문서 머리·결정성 테스트."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

import pythograph
from pythograph.cli import main as cli
from tests.conftest import FIXTURES, GENERATED_AT, REPOSITORY, routes_document, run_cli

#: Django fixture 경로다.
DJANGO = str(FIXTURES / "django" / "drf-shop")


def test_version_matches_pyproject() -> None:
    """`--version`이 pyproject 버전과 같다."""
    match = re.search(r'^version = "([^"]+)"', (REPOSITORY / "pyproject.toml").read_text(), re.MULTILINE)
    assert match is not None
    version = match.group(1)
    assert pythograph.__version__ == version
    assert run_cli(["--version"]) == (0, f"{version}\n", "")


def test_help_outputs() -> None:
    """도움말은 성공(0)이다."""
    assert run_cli(["--help"])[1].startswith("Usage: pythograph")
    assert run_cli(["help"])[1].startswith("Usage: pythograph")
    assert run_cli(["help", "routes"])[1].startswith("Usage: pythograph routes")
    assert run_cli(["routes", "--help"])[1].startswith("Usage: pythograph routes")


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["nope"],
        ["help", "nope"],
        ["routes", "--project", DJANGO],
        ["routes", "--role", "client", "--project", DJANGO],
        ["routes", "--role", "server"],
        ["routes", "--role", "server", "--project", DJANGO, "--format", "yaml"],
        ["routes", "--role", "server", "--project", DJANGO, "--framework", "rails"],
        ["routes", "--role", "server", "--project", DJANGO, "--dispatch", "registration-order"],
        ["routes", "--role", "server", "--project", DJANGO, "--service", "a\nb"],
        ["routes", "--role", "server", "--project", DJANGO, "--service", "\udcff"],
        ["routes", "--role", "server", "--project", DJANGO, "--settings", "not a module"],
        ["routes", "--role", "server", "--project", DJANGO, "--generated-at", "yesterday"],
        ["routes", "--role", "server", "--project", DJANGO, "--generated-at", "2026-02-30T00:00:00Z"],
        ["routes", "--role", "server", "--project", DJANGO, "positional"],
        ["routes", "--role", "server", "--role", "server", "--project", DJANGO],
        ["routes", "--role"],
        ["routes", "--role", "--project"],
    ],
)
def test_usage_errors_exit_64(arguments: list[str]) -> None:
    """잘못된 호출은 64다."""
    code, out, err = run_cli(arguments)
    assert code == 64
    assert out == ""
    assert err.startswith("pythograph: ")


def test_missing_project_exits_2(tmp_path: Path) -> None:
    """읽을 수 없는 프로젝트는 2이고 오류 문구에 절대 경로를 싣지 않는다."""
    missing = tmp_path / "missing"
    code, out, err = run_cli(["routes", "--role", "server", "--project", str(missing)])
    assert (code, out) == (2, "")
    assert str(tmp_path) not in err


def test_internal_error_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """예기치 못한 내부 오류도 1이 아니라 2로 끝나고 원문을 싣지 않는다."""

    def explode(*_: object) -> None:
        """항상 실패한다."""
        raise RuntimeError("secret detail")

    monkeypatch.setattr(cli, "extract_routes", explode)
    code, _, err = run_cli(["routes", "--role", "server", "--project", DJANGO])
    assert code == 2
    assert "secret detail" not in err and "RuntimeError" in err


def test_output_limit_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """출력이 isthmus 상한을 넘으면 부분 문서 없이 2다."""
    monkeypatch.setattr("pythograph.exchange.document.MAX_OUTPUT_LENGTH", 10)
    code, out, err = run_cli(["routes", "--role", "server", "--project", DJANGO])
    assert (code, out) == (2, "")
    assert "exceed" in err


def test_both_frameworks_is_usage_error(make_project: Callable[[dict[str, str]], Path]) -> None:
    """Django와 Flask가 모두 있으면 --framework를 요구한다(64). 지정하면 성공한다."""
    root = make_project(
        {
            "manage.py": 'import os\nos.environ.setdefault("DJANGO_SETTINGS_MODULE", "s")\n',
            "s.py": 'ROOT_URLCONF = "u"\n',
            "u.py": "urlpatterns = []\n",
            "flask_app.py": "from flask import Flask\napp = Flask(__name__)\n",
        }
    )
    code, _, err = run_cli(["routes", "--role", "server", "--project", str(root)])
    assert code == 64 and "--framework" in err
    assert routes_document(root, "--framework", "flask")["dispatch"] == "specificity"
    assert routes_document(root, "--framework", "django")["dispatch"] == "registration-order"


def test_empty_project_document(make_project: Callable[[dict[str, str]], Path]) -> None:
    """프레임워크가 없으면 사실 0건 http 문서(roles 유지, dispatch 없음)와 한계다."""
    root = make_project({"lib.py": "x = 1\n", "broken.py": "def (:\n"})
    document = routes_document(root, "--service", "svc", "--include-tests")
    assert document["facts"] == [] and document["target"] == "http" and document["roles"] == ["server"]
    assert "dispatch" not in document
    assert document["service"] == "svc" and document["sourceSets"] == {"tests": "included"}
    assert any("no Django settings module" in text for text in document["limitations"])  # type: ignore[union-attr]


def test_document_envelope_and_determinism() -> None:
    """문서 머리와 바이트 단위 결정성(같은 generatedAt)."""
    first = run_cli(
        ["routes", "--role", "server", "--project", DJANGO, "--generated-at", GENERATED_AT, "--service", "shop"]
    )
    second = run_cli(
        [
            "routes",
            "--role",
            "server",
            f"--project={DJANGO}",
            f"--generated-at={GENERATED_AT}",
            "--service=shop",
            "--format",
            "json",
        ]
    )
    assert first == second
    document = json.loads(first[1])
    assert document["format"] == "bridge-facts" and document["version"] == 1
    assert document["tool"] == {"name": "pythograph", "version": pythograph.__version__}
    assert document["platform"] == "python" and document["generatedAt"] == GENERATED_AT
    assert all(fact["service"] == "shop" for fact in document["facts"])
    assert first[1] == json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_generated_at_defaults_to_now() -> None:
    """--generated-at이 없으면 현재 시각(밀리초 세 자리 UTC)을 쓴다."""
    document = json.loads(run_cli(["routes", "--role", "server", "--project", DJANGO])[1])
    assert len(document["generatedAt"]) == len(GENERATED_AT) and document["generatedAt"].endswith("Z")


def test_symlinks_are_not_followed(make_project: Callable[[dict[str, str]], Path], tmp_path: Path) -> None:
    """심볼릭 링크는 따라가지 않고 한계로 센다."""
    root = make_project({"app.py": "from flask import Flask\napp = Flask(__name__)\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    limitations = routes_document(root)["limitations"]
    assert any("symbolic links" in text for text in limitations)  # type: ignore[union-attr]


class _BrokenStream:
    """쓰기가 항상 실패하는 스트림(닫힌 파이프 흉내)."""

    def write(self, text: str) -> int:
        """항상 실패한다."""
        raise BrokenPipeError(text[:1])

    def flush(self) -> None:
        """아무것도 하지 않는다."""


def test_output_write_failure_exits_2() -> None:
    """출력·오류 스트림 쓰기가 실패해도 1이 아니라 2다(GLM 리뷰 재현: 인코딩 실패·닫힌 파이프)."""
    broken = _BrokenStream()
    assert cli.main(["routes", "--role", "server", "--project", DJANGO], stdout=broken, stderr=broken) == 2  # type: ignore[arg-type]
    assert cli.main(["nope"], stdout=broken, stderr=broken) == 64  # type: ignore[arg-type]
