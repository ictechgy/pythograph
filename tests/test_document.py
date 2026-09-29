"""문서 조립(한계 집계·스코프 검증·상한)과 버전 선언 읽기 테스트."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from pythograph.exchange.document import (
    MAX_FACTS,
    DocumentHeader,
    DocumentLimitError,
    build_document,
    format_timestamp,
)
from pythograph.routes.model import Extraction, Location, RouteDecl, RouteShape, ScopeRange
from pythograph.routes.versions import declared_majors, spec_major, version_gap
from pythograph.source.project import Project

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
