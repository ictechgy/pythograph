"""CLI 종료 코드 계약(0/2/64, 1 예약)과 문서 머리·결정성 테스트."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import pythograph
from pythograph.cli import main as cli
from pythograph.graph.roots import RootsError, collect_roots
from tests.conftest import DEEP_SOURCES, FIXTURES, GENERATED_AT, REPOSITORY, SYMLINKS, routes_document, run_cli

#: Django fixture 경로다.
DJANGO = str(FIXTURES / "django" / "drf-shop")


def test_version_matches_pyproject() -> None:
    """`--version`이 pyproject 버전과 같다."""
    match = re.search(
        r'^version = "([^"]+)"', (REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE
    )
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
        ["routes", "--role", "browser", "--project", DJANGO],
        ["routes", "--role", "client", "--project", DJANGO, "--framework", "django"],
        ["routes", "--role", "server", "--project", DJANGO, "--wrappers", "w.json"],
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


def _deep_project(make_project: Callable[[dict[str, str]], Path]) -> Path:
    """정상 Django·클라이언트 모듈 곁에 구문 트리가 너무 깊은 모듈들을 둔 합성 프로젝트를 만든다.

    Args:
        make_project: 프로젝트 생성 함수.

    Returns:
        프로젝트 루트.
    """
    return make_project(
        {
            "manage.py": 'import os\nos.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop.settings")\n',
            "shop/__init__.py": "",
            "shop/settings.py": 'ROOT_URLCONF = "shop.urls"\nINSTALLED_APPS = ["shop"]\n',
            "shop/urls.py": (
                "from django.urls import path\nfrom shop.views import index\n\nurlpatterns = [path('ok/', index)]\n"
            ),
            "shop/views.py": (
                "import requests\n\n\ndef index(request):\n    return requests.get('https://example.com/ok')\n"
            ),
            "shop/models.py": (
                "from django.db import models\n\n\n"
                "class Item(models.Model):\n    name = models.CharField(max_length=10)\n"
            ),
            "shop/calls.py": (
                "import requests\n\n\ndef fetch(base):\n    return requests.get(base" + "[0]" * 250 + ")\n"
            ),
            **{f"shop/{name}": text for name, text in DEEP_SOURCES.items()},
        }
    )


@pytest.mark.parametrize(
    "command",
    [
        ["graph"],
        ["reach", "shop/views.py#index"],
        ["impact", "shop/views.py#index"],
        ["routes", "--role", "server"],
        ["routes", "--role", "client"],
        ["schema"],
    ],
)
def test_deep_modules_do_not_abort_commands(make_project: Callable[[dict[str, str]], Path], command: list[str]) -> None:
    """구문 트리가 너무 깊은 모듈이 있어도 모든 명령이 0이고, 그 모듈은 한계로 남으며 나머지 사실은 그대로다."""
    root = _deep_project(make_project)
    code, out, err = run_cli([*command, "--project", str(root), "--generated-at", GENERATED_AT])
    assert code == 0, err
    document = json.loads(out)
    text = json.dumps(document["limitations"])
    if command[0] in ("graph", "reach", "impact"):
        assert "unparsed-files: " in text
    elif command[-1] == "server":
        assert [fact["channel"] for fact in document["facts"]] == ["/ok/"]
    else:
        assert "could not be analyzed: source nesting too deep to parse" in text
        assert document["facts"]


def test_moderately_deep_modules_stay_analyzed_outside_graph(make_project: Callable[[dict[str, str]], Path]) -> None:
    """graph 상한(128)과 routes·schema 상한(256) 사이 깊이의 모듈은 graph만 빼고 routes·client·schema는 분석한다.

    graph 상한을 모든 명령에 걸었을 때는 urls.py·models.py의 무관한 깊은 식 하나로 server·schema 사실이, views.py의
    130분기 elif 함수 하나로 client 사실이 사라지고 스코프 없는 한계가 붙었다. 상한이 없던 코드도 이 깊이에서는 세
    명령 모두 정상이었다(내부 오류는 graph 계열만 났다).
    """
    unrelated = "\nUNRELATED = " + " + ".join(["1"] * 200) + "\n"
    dispatch = "\n\ndef dispatch(code):\n    if code == 0:\n        return 0\n" + "".join(
        f"    elif code == {index}:\n        return {index}\n" for index in range(1, 130)
    )
    root = make_project(
        {
            "manage.py": 'import os\nos.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop.settings")\n',
            "shop/__init__.py": "",
            "shop/settings.py": 'ROOT_URLCONF = "shop.urls"\nINSTALLED_APPS = ["shop"]\n' + unrelated,
            "shop/urls.py": (
                "from django.urls import path\nfrom shop.views import index\n\nurlpatterns = [path('ok/', index)]\n"
                + unrelated
            ),
            "shop/views.py": (
                "import requests\n\n\ndef index(request):\n    return requests.get('https://example.com/ok')\n"
                + dispatch
            ),
            "shop/models.py": (
                "from django.db import models\n\n\n"
                "class Item(models.Model):\n    name = models.CharField(max_length=10)\n" + unrelated
            ),
        }
    )
    documents = {
        command[-1]: json.loads(run_cli([*command, "--project", str(root), "--generated-at", GENERATED_AT])[1])
        for command in (["graph"], ["routes", "--role", "server"], ["routes", "--role", "client"], ["schema"])
    }
    assert documents["graph"]["statistics"]["unparsedFiles"] == 4
    for name in ("server", "client", "schema"):
        assert not any("too deep" in text for text in documents[name]["limitations"]), name
    assert [fact["channel"] for fact in documents["server"]["facts"]] == ["/ok/"]
    assert [fact["location"]["path"] for fact in documents["client"]["facts"]] == ["shop/views.py"]
    assert "shop/models.py#Item" in {fact["symbol"]["usr"] for fact in documents["schema"]["facts"]}


#: 3.10 파서가 C 스택을 넘기는 20만 단위 사슬 모듈이다. f-string 치환 필드 안의 사슬은 3.10 tokenize에 STRING 토큰
#: 하나로만 보인다.
LONG_CHAINS = {
    "plus": "VALUE = " + "+".join(["1"] * 200_000) + "\n",
    "fstring-plus": 'VALUE = f"{' + "+".join(["a"] * 200_000) + '}"\n',
    "fstring-subscript": 'VALUE = f"{a' + "[0]" * 200_000 + '}"\n',
    "fstring-format-spec": 'WIDTH = 3\nVALUE = f"{a' + ".b" * 200_000 + '!r:>{WIDTH}}"\n',
}


def _run_process(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """CLI를 별도 프로세스로 실행한다(파서가 프로세스를 죽여도 테스트가 살아 있게).

    Args:
        arguments: 명령 인자.

    Returns:
        실행 결과.
    """
    return subprocess.run(
        [sys.executable, "-m", "pythograph", *arguments],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=REPOSITORY,
    )


@pytest.mark.parametrize("shape", sorted(LONG_CHAINS))
def test_long_chains_do_not_crash_the_parser(make_project: Callable[[dict[str, str]], Path], shape: str) -> None:
    """파서가 C 스택을 넘길 만큼 긴 사슬도 프로세스를 죽이지 않고 파싱 실패 한계가 된다.

    3.10의 `ast.parse`는 20만 항 `+` 사슬에서 세그폴트해 종료 코드가 계약에 없는 139였다. f-string 안의 사슬은 사슬
    검사가 세지 않아 그대로 세그폴트했다. 별도 프로세스로 실행한다.
    """
    root = make_project({"pkg/__init__.py": "", "pkg/table.py": LONG_CHAINS[shape]})
    result = _run_process(["graph", "--project", str(root), "--revision", "x"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["statistics"]["unparsedFiles"] == 1


def test_long_chain_in_a_string_annotation_does_not_crash(make_project: Callable[[dict[str, str]], Path]) -> None:
    """매개변수의 문자열 주석이 f-string 사슬이어도 graph·client·schema가 끝나고 그 주석만 모르는 주석이 된다.

    모듈은 파싱되고 주석을 풀 때 `parse_annotation`이 파싱하는데, 3.10에서 사슬 검사가 f-string을 세지 않아 세 명령이
    세그폴트(종료 139)했다.
    """
    annotation = "f'{" + "+".join(["a"] * 200_000) + "}'"
    source = f'import requests\n\n\ndef fetch(session: "{annotation}"):\n    return session.get("/b")\n'
    root = make_project({"pkg/__init__.py": "", "pkg/client.py": source})
    for command in (["graph", "--revision", "x"], ["routes", "--role", "client"], ["schema"]):
        result = _run_process([command[0], "--project", str(root), *command[1:]])
        assert result.returncode == 0, (command, result.returncode, result.stderr)
        document = json.loads(result.stdout)
        if command[0] == "graph":
            assert document["statistics"]["unparsedFiles"] == 0


#: 이름·식별자에 Windows ANSI 코드 페이지(cp1252)에 없는 문자(한글)와 있는 문자(é)를 함께 담은 합성 프로젝트다.
NON_ASCII_PROJECT = {"pkg/__init__.py": "", "pkg/café.py": "def 합계():\n    return 1\n"}

#: `NON_ASCII_PROJECT`의 함수 정점 id다.
NON_ASCII_ROOT = "pkg/café.py#합계"


def _code_page_process(arguments: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """표준 입출력이 UTF-8이 아닌 코드 페이지(cp1252)인 프로세스로 CLI를 실행하고 바이트를 그대로 돌려준다.

    파이썬은 파이프·파일로 돌린 표준 입출력을 로캘 인코딩으로 연다. Windows에서는 ANSI 코드 페이지(영문 러너는
    cp1252)이므로 `PYTHONIOENCODING`으로 같은 조건을 POSIX에서도 만든다. 출력을 디코드하지 않아야 인코딩·줄바꿈
    바이트를 검사할 수 있다.

    Args:
        arguments: 명령 인자.
        stdin: 표준 입력 바이트(없으면 비어 있다).

    Returns:
        실행 결과(stdout·stderr는 바이트).
    """
    return subprocess.run(
        [sys.executable, "-m", "pythograph", *arguments],
        input=stdin or b"",
        capture_output=True,
        check=False,
        cwd=REPOSITORY,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )


def test_output_is_utf8_with_lf_under_a_non_utf8_code_page(make_project: Callable[[dict[str, str]], Path]) -> None:
    """표준 출력의 로캘 인코딩과 무관하게 문서는 UTF-8·LF 바이트다(isthmus는 UTF-8로 읽는다).

    전에는 로캘 인코딩으로 써서 Windows(cp1252)에서 한글 식별자·경로가 있으면 쓰기 실패로 종료 2였고, cp1252에 있는
    문자(é)는 UTF-8이 아닌 바이트가 됐다. Windows 텍스트 모드는 줄바꿈도 CRLF로 바꿔 같은 입력의 출력 바이트가
    플랫폼마다 달랐다(줄바꿈 단언은 Windows CI에서만 수정 전과 후를 가른다).
    """
    root = make_project(NON_ASCII_PROJECT)
    result = _code_page_process(["graph", "--project", str(root), "--generated-at", GENERATED_AT, "--revision", "x"])
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout.decode("utf-8"))
    assert NON_ASCII_ROOT in {node["id"] for node in document["nodes"]}
    assert b"\r" not in result.stdout


def test_roots_from_stdin_are_read_as_utf8_under_a_non_utf8_code_page(
    make_project: Callable[[dict[str, str]], Path],
) -> None:
    """`--roots-from -`도 파일 입력처럼 UTF-8로 읽는다.

    전에는 로캘 인코딩으로 읽어 한글 id가 깨진 채 정점을 찾지 못해 사용법 오류(64)였다.
    """
    root = make_project(NON_ASCII_PROJECT)
    roots = json.dumps([NON_ASCII_ROOT], ensure_ascii=False).encode("utf-8")
    result = _code_page_process(
        ["impact", "--project", str(root), "--generated-at", GENERATED_AT, "--revision", "x", "--roots-from", "-"],
        stdin=roots,
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout.decode("utf-8"))
    assert [root["id"] for root in document["roots"]] == [NON_ASCII_ROOT]
    assert "symbol" in document["roots"][0]


def test_roots_from_stdin_rejects_non_utf8_bytes_in_utf8_mode(
    make_project: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    """UTF-8 모드에서도 `--roots-from -`은 UTF-8이 아닌 바이트를 파일 입력처럼 거부한다(64).

    UTF-8 모드(`PYTHONUTF8=1`)와 C·C.UTF-8 로캘에서 파이썬은 표준 입력을 surrogateescape로 연다. 0.3.1은 그대로 읽어
    usr가 아닌 필드의 깨진 바이트를 서러게이트로 받아 0이었다. 파일 입력은 원래 엄격한 UTF-8이라 64였고, 이제 표준
    입력도 같은 오류 처리(strict)다.
    """
    root = make_project(NON_ASCII_PROJECT)
    document = (
        b'{"format": "bridge-facts", "facts": [{"symbol": {"usr": "' + NON_ASCII_ROOT.encode() + b'"}}], '
        b'"limitations": ["broken \xff byte"]}'
    )
    (tmp_path / "roots.json").write_bytes(document)
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONIOENCODING"}
    environment["PYTHONUTF8"] = "1"
    common = ["impact", "--project", str(root), "--generated-at", GENERATED_AT, "--revision", "x", "--roots-from"]
    for source, stdin in (("-", document), (str(tmp_path / "roots.json"), b"")):
        result = subprocess.run(
            [sys.executable, "-m", "pythograph", *common, source],
            input=stdin,
            capture_output=True,
            check=False,
            cwd=REPOSITORY,
            env=environment,
        )
        assert (result.returncode, result.stdout) == (64, b""), (source, result.stderr)
        assert b"could not be read as UTF-8 text" in result.stderr


def test_stdin_is_reconfigured_only_when_roots_are_read_from_it(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """표준 입력은 `--roots-from -`이 읽을 때만 UTF-8로 다시 연다. 다른 명령은 프로세스 표준 입력을 건드리지 않는다."""
    root = make_project(NON_ASCII_PROJECT)
    untouched = io.TextIOWrapper(io.BytesIO(b""), encoding="cp1252")
    monkeypatch.setattr(sys, "stdin", untouched)
    assert run_cli(["graph", "--project", str(root), "--revision", "x"])[0] == 0
    assert untouched.encoding == "cp1252"
    roots = io.TextIOWrapper(io.BytesIO(json.dumps([NON_ASCII_ROOT], ensure_ascii=False).encode()), encoding="cp1252")
    monkeypatch.setattr(sys, "stdin", roots)
    code, out, err = run_cli(["impact", "--project", str(root), "--revision", "x", "--roots-from", "-"])
    assert code == 0, err
    assert roots.encoding == "utf-8"
    assert [item["id"] for item in json.loads(out)["roots"]] == [NON_ASCII_ROOT]


def test_exchange_stdout_reconfigures_only_streams_that_allow_it() -> None:
    """프로세스 표준 출력은 UTF-8·LF로 바꾸고, 바꿀 수 없는 스트림은 오류 없이 그대로 둔다."""
    raw = io.BytesIO()
    windows_like = io.TextIOWrapper(raw, encoding="cp1252", newline="\r\n")
    assert cli._exchange_stdout(windows_like) is windows_like
    windows_like.write("합계\n")
    windows_like.flush()
    assert raw.getvalue() == "합계\n".encode()
    closed = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    closed.close()
    injected = io.StringIO()
    for stream in (closed, injected):
        assert cli._exchange_stdout(stream) is stream
    closed_stdin = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    closed_stdin.close()
    with pytest.raises(RootsError, match="UTF-8 text"):
        collect_roots([], "-", closed_stdin)


@SYMLINKS
def test_symlinks_are_not_followed(make_project: Callable[[dict[str, str]], Path], tmp_path: Path) -> None:
    """심볼릭 링크는 따라가지 않고 한계로 센다."""
    root = make_project({"app.py": "from flask import Flask\napp = Flask(__name__)\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    limitations = routes_document(root)["limitations"]
    assert any("symbolic links" in text for text in limitations)  # type: ignore[union-attr]


#: isthmus가 식별자에서 거부하는 문자다(parse.ts `controlCharacterPattern`과 짝 없는 서러게이트, 제품 코드와 따로 둔다).
CONTRACT_FORBIDDEN = re.compile("[\u0000-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]")

#: 이름에 계약 금지 문자가 든 모듈 경로다(디렉터리 성분 포함). git은 이런 이름을 허용한다.
FORBIDDEN_NAMES = (
    "shop/ev\x1b[31mil.py",
    "shop/bel\x07.py",
    "shop/nel\x85.py",
    "shop/ls\u2028.py",
    "odd\x07dir/mod.py",
)

#: 금지 이름 모듈의 내용이다. 정상 뷰를 부르고 HTTP·ORM 호출을 담아, 이름이 사실·정점에 실리면 드러난다.
FORBIDDEN_NAME_SOURCE = (
    "import requests\nfrom shop.models import Item\nfrom shop.views import index\n\n\n"
    "def caller(request):\n    Item.objects.filter(name='x')\n"
    "    requests.get('https://example.com/items')\n    return index(request)\n"
)


def _forbidden_name_project(make_project: Callable[[dict[str, str]], Path]) -> Path:
    """정상 Django·클라이언트·모델 모듈 곁에 이름에 계약 금지 문자가 든 모듈을 둔 합성 프로젝트를 만든다.

    Args:
        make_project: 프로젝트 생성 함수.

    Returns:
        프로젝트 루트(파일시스템이 그런 이름을 거부하면 테스트를 건너뛴다).
    """
    root = make_project(
        {
            "manage.py": 'import os\nos.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop.settings")\n',
            "shop/__init__.py": "",
            "shop/settings.py": 'ROOT_URLCONF = "shop.urls"\nINSTALLED_APPS = ["shop"]\n',
            "shop/urls.py": (
                "from django.urls import path\nfrom shop.views import index\n\nurlpatterns = [path('ok/', index)]\n"
            ),
            "shop/views.py": (
                "import requests\n\n\ndef index(request):\n    return requests.get('https://example.com/ok')\n"
            ),
            "shop/models.py": (
                "from django.db import models\n\n\n"
                "class Item(models.Model):\n    name = models.CharField(max_length=10)\n"
            ),
        }
    )
    try:
        for name in FORBIDDEN_NAMES:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(FORBIDDEN_NAME_SOURCE, encoding="utf-8")
    except OSError:
        pytest.skip("the filesystem rejects control characters in file names")
    return root


def _strings(value: object) -> Iterator[str]:
    """JSON 값 안의 모든 문자열(키 포함)을 돌려준다.

    Args:
        value: JSON 값.

    Yields:
        문자열.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


@pytest.mark.parametrize(
    ("command", "prefix"),
    [
        (["graph"], "scan-incomplete:"),
        (["reach", "shop/views.py#index"], "scan-incomplete:"),
        (["impact", "shop/views.py#index"], "scan-incomplete:"),
        (["routes", "--role", "server"], "route-coverage:"),
        (["routes", "--role", "client"], "route-call-coverage:"),
        (["schema"], "unreadable-sources:"),
    ],
)
def test_file_names_with_forbidden_characters_never_reach_the_document(
    make_project: Callable[[dict[str, str]], Path], command: list[str], prefix: str
) -> None:
    """이름에 계약 금지 문자가 든 모듈은 분석하지 않고 한계로 센다. 출력의 어떤 id·경로에도 그 문자가 없다.

    그런 이름이 `symbol.usr`·`location.path`·정점 id에 그대로 실려 isthmus가 문서 전체를 거부하던 회귀다.
    """
    root = _forbidden_name_project(make_project)
    code, out, err = run_cli([*command, "--project", str(root), "--generated-at", GENERATED_AT])
    assert code == 0, err
    document = json.loads(out)
    limitations = document.pop("limitations")
    expected = f"{prefix} {len(FORBIDDEN_NAMES)} Python files have names with characters the exchange format forbids"
    assert any(text.startswith(expected) for text in limitations), limitations
    assert [ascii(text) for text in _strings(document) if CONTRACT_FORBIDDEN.search(text)] == []
    if command == ["graph"]:
        assert document["statistics"]["boundDispatch"]["scanIncomplete"] is True


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
