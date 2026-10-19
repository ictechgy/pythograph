"""문서 조립(한계 집계·스코프 검증·상한)과 버전 선언 읽기 테스트."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from pythograph.exchange.document import (
    MAX_FACTS,
    DocumentEncodingError,
    DocumentHeader,
    DocumentLimitError,
    build_document,
    encode_document,
    format_timestamp,
)
from pythograph.graph.document import encode_snapshot
from pythograph.routes import versions as versions_module
from pythograph.routes.model import Extraction, Location, ParamConstraint, RouteDecl, RouteShape, ScopeRange
from pythograph.routes.versions import _manifest_specs, declared_majors, spec_major, version_gap
from pythograph.source.project import Project
from tests.conftest import REPOSITORY

#: 테스트용 문서 머리다.
HEADER = DocumentHeader("0.0.0", datetime(2026, 1, 1), "/work", None, False)


def test_limitations_aggregate_and_drop_invalid_scopes() -> None:
    """스코프 없는 같은 공백은 개수로 모으고, 계약을 어긴 스코프는 떼어 문서 전체 효과로 남긴다."""
    extraction = Extraction()
    extraction.add_gap("route-coverage:", "{count} things")
    extraction.add_gap("route-coverage:", "{count} things")
    extraction.add_gap("framework-provided-routes:", "admin", ScopeRange(template_prefixes=("/admin",)))
    extraction.add_gap("framework-provided-routes:", "broken", ScopeRange(template_prefixes=("/bad/",)))
    document = build_document(HEADER, extraction)
    assert document["limitations"] == [
        "framework-provided-routes: admin",
        "framework-provided-routes: broken",
        "route-coverage: 2 things",
    ]
    assert document["limitationScopes"] == [{"limitationIndex": 0, "templatePrefixes": ["/admin"]}]


def test_scope_cap_falls_back_to_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """스코프가 상한을 넘으면 모두 떼고 개수로 센다."""
    monkeypatch.setattr("pythograph.exchange.document.MAX_SCOPES", 1)
    extraction = Extraction()
    extraction.add_gap("route-coverage:", "{count} scoped", ScopeRange(templates=("/a",)))
    extraction.add_gap("route-coverage:", "{count} scoped", ScopeRange(templates=("/b",)))
    document = build_document(HEADER, extraction)
    assert document["limitations"] == ["route-coverage: 2 scoped"]
    assert "limitationScopes" not in document


def test_fact_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """사실 수 상한을 넘으면 실패한다."""
    decl = RouteDecl("GET", RouteShape("/a", False), "root", Location("a.py", 1, 1), "a", None)
    monkeypatch.setattr("pythograph.exchange.document.MAX_FACTS", 1)
    extraction = Extraction(decls=[decl, decl])
    with pytest.raises(DocumentLimitError):
        build_document(HEADER, extraction)
    assert MAX_FACTS == 100_000


def test_catch_all_prefix_requires_usr() -> None:
    """catchAllPrefix는 usr가 있을 때만 싣는다(isthmus가 요구)."""
    shape = RouteShape("/a", False, catch_all_prefix=True)
    with_usr = RouteDecl("GET", shape, "root", Location("a.py", 1, 1), "a.py#f", "a.py#f")
    without = RouteDecl("POST", shape, "root", Location("a.py", 1, 1), "f", None)
    facts = build_document(HEADER, Extraction(decls=[with_usr, without]))["facts"]
    assert [fact.get("catchAllPrefix") for fact in facts] == [True, None]  # type: ignore[union-attr]


def test_regex_pattern_with_forbidden_characters_is_omitted() -> None:
    """`pattern`(regex 전용 정보 필드)에 계약이 금지하는 문자가 있으면 그 키만 빼고 세그먼트·종류는 싣는다.

    제어 문자가 든 `pattern`은 isthmus가 문서 전체를 거부했고, 짝 없는 서러게이트는 출력 쓰기부터 실패했다.
    """

    def constraints(pattern: str) -> object:
        """정규식 제약 하나를 가진 선언의 `paramConstraints`를 돌려준다."""
        shape = RouteShape("/items/{}/", False, "strict", (ParamConstraint(1, "regex", pattern),))
        decl = RouteDecl("GET", shape, "root", Location("urls.py", 1, 1), "urls.py#view", "urls.py#view")
        facts = build_document(HEADER, Extraction(decls=[decl]))["facts"]
        assert isinstance(facts, list)
        return facts[0]["paramConstraints"]

    assert constraints("[a-z]+") == [{"kind": "regex", "pattern": "[a-z]+", "segment": 1}]
    for pattern in ("[a-z\x01]+", "[a-z\x85]+", "[a-z\u2028]+", "[a-z\ud800]+"):
        assert constraints(pattern) == [{"kind": "regex", "segment": 1}], ascii(pattern)


def test_unencodable_document_reports_its_cause() -> None:
    """짝 없는 서러게이트가 든 문서는 출력 쓰기의 "출력 대상" 오류 대신 원인을 밝힌 DocumentLimitError(종료 2)다."""
    for encode in (encode_document, encode_snapshot):
        with pytest.raises(DocumentEncodingError, match="unpaired surrogate") as caught:
            encode({"facts": [{"channel": "/\udc00"}]})
        assert isinstance(caught.value, DocumentLimitError)
        assert encode({"channel": "/caf\u00e9\U0001f600"}).endswith("\n")


def test_format_timestamp_converts_to_utc() -> None:
    """시각은 UTC 밀리초 형식이다."""
    assert format_timestamp(datetime(2026, 1, 2, 3, 4, 5, 678901)) == "2026-01-02T03:04:05.678Z"


@pytest.mark.parametrize(
    ("spec", "major"),
    [
        ("==5.2.1", 5),
        ("==5.*", 5),
        ("~=5.2", 5),
        (">=5.2,<6", 5),
        (">=5.2,<6.0", 5),
        (">=5.2, <5.3", 5),
        ("^5.2", 5),
        ("~5.2", 5),
        (">=5.2", None),
        ("<6", None),
        (">=4.2,<6", None),
        ("~=5", None),
        ("==x", None),
        ("<=5.9,>=5.0", 5),
        ("!=5.1,>=5,<6", 5),
    ],
)
def test_spec_major(spec: str, major: int | None) -> None:
    """버전 지정자의 메이저 제한 판정."""
    assert spec_major(spec) == major


def test_declared_majors_from_manifests(make_project: Callable[[dict[str, str]], Path]) -> None:
    """잠금·요구·pyproject·Pipfile.lock에서 메이저를 읽는다."""
    root = make_project(
        {
            "uv.lock": 'version = 1\n\n[[package]]\nname = "django"\nversion = "5.2.17"\n',
            "requirements/base.txt": "Flask[async]>=3.0,<4  # comment\n",
            "pyproject.toml": '[project]\ndependencies = ["djangorestframework>=3.15,<4"]\n'
            '[tool.poetry.dependencies]\nwerkzeug = "^3.1"\n',
            "Pipfile.lock": '{"default": {"django": {"version": "==4.2.1"}}}',
            "requirements-dev.txt": "pytest==8\n",
            "setup.cfg": "[options]\ninstall_requires = django>=5\n",
        }
    )
    majors = declared_majors(Project.open(root))
    assert majors["django"] == {5, 4, None}
    assert majors["flask"] == {3}
    assert majors["djangorestframework"] == {3}
    assert majors["werkzeug"] == {3}
    assert version_gap(majors, "flask", 3, "Flask") is None
    assert version_gap(majors, "django", 5, "Django") is not None
    assert version_gap(majors, "missing", 1, "Missing") is not None
    (root / "Pipfile.lock").write_text("not json")
    (root / "Pipfile").write_text('[packages]\nflask = ">=3.0,<4.0"\n')
    (root / "pyproject.toml").write_text('[tool.poetry.dependencies]\ndjangorestframework = {version = ">=3.15,<4"}\n')
    majors = declared_majors(Project.open(root))
    assert majors["flask"] == {3} and majors["djangorestframework"] == {3}
    assert declared_majors(Project.open(root))["django"] == {5, None}


@pytest.mark.parametrize(
    ("files", "expected", "expected_reads"),
    [
        # 루트가 상한(3)을 넘으면 루트의 요구 파일도 requirements/도 읽지 않는다.
        (
            {"requirements.txt": "django==5.2\n", "requirements/base.txt": "flask==3.0\n"}
            | {f"{name}.txt": "" for name in "abcd"},
            {},
            4,
        ),
        # 루트는 상한 안이고 requirements/만 넘으면 루트의 요구 파일은 그대로 읽는다.
        (
            {"requirements.txt": "django==5.2\n"} | {f"requirements/{name}.txt": "flask==3.0\n" for name in "abcde"},
            {"django": {5}},
            2 + 4,
        ),
    ],
)
def test_requirement_search_enumerates_at_most_the_entry_budget(
    make_project: Callable[[dict[str, str]], Path],
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
    expected: dict[str, set[int | None]],
    expected_reads: int,
) -> None:
    """요구 파일을 찾는 디렉터리 열거도 소스 순회처럼 항목 상한+1개만 꺼내고, 넘친 디렉터리는 일부만 고르지 않는다.

    전에는 루트와 `requirements/`를 끝까지 나열했다. 넘친 디렉터리는 소스 순회도 통째로 빼고 한계로 센다.
    """
    project = Project.open(make_project(files))
    monkeypatch.setattr(versions_module, "MAX_SCANNED_ENTRIES", 3)
    original = os.scandir
    reads = 0

    def counting(entries: Iterator[os.DirEntry[str]]) -> Iterator[os.DirEntry[str]]:
        """꺼낸 항목 수를 센다."""
        nonlocal reads
        for entry in entries:
            reads += 1
            yield entry

    @contextmanager
    def scan(directory: int | Path) -> Iterator[Iterator[os.DirEntry[str]]]:
        """실제 열거를 감싼다."""
        with original(directory) as entries:
            yield counting(entries)

    monkeypatch.setattr(os, "scandir", scan)
    assert declared_majors(project) == expected
    assert reads == expected_reads


#: 아주 긴 extras(40개)다. 길이 상한으로 역추적을 막으면 이런 선언을 놓친다.
_LONG_EXTRAS = "apache-airflow[" + ",".join(f"provider{index}" for index in range(40)) + "]>=2.9\n"


@pytest.mark.parametrize(
    ("path", "text", "expected"),
    [
        (
            "requirements.txt",
            "django>=5.2,<6\nFlask[async]>=3.0,<4  # comment\n",
            [("django", ">=5.2,<6\n"), ("Flask", ">=3.0,<4  ")],
        ),
        (
            "requirements.txt",
            "django [argon2] >= 5.2 , < 6\nhttpx==0.28.1\n",
            [("django", ">= 5.2 , < 6\n"), ("httpx", "==0.28.1\n")],
        ),
        ("requirements.txt", "django>=5.2\r\nflask==3.0\r\n", [("django", ">=5.2\r\n"), ("flask", "==3.0\r\n")]),
        ("requirements.txt", "django\n>=5.2\n", [("django", ">=5.2\n")]),
        ("requirements.txt", "django[\n  argon2,\n  bcrypt\n]>=5.2\n", [("django", ">=5.2\n")]),
        ("requirements.txt", _LONG_EXTRAS, [("apache-airflow", ">=2.9\n")]),
        ("setup.cfg", "[options]\ninstall_requires =\n    django>=5.2,<6\n", [("django", ">=5.2,<6\n")]),
        (
            "pyproject.toml",
            '[project]\ndependencies = ["djangorestframework>=3.15,<4", "httpx[http2]~=0.27"]\n'
            '[tool.poetry.dependencies]\nwerkzeug = "^3.1"\ndjango = {version = ">=5.2, <6", extras = ["bcrypt"]}\n',
            [("djangorestframework", ">=3.15,<4"), ("httpx", "~=0.27"), ("werkzeug", "^3.1"), ("django", ">=5.2, <6")],
        ),
        ("pyproject.toml", 'flask = {\n  version = "~3.1",\n  optional = true\n}\n', [("flask", "~3.1")]),
        (
            "pyproject.toml",
            '\n\n\n  aiohttp = ">=3.12"\n\t\nrequests = { python = ">=3.10", version = "2.*" }\n',
            [("aiohttp", ">=3.12"), ("requests", "2.*")],
        ),
        (
            "Pipfile",
            '[packages]\nflask = ">=3.0,<4.0"\ndjango = {version = "==5.2.1"}\n',
            [("flask", ">=3.0,<4.0"), ("django", "==5.2.1")],
        ),
        (
            "uv.lock",
            'version = 1\n\n[[package]]\nname = "django"\nversion = "5.2.17"\n'
            'source = { registry = "https://pypi.org/simple" }\n\n[[package]]\nname = "flask"\nversion = "3.1.0"\n',
            [("django", "==5.2.17"), ("flask", "==3.1.0")],
        ),
        ("poetry.lock", '[[package]]\r\nname = "django"\r\nversion = "5.2.1"\r\n', [("django", "==5.2.1")]),
        ("pdm.lock", '[[package]]\nname = "flask"\n\nversion = "3.0.3"\n', [("flask", "==3.0.3")]),
    ],
)
def test_manifest_specs_keep_their_results(path: str, text: str, expected: list[tuple[str, str]]) -> None:
    """선언 파일 정규식을 선형으로 바꿔도 (이름, 지정자) 결과는 바이트 단위로 같다.

    수정 전 출력을 그대로 적었다. 줄바꿈을 넘는 공백, CRLF, 여러 줄 extras·인라인 표, 긴 extras, 빈 줄을 사이에 둔
    잠금 블록처럼 `\\s`가 줄을 넘는 경우를 포함한다.
    """
    assert _manifest_specs(path, text) == expected


#: 일치 없이 끝나는 조작 입력을 읽는 별도 프로세스 스크립트다(각 2 MiB 상한 안).
_CRAFTED_MANIFESTS = r"""
import json
from pythograph.routes.versions import _manifest_specs

crafted = [
    ("requirements.txt", "django" + " " * 1_000_000),
    ("requirements.txt", "[a" * 500_000),
    ("uv.lock", 'name = "x"' + "\n" * 1_000_000),
    ("pyproject.toml", "\n" * 1_000_000),
    ("pyproject.toml", "a = {\n" * 200_000),
    ("Pipfile", 'a = ">=1' + " " * 1_000_000 + "x"),
]
print(json.dumps([_manifest_specs(path, text) for path, text in crafted]))
"""


def test_manifest_patterns_stay_linear_on_crafted_input() -> None:
    """조작한 요구·잠금·pyproject·Pipfile도 선형 시간에 읽는다(결과 없음).

    겹치는 `\\s*`와 줄을 넘는 무한 문자 클래스 때문에 수정 전에는 입력 길이의 제곱으로 역추적해, 2 MiB 상한 안의 선언
    파일 하나로 schema·routes가 수 시간 멈췄다. 별도 프로세스의 제한 시간은 멈춤을 실패로 바꾸는 안전장치일 뿐 성능
    측정이 아니다(지금은 1초 안에 끝난다).
    """
    result = subprocess.run(
        [sys.executable, "-c", _CRAFTED_MANIFESTS],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPOSITORY,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [[]] * 6
