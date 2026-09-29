"""Flask 추출 규칙 테스트(합성 프로젝트)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.routes.flask.rules import parse_converter_arguments, rule_alternatives
from pythograph.routes.pattern import Unconvertible
from tests.conftest import FIXTURES, fact_rows, facts_for, routes_document

#: 합성 Flask 프로젝트의 버전 선언이다.
REQUIREMENTS = {"requirements.txt": "flask>=3.1,<4\n"}


def test_fixture_is_specificity_with_static_scope() -> None:
    """Flask 문서는 specificity이고 정적 파일 경로를 GET·HEAD 스코프로 좁힌다."""
    document = routes_document(FIXTURES / "flask" / "blog-app")
    assert document["dispatch"] == "specificity"
    assert document["limitationScopes"] == [
        {"limitationIndex": 0, "methods": ["GET", "HEAD"], "templatePrefixes": ["/static"]}
    ]
    assert facts_for(document, "/feed")[0]["trailingSlash"] == "optional"
    assert facts_for(document, "/api/v1/admin")[0]["symbol"]["usr"] == "blog/api.py#admin_home"


def test_module_level_app_loops_and_map_settings(make_project: Callable[[dict[str, str]], Path]) -> None:
    """모듈 수준 앱, 리터럴 목록 반복 등록, url_map 설정·변환기 update, 명시 OPTIONS를 처리한다."""
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask, Blueprint
        from werkzeug.routing import BaseConverter
        class Year(BaseConverter):
            regex = "[0-9]{4}"
        app = Flask(__name__, static_folder=None)
        app.url_map.strict_slashes = False
        app.url_map.merge_slashes = False
        app.url_map.converters.update({"year": Year})
        one = Blueprint("one", __name__, url_prefix="/one")
        two = Blueprint("two", __name__)
        @one.route("/x", methods=["GET", "OPTIONS"])
        def one_x():
            pass
        @two.route("//y/<year:y>")
        def two_y(y):
            pass
        for bp in (one, two):
            app.register_blueprint(bp)
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("GET", "/one/x", False, "app.py#one_x"),
        ("OPTIONS", "/one/x", False, "app.py#one_x"),
        ("GET", "//y/{}", False, "app.py#two_y"),
    }
    assert {fact["trailingSlash"] for fact in document["facts"]} == {"optional"}  # type: ignore[union-attr]
    assert "limitationScopes" not in document


def test_unregistered_blueprint_is_base(make_project: Callable[[dict[str, str]], Path]) -> None:
    """등록을 찾지 못한 블루프린트는 base 앵커와 unresolved-route-prefix다."""
    root = make_project(
        {
            **REQUIREMENTS,
            "bp.py": """
        from flask import Blueprint
        bp = Blueprint("bp", __name__, url_prefix="/ignored")
        @bp.get("/items/<int:i>")
        def item(i):
            pass
    """,
            "app.py": """
        from flask import Flask
        app = Flask(__name__)
        for name in blueprints():
            app.register_blueprint(name)
    """,
        }
    )
    document = routes_document(root)
    fact = facts_for(document, "/items/{}")[0]
    assert fact["pathAnchor"] == "base"
    assert any(text.startswith("unresolved-route-prefix:") for text in document["limitations"])  # type: ignore[union-attr]


def test_view_classes_and_unknown_methods(make_project: Callable[[dict[str, str]], Path]) -> None:
    """View·MethodView 변형과 method를 확정하지 못한 규칙을 처리한다."""
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask
        from flask.views import MethodView, View
        from ext import Mystery
        METHODS = compute()
        class Plain(View):
            def dispatch_request(self):
                pass
        class Declared(MethodView):
            methods = ["PATCH"]
            def patch(self):
                pass
        class Dyn(View):
            methods = compute()
        class Base(MethodView):
            def get(self):
                pass
        class Child(Base):
            def delete(self):
                pass
        def factory():
            app = Flask(__name__, static_url_path="/assets")
            app.add_url_rule("/plain", view_func=Plain.as_view("plain"))
            app.add_url_rule("/declared", "declared", Declared.as_view("declared"))
            app.add_url_rule("/dyn", view_func=Dyn.as_view("dyn"))
            app.add_url_rule("/child", view_func=Child.as_view("child"))
            app.add_url_rule("/mystery", view_func=Mystery.as_view("mystery"))
            app.add_url_rule("/lambda", view_func=lambda: None)
            def local():
                pass
            app.add_url_rule("/local", view_func=local, methods=METHODS)
            app.add_url_rule(rule_name(), view_func=local)
            return app
    """,
        }
    )
    document = routes_document(root)
    rows = fact_rows(document)
    assert ("GET", "/plain", False, "app.py#Plain.dispatch_request") in rows
    assert ("PATCH", "/declared", False, "app.py#Declared.patch") in rows
    assert ("ANY", "/dyn", False, None) in rows
    assert ("GET", "/child", False, "app.py#Child.get") in rows
    assert ("DELETE", "/child", False, "app.py#Child.delete") in rows
    assert ("ANY", "/mystery", False, None) in rows
    assert ("GET", "/lambda", False, None) in rows
    assert ("ANY", "/local", False, "app.py#factory.local") in rows
    assert any(fact["dynamic"] for fact in document["facts"])  # type: ignore[union-attr]
    assert {"limitationIndex": 0, "methods": ["GET", "HEAD"], "templatePrefixes": ["/assets"]} in document[
        "limitationScopes"
    ]  # type: ignore[operator]


def test_routing_extension_and_blueprint_static(make_project: Callable[[dict[str, str]], Path]) -> None:
    """라우트를 따로 등록하는 확장은 한계, 블루프린트 정적 폴더는 접두사 아래 스코프다."""
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask, Blueprint
        import flask_restx
        app = Flask(__name__, static_folder=folder())
        bp = Blueprint("bp", __name__, static_folder="files", url_prefix="/bp")
        nested = Blueprint("nested", __name__)
        bp.register_blueprint(nested, url_prefix=prefix())
        @nested.get("/n")
        def n():
            pass
        app.register_blueprint(bp)
    """,
        }
    )
    document = routes_document(root)
    limitations = document["limitations"]
    assert any("extensions" in text for text in limitations)  # type: ignore[union-attr]
    assert {
        "limitationIndex": limitations.index(
            "framework-provided-routes: Flask serves static files under "  # type: ignore[union-attr]
            "the static URL path"
        ),
        "methods": ["GET", "HEAD"],
        "templatePrefixes": ["/bp/files"],
    } in document["limitationScopes"]  # type: ignore[operator]
    assert facts_for(document, "/n")[0]["pathAnchor"] == "base"


def test_rule_parsing_edge_cases() -> None:
    """규칙 문자열 파싱: 선행 슬래시·잘못된 규칙·변환기 인자·any 상한."""
    with pytest.raises(Unconvertible):
        rule_alternatives("no-slash", True, {})
    with pytest.raises(Unconvertible):
        rule_alternatives("/<bad name>", True, {})
    with pytest.raises(Unconvertible):
        rule_alternatives("/<unknown:x>", True, {})
    with pytest.raises(Unconvertible):
        rule_alternatives("/<custom:x>", True, {"custom": None})
    with pytest.raises(Unconvertible):
        rule_alternatives("/<string(length=x):s>", True, {})
    with pytest.raises(Unconvertible):
        rule_alternatives("/<any(" + ", ".join(f"v{i}" for i in range(20)) + "):s>", True, {})
    assert parse_converter_arguments("1, maxlength=3, name='a', flag=True, none=None, ratio=1.5").keywords == {
        "maxlength": 3,
        "name": "a",
        "flag": True,
        "none": None,
        "ratio": 1.5,
    }
    with pytest.raises(Unconvertible):
        parse_converter_arguments("=")
    assert len(rule_alternatives("/<string(minlength=0):s>/<float(signed=True):f>", True, {})) == 1
