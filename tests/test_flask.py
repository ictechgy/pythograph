"""Flask 추출 규칙 테스트(합성 프로젝트)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.routes.flask.rules import parse_converter_arguments, rule_alternatives
from pythograph.routes.pattern import Unconvertible
from tests.conftest import FIXTURES, ROUTE_DECL_METHODS, fact_rows, facts_for, routes_document, scoped_limitations

#: 합성 Flask 프로젝트의 버전 선언이다.
REQUIREMENTS = {"requirements.txt": "flask>=3.1,<4\n"}

#: 계약 동사 집합 밖 method를 받는 등록의 한계 문장이다(`{count}` 자리).
OUTSIDE_METHODS = (
    "route-coverage: {count} route registrations accept HTTP methods the exchange format does not allow; "
    "declarations for those methods were not emitted"
)


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


def test_factory_local_blueprint_imports(make_project: Callable[[dict[str, str]], Path]) -> None:
    """앱 팩토리 안의 `from pkg.auth import bp as auth_bp` 뒤 `register_blueprint(auth_bp, url_prefix=...)`를 따른다.

    도그푸딩(앱 팩토리 튜토리얼 구조)에서 함수 안 import를 따라가지 않아 모든 블루프린트 경로가 base 앵커로 나왔다.
    다른 함수의 지역 import는 보이지 않고, import 뒤 대입·반복 변수로 다시 묶은 이름(안쪽 함수 포함)은 풀지 않는다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app/__init__.py": """
        from flask import Flask
        def create_app():
            app = Flask(__name__, static_folder=None)
            from app.auth import bp as auth_bp
            app.register_blueprint(auth_bp, url_prefix="/auth")
            from . import main
            app.register_blueprint(main.bp)
            if True:
                import app.api as api_module
                app.register_blueprint(api_module.bp, url_prefix="/api")
            def later():
                from app.auth import bp as auth_again
                auth_again = make_blueprint()
                app.register_blueprint(auth_again, url_prefix="/rebound")
                main = pick()
                app.register_blueprint(main.bp, url_prefix="/inner")
                for api_module in modules():
                    app.register_blueprint(api_module.bp, url_prefix="/loop")
            return app
        def other(app):
            app.register_blueprint(auth_bp, url_prefix="/wrong")
    """,
            "app/auth.py": """
        from flask import Blueprint
        bp = Blueprint("auth", __name__)
        @bp.route("/login", methods=["GET", "POST"])
        def login():
            pass
    """,
            "app/main.py": """
        from flask import Blueprint
        bp = Blueprint("main", __name__)
        @bp.get("/")
        def index():
            pass
    """,
            "app/api.py": """
        from flask import Blueprint
        bp = Blueprint("api", __name__)
        @bp.delete("/tokens")
        def revoke():
            pass
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("GET", "/auth/login", False, "app/auth.py#login"),
        ("POST", "/auth/login", False, "app/auth.py#login"),
        ("GET", "/", False, "app/main.py#index"),
        ("DELETE", "/api/tokens", False, "app/api.py#revoke"),
    }
    assert {fact["pathAnchor"] for fact in document["facts"]} == {"root"}  # type: ignore[union-attr]


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


def test_glm_review_regressions(make_project: Callable[[dict[str, str]], Path]) -> None:
    """GLM 리뷰 재현: 핸들러 없는 MethodView method, Flask 위치 인자 static_url_path, 반복문 규칙 문자열."""
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask
        from flask.views import MethodView
        class MV(MethodView):
            def get(self):
                pass
        app = Flask("myapp", "/assets")
        app.add_url_rule("/m", view_func=MV.as_view("m"), methods=["GET", "HEAD", "TRACE"])
        for r in ["/a", "/b"]:
            app.add_url_rule(r, r, MV.as_view(r))
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("GET", "/m", False, "app.py#MV.get"),
        ("TRACE", "/m", False, None),
        ("GET", "/a", False, "app.py#MV.get"),
        ("GET", "/b", False, "app.py#MV.get"),
    }
    assert document["limitationScopes"] == [
        {"limitationIndex": 0, "methods": ["GET", "HEAD"], "templatePrefixes": ["/assets"]}
    ]


def test_dynamic_rule_with_unpaired_surrogate_has_no_channel(make_project: Callable[[dict[str, str]], Path]) -> None:
    """짝 없는 서러게이트가 든 dynamic 규칙은 channel 없이 내고, 문서는 UTF-8로 쓸 수 있다.

    원문을 channel에 그대로 실어 출력 쓰기가 UnicodeEncodeError로 실패하고 종료 2·빈 출력으로 끝나던 회귀다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": (
                "from flask import Flask\napp = Flask(__name__, static_folder=None)\n"
                '@app.route("/\\udc00/<path:rest>/tail/<path:more>")\n'
                "def view(rest, more):\n    pass\n"
                '@app.route("/ok")\n'
                "def ok():\n    pass\n"
            ),
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {("GET", "/ok", False, "app.py#ok"), ("GET", None, True, "app.py#view")}
    json.dumps(document, ensure_ascii=False).encode("utf-8")


def test_methods_outside_the_contract_are_not_emitted(make_project: Callable[[dict[str, str]], Path]) -> None:
    """계약 동사 집합 밖 method(`PURGE`·`PROPFIND`)는 decl로 내지 않고 그 규칙 템플릿 스코프의 `route-coverage:`로 센다.

    `methods`의 임의 동사를 대문자로만 바꿔 decl로 내 isthmus가 서버 문서 전체를 입력 오류("Invalid route method")로
    거부하던 회귀다. `ANY`로 바꾸면 그 핸들러가 받지 않는 method까지 거짓 match가 되므로 바꾸지 않는다. 템플릿을
    증명하지 못한 규칙(dynamic·base 앵커)은 스코프 없이 센다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask, Blueprint
        app = Flask(__name__, static_folder=None)
        loose = Blueprint("loose", __name__)
        @app.route("/cache/<key>", methods=["GET", "purge"])
        def cache(key):
            pass
        @app.route("/dav/<any(a, b):part>", methods=["PROPFIND"])
        def dav(part):
            pass
        @app.route(rule_name(), methods=["PURGE"])
        def unknown_rule():
            pass
        @loose.route("/drop", methods=["PURGE"])
        def drop():
            pass
        for bp in blueprints():
            app.register_blueprint(bp)
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {("GET", "/cache/{}", False, "app.py#cache")}
    assert {fact["method"] for fact in document["facts"]} <= ROUTE_DECL_METHODS  # type: ignore[union-attr]
    assert OUTSIDE_METHODS.format(count=2) in document["limitations"]  # type: ignore[operator]
    assert scoped_limitations(document) == [
        (OUTSIDE_METHODS.format(count=1), {"templates": ["/cache/{}"]}),
        (OUTSIDE_METHODS.format(count=1), {"templates": ["/dav/a", "/dav/b"]}),
    ]


def test_expansion_cap_uses_the_capped_prefix(make_project: Callable[[dict[str, str]], Path]) -> None:
    """템플릿 펼침이 16개를 넘은 규칙은 Django와 같이 `route-template-expansion-capped:`, 그 밖의 dynamic은
    `route-coverage:`다.

    같은 펼침 상한(`any()` 곱·`any()` 하나·빈 값 변형)을 Flask만 `route-coverage:`로 내 계약(GRAPH-EXCHANGE
    "정규 경로 템플릿", HTTP-ROUTES.md 한계 표)의 접두사와 어긋나던 회귀다.
    """
    wide = ", ".join(f"v{index}" for index in range(17))
    empties = "/".join(f"<string(minlength=0):{name}>" for name in "abcde")
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": f"""
        from flask import Flask
        app = Flask(__name__, static_folder=None)
        @app.route("/<any(a, b, c, d, e):x>/<any(f, g, h, i, j):y>")
        def grid(x, y):
            pass
        @app.route("/one/<any({wide}):z>")
        def wide(z):
            pass
        @app.route("/e/{empties}")
        def empties(a, b, c, d, e):
            pass
        @app.route("/two/<int:a>-<int:b>")
        def pair(a, b):
            pass
    """,
        }
    )
    document = routes_document(root)
    assert {fact["dynamic"] for fact in document["facts"]} == {True}  # type: ignore[union-attr]
    assert document["limitations"] == [
        "route-coverage: 1 Flask rules are emitted as dynamic: a path segment holds more than one parameter",
        "route-template-expansion-capped: 1 Flask rules are emitted as dynamic: an any() converter expands to more "
        "than 16 templates",
        "route-template-expansion-capped: 1 Flask rules are emitted as dynamic: any() converters expand to more than "
        "16 templates",
        "route-template-expansion-capped: 1 Flask rules are emitted as dynamic: optional parts expand to more than 16 "
        "templates",
    ]
