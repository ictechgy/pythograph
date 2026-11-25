"""Flask 추출 규칙 테스트(합성 프로젝트)."""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.exchange import document as document_module
from pythograph.routes.flask import class_body as class_body_module
from pythograph.routes.flask import extract as flask_extract
from pythograph.routes.flask import rules as rules_module
from pythograph.routes.flask import view_methods as view_methods_module
from pythograph.routes.flask.rules import ConverterArguments, parse_converter_arguments, rule_alternatives
from pythograph.routes.pattern import MAX_ROUTE_SOURCE_LENGTH, Literal, Skeleton, Unconvertible
from pythograph.source.symbols import ProjectSymbol
from tests.conftest import (
    FIXTURES,
    GENERATED_AT,
    ROUTE_DECL_METHODS,
    class_lattice,
    fact_rows,
    facts_for,
    routes_document,
    run_cli,
    scoped_limitations,
)

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


def test_rebound_rule_constants_are_dynamic(make_project: Callable[[dict[str, str]], Path]) -> None:
    """누적 대입(`PREFIX += "/v2"`)이나 조건부로 다시 묶은 모듈 이름은 규칙·접두사 상수로 쓰지 않는다.

    마지막 무조건 대입을 상수로 써서 실제 `/api/v2/users`를 `/api/users`로 한계 없이 냈다. 한 번만 대입한 상수는
    그대로 정적이다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        import os
        from flask import Blueprint, Flask
        app = Flask(__name__, static_folder=None)
        PREFIX = "/api"
        PREFIX += "/v2"
        MOUNT = "/shop"
        if os.environ.get("TENANT"):
            MOUNT = "/tenant"
        STABLE = "/stable"
        shop = Blueprint("shop", __name__)
        @app.route(PREFIX + "/users")
        def users():
            pass
        @app.route(STABLE + "/items")
        def items():
            pass
        @shop.route("/orders")
        def orders():
            pass
        app.register_blueprint(shop, url_prefix=MOUNT)
    """,
        }
    )
    facts = routes_document(root)["facts"]
    assert isinstance(facts, list)
    assert {(fact["channel"], fact["dynamic"]) for fact in facts if fact["symbol"]["usr"] != "app.py#users"} == {
        ("/stable/items", False),
        ("/orders", False),
    }
    assert [fact["dynamic"] for fact in facts if fact["symbol"]["usr"] == "app.py#users"] == [True]
    anchors = {fact["symbol"]["usr"]: fact["pathAnchor"] for fact in facts}
    assert anchors["app.py#orders"] == "base"


#: 다시 묶이는 모듈 이름이 가리키는 블루프린트·앱의 등록 한계 문장이다(`{count}` 자리).
REBOUND_REGISTRATIONS = (
    "unresolved-route-prefix: {count} Flask blueprint registrations go through a module name that may refer to "
    "another app or blueprint (bound more than once or conditionally), so their routes are emitted with a base anchor"
)

#: 다시 묶이는 모듈 이름이 가리키는 앱·블루프린트에 단 규칙의 한계 문장이다(`{count}` 자리).
REBOUND_RULES = (
    "unresolved-route-prefix: {count} Flask rules are registered through a module name that may refer to another app "
    "or blueprint (bound more than once or conditionally), so they are emitted with a base anchor"
)


def test_rebound_app_and_blueprint_names_are_base_anchored(make_project: Callable[[dict[str, str]], Path]) -> None:
    """다시 묶이는 모듈 이름(두 번 이상·조건부 대입)이 가리키는 블루프린트·앱의 규칙과 등록은 `base` 앵커와
    `unresolved-route-prefix:`로 낸다. 그 이름을 import해 쓰는 다른 모듈도 같다.

    객체 사전이 이름의 마지막 대입만 보고 묶음은 보지 않아, `bp = Blueprint(…, url_prefix=PREFIX)` 뒤
    `if V2: bp = Blueprint(…, url_prefix="/v2")`인 `@bp.route("/users")`를 한계 없이 정적 `/v2/users`(root)로 냈다.
    실행 조건에 따라 실제 경로는 `/api/users`다. 한 번만 묶인 블루프린트·앱과 리터럴 목록 반복으로 등록한 블루프린트는
    그대로 root다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        import os
        from flask import Blueprint, Flask
        from shop import shop
        V2 = os.environ.get("V2")
        PREFIX = "/api"
        bp = Blueprint("api", __name__, url_prefix=PREFIX)
        if V2:
            bp = Blueprint("api", __name__, url_prefix="/v2")
        stable = Blueprint("stable", __name__, url_prefix="/stable")
        @bp.route("/users")
        def users():
            pass
        @stable.route("/items")
        def items():
            pass
        app = Flask(__name__, static_folder=None)
        for blueprint in (bp, stable):
            app.register_blueprint(blueprint)
        app.register_blueprint(shop, url_prefix="/shop")
    """,
            "views.py": """
        from app import bp
        @bp.route("/extra")
        def extra():
            pass
    """,
            "other.py": """
        from flask import Flask
        from app import bp
        other = Flask(__name__, static_folder=None)
        other.register_blueprint(bp, url_prefix="/other")
    """,
            "shop.py": """
        from flask import Blueprint
        shop = Blueprint("shop", __name__)
        @shop.get("/orders")
        def orders():
            pass
    """,
            "legacy.py": """
        import os
        from flask import Flask
        from shop import shop
        legacy = Flask(__name__, static_folder=None)
        @legacy.route("/ping")
        def ping():
            pass
        def health():
            pass
        legacy.add_url_rule("/health", view_func=health)
        legacy.register_blueprint(shop, url_prefix="/old")
        if os.environ.get("ALT"):
            legacy = Flask("alt", static_folder=None)
    """,
        }
    )
    document = routes_document(root)
    facts = document["facts"]
    assert isinstance(facts, list)
    rows = {(fact["channel"], fact["pathAnchor"], fact["symbol"]["usr"]) for fact in facts}
    assert rows == {
        ("/users", "base", "app.py#users"),
        ("/extra", "base", "views.py#extra"),
        ("/ping", "base", "legacy.py#ping"),
        ("/health", "base", "legacy.py#health"),
        ("/orders", "base", "shop.py#orders"),
        ("/stable/items", "root", "app.py#items"),
        ("/shop/orders", "root", "shop.py#orders"),
    }
    assert not any(fact["dynamic"] for fact in facts)
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert REBOUND_REGISTRATIONS.format(count=3) in limitations
    assert REBOUND_RULES.format(count=2) in limitations
    # 다시 묶인 앱의 정적 파일 경로는 다른 묶음의 값을 알 수 없어 스코프 없는 한계다.
    assert "framework-provided-routes: Flask serves static files under the static URL path" in limitations
    assert "limitationScopes" not in document


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


#: method를 확정하지 못한 Flask 뷰의 한계 문장이다(`{count}` 자리).
UNKNOWN_VIEW_METHODS = (
    "route-coverage: {count} Flask views accept methods that could not be determined statically and were emitted "
    "as ANY or as their known methods"
)


def test_unresolved_view_without_methods_is_any(make_project: Callable[[dict[str, str]], Path]) -> None:
    """`methods` 없이 등록한 모르는 뷰 식은 GET이 아니라 `ANY`다.

    `add_url_rule`은 `methods`가 없으면 `view_func.methods`를 먼저 보고 없을 때만 GET을 쓴다. 다시 묶은 변수의
    `Hook.as_view("hook")`(post만 정의), 감싼 `login_required(Put.as_view("put"))`, 외부 함수, 모르는 기반 클래스 뷰를
    GET으로 내고 "ANY로 냈다"는 한계를 붙여, Flask가 405로 거부하는 GET 사실을 만들고 받는 POST·PUT은 빠뜨렸다.
    그 자리에서 만든 람다와 None은 속성이 없어 GET이 맞고 한계도 세지 않는다. `methods`를 준 등록은 그 동사 그대로다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask
        from flask.views import MethodView, View
        from ext import ExtMixin, login_required, webhook
        app = Flask(__name__, static_folder=None)
        class Hook(MethodView):
            def post(self):
                pass
        class Put(MethodView):
            def put(self):
                pass
        class Mixed(ExtMixin, View):
            def dispatch_request(self):
                pass
        class Bare(ExtMixin, MethodView):
            pass
        hook_view = Hook.as_view("hook")
        hook_view = login_required(hook_view)
        app.add_url_rule("/hook", view_func=hook_view)
        app.add_url_rule("/put", view_func=login_required(Put.as_view("put")))
        app.add_url_rule("/webhook", "webhook", webhook)
        app.add_url_rule("/mixed", view_func=Mixed.as_view("mixed"))
        app.add_url_rule("/bare", view_func=Bare.as_view("bare"))
        app.add_url_rule("/declared", view_func=hook_view, methods=["POST"])
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("ANY", "/hook", False, None),
        ("ANY", "/put", False, None),
        ("ANY", "/webhook", False, None),
        ("ANY", "/mixed", False, "app.py#Mixed.dispatch_request"),
        ("ANY", "/bare", False, None),
        ("POST", "/declared", False, None),
    }
    assert sorted((text, scope["templates"]) for text, scope in scoped_limitations(document)) == [
        (UNKNOWN_VIEW_METHODS.format(count=1), [path])
        for path in sorted(("/hook", "/put", "/webhook", "/mixed", "/bare", "/declared"))
    ]
    lambda_root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask
        app = Flask(__name__, static_folder=None)
        app.add_url_rule("/lambda", "lambda", lambda: "")
        app.add_url_rule("/none", "none", None)
        app.add_url_rule("/none-keyword", endpoint="none_keyword", view_func=None, methods=["DELETE"])
    """,
        }
    )
    lambda_document = routes_document(lambda_root)
    assert fact_rows(lambda_document) == {
        ("GET", "/lambda", False, None),
        ("GET", "/none", False, None),
        ("DELETE", "/none-keyword", False, None),
    }
    assert not any(str(text).startswith("route-coverage:") for text in lambda_document["limitations"])  # type: ignore[union-attr]


def test_single_bound_class_view_variable_keeps_class_methods(make_project: Callable[[dict[str, str]], Path]) -> None:
    """한 번만 묶인 모듈 변수에 담은 `Cls.as_view(...)`는 그 클래스의 method와 핸들러를 낸다.

    Flask 문서의 `view = UserAPI.as_view(...)` 모양이다. post만 정의한 `Hook`을 변수로 등록하면 Flask는 POST만 받는데
    `ANY`와 한계로 냈다. 그 변수에 옵션 속성을 쓰거나(`marked.methods = …`) 다시 묶으면 여전히 모른다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask
        from flask.views import MethodView
        app = Flask(__name__, static_folder=None)
        class Hook(MethodView):
            def post(self):
                pass
        hook_view = Hook.as_view("hook")
        app.add_url_rule("/hook", view_func=hook_view)
        app.add_url_rule("/hook-get", view_func=hook_view, methods=["GET"])
        marked = Hook.as_view("marked")
        marked.methods = ["PUT"]
        app.add_url_rule("/marked", view_func=marked)
        twice = Hook.as_view("twice")
        twice = Hook.as_view("again")
        app.add_url_rule("/twice", view_func=twice)
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("POST", "/hook", False, "app.py#Hook.post"),
        ("GET", "/hook-get", False, None),
        ("ANY", "/marked", False, None),
        ("ANY", "/twice", False, None),
    }
    assert sorted(scope["templates"][0] for _, scope in scoped_limitations(document)) == ["/marked", "/twice"]


def test_method_view_subclasses_union_inherited_methods(make_project: Callable[[dict[str, str]], Path]) -> None:
    """본문이 `methods`를 정의하지 않은 `MethodView` 하위 클래스는 기반 값과 핸들러의 합집합이다.

    Flask `MethodView.__init_subclass__`는 직접 기반마다 참인 `methods`와 `hasattr`인 핸들러 이름을 모은다. 기반이
    `methods`를 정의하면 그 값만 내서 하위 클래스 핸들러(`/child` PUT)·믹스인 앞뒤의 핸들러·다른 기반 값이 한계 없이
    빠졌다. 기대 사실은 Flask 3.1.3 `url_map`과 같다. 중간 클래스가 정의하면 그 위 정의는 섞이지 않고(`/leaf`), 빈 목록
    기반은 거짓이라 GET이며(`/from-empty`, 전에는 `ANY`), 본문의 `methods = None`은 GET이고(`/cleared`, 전에는 거짓
    PUT), 본문의 마지막 대입이 이긴다(`/twice`, 전에는 거짓 GET). `View`가 앞선 보통 클래스는 `View.methods`(None)에서
    조회가 멈추고(`/late`, 전에는 거짓 POST), 프로젝트 `View` 기반이 정의한 값은 상속한다(`/inherits`, 전과 같다).
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "bases.py": """
        from flask.views import MethodView
        class Shared(MethodView):
            methods = ["PUT"]
    """,
            "app.py": """
        from flask import Flask
        from flask.views import MethodView, View
        from bases import Shared
        app = Flask(__name__, static_folder=None)
        class Base(MethodView):
            methods = ["GET", "POST"]
            def get(self):
                pass
        class Child(Base):
            def put(self):
                pass
        class Methods:
            methods = ["post"]
        class Mixed(Methods, MethodView):
            def get(self):
                pass
        class Left(MethodView):
            methods = ["GET"]
        class Right(MethodView):
            methods = ("PUT",)
        class Both(Left, Right):
            def post(self):
                pass
        class Middle(Base):
            methods = ["DELETE"]
        class Leaf(Middle):
            def patch(self):
                pass
        class Top(MethodView):
            methods = ["POST"]
        class West(Top):
            def get(self):
                pass
        class East(Top):
            def put(self):
                pass
        class Diamond(West, East):
            pass
        class Empty(MethodView):
            methods = []
        class FromEmpty(Empty):
            def get(self):
                pass
        class Cleared(Base):
            methods = None
            def put(self):
                pass
        class Twice(MethodView):
            methods = ["GET"]
            methods = ["POST"]
            def get(self):
                pass
            def post(self):
                pass
        class Late(View, Methods):
            def dispatch_request(self):
                pass
        class Imported(Shared):
            def get(self):
                pass
        class PostOnly(View):
            methods = ["POST"]
            def dispatch_request(self):
                pass
        class Inherits(PostOnly):
            pass
        app.add_url_rule("/child", view_func=Child.as_view("child"))
        app.add_url_rule("/mixed", view_func=Mixed.as_view("mixed"))
        app.add_url_rule("/both", view_func=Both.as_view("both"))
        app.add_url_rule("/leaf", view_func=Leaf.as_view("leaf"))
        app.add_url_rule("/diamond", view_func=Diamond.as_view("diamond"))
        app.add_url_rule("/from-empty", view_func=FromEmpty.as_view("from_empty"))
        app.add_url_rule("/cleared", view_func=Cleared.as_view("cleared"))
        app.add_url_rule("/twice", view_func=Twice.as_view("twice"))
        app.add_url_rule("/late", view_func=Late.as_view("late"))
        app.add_url_rule("/imported", view_func=Imported.as_view("imported"))
        app.add_url_rule("/inherits", view_func=Inherits.as_view("inherits"))
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("GET", "/child", False, "app.py#Child.get"),
        ("POST", "/child", False, None),
        ("PUT", "/child", False, "app.py#Child.put"),
        ("GET", "/mixed", False, "app.py#Mixed.get"),
        ("POST", "/mixed", False, None),
        ("GET", "/both", False, None),
        ("POST", "/both", False, "app.py#Both.post"),
        ("PUT", "/both", False, None),
        ("DELETE", "/leaf", False, None),
        ("GET", "/leaf", False, "app.py#Leaf.get"),
        ("PATCH", "/leaf", False, "app.py#Leaf.patch"),
        ("GET", "/diamond", False, "app.py#Diamond.get"),
        ("POST", "/diamond", False, None),
        ("PUT", "/diamond", False, "app.py#Diamond.put"),
        ("GET", "/from-empty", False, "app.py#FromEmpty.get"),
        ("GET", "/cleared", False, "app.py#Cleared.get"),
        ("POST", "/twice", False, "app.py#Twice.post"),
        ("GET", "/late", False, "app.py#Late.dispatch_request"),
        ("GET", "/imported", False, "app.py#Imported.get"),
        ("PUT", "/imported", False, None),
        ("POST", "/inherits", False, "app.py#Inherits.dispatch_request"),
    }
    assert scoped_limitations(document) == []
    assert "missing-route-usrs: 7 Flask route handlers have no pythograph symbol id" in document["limitations"]  # type: ignore[operator]


def test_class_view_methods_through_writes_and_subclass_hooks(make_project: Callable[[dict[str, str]], Path]) -> None:
    """본문 밖에서 `methods`를 쓴 기반과 프로젝트 `__init_subclass__`는 값을 모르게 만든다(`ANY`와 템플릿 한계).

    기반에 쓴 값은 쓰는 순서에 따라 하위 클래스에 닿거나 닿지 않는다. 전에는 기반 쓰기를 보지 않아 `/from-written`의
    PATCH를 빠뜨리고 `View` 하위 클래스 `/from-plain`을 거짓 GET으로 냈다. `super().__init_subclass__`를 부르지 않는
    기반 훅은 `MethodView`의 합집합을 막아(`/from-silent`는 Flask에서 GET만) 거짓 POST를, 옵션을 쓰는 훅은
    (`/from-setter`는 DELETE만) 거짓 POST를 냈다. 조건 안에서만 부르는 훅(`/from-gate`, 클래스 키워드로 건너뛴다)도
    모른다. 함수가 아닌 훅(`/from-assigned`), 리터럴이 아닌 기반 정의(`/from-named`, 전부터 `ANY`), 믹스인 기반에 쓴 값
    (`/from-mid`, 전에는 PATCH를 빠뜨렸다)도 모른다. 맨 위 문장에서 `super()`를 부르는 훅(`return super(…)` 포함),
    본문이 정의한 클래스, `MethodView`보다 뒤의
    훅, 등록한 클래스 자신의 훅(하위 클래스에만 돈다)은 값을 그대로 둔다. 기대 사실은 Flask 3.1.3 `url_map`과 같거나
    그것을 덮는 `ANY`다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask
        from flask.views import MethodView, View
        app = Flask(__name__, static_folder=None)
        class Written(MethodView):
            def get(self):
                pass
        Written.methods = ["PATCH"]
        class FromWritten(Written):
            def put(self):
                pass
        class Plain(View):
            def dispatch_request(self):
                pass
        Plain.methods = ["POST"]
        class FromPlain(Plain):
            pass
        REGISTRY = []
        class Silent(MethodView):
            def __init_subclass__(cls, **kwargs):
                REGISTRY.append(cls)
            def get(self):
                pass
        class FromSilent(Silent):
            def post(self):
                pass
        class Setter(MethodView):
            def __init_subclass__(cls, **kwargs):
                super().__init_subclass__(**kwargs)
                cls.methods = ["DELETE"]
        class FromSetter(Setter):
            methods = ["POST"]
            def post(self):
                pass
        class Registry(MethodView):
            def __init_subclass__(cls, **kwargs):
                super().__init_subclass__(**kwargs)
            def get(self):
                pass
        class FromRegistry(Registry):
            def post(self):
                pass
        class Declared(Silent):
            methods = ["PUT"]
            def put(self):
                pass
        class Tail:
            def __init_subclass__(cls, **kwargs):
                pass
        class AfterView(MethodView, Tail):
            def get(self):
                pass
        class OwnHook(MethodView):
            def __init_subclass__(cls, **kwargs):
                pass
            def get(self):
                pass
        class Gate(MethodView):
            def __init_subclass__(cls, skip=False, **kwargs):
                if not skip:
                    super().__init_subclass__(**kwargs)
            def get(self):
                pass
        class FromGate(Gate, skip=True):
            def post(self):
                pass
        class Forward(MethodView):
            def __init_subclass__(cls, **kwargs):
                return super(Forward, cls).__init_subclass__(**kwargs)
            def get(self):
                pass
        class FromForward(Forward):
            def post(self):
                pass
        def _hook(cls, **kwargs):
            pass
        class Assigned(MethodView):
            __init_subclass__ = classmethod(_hook)
            def get(self):
                pass
        class FromAssigned(Assigned):
            def post(self):
                pass
        NAMES = ["POST"]
        class Named(MethodView):
            methods = NAMES
        class FromNamed(Named):
            def get(self):
                pass
        class Root:
            pass
        Root.methods = ["PATCH"]
        class Mid(Root):
            pass
        class FromMid(Mid, MethodView):
            def get(self):
                pass
        app.add_url_rule("/from-written", view_func=FromWritten.as_view("from_written"))
        app.add_url_rule("/from-plain", view_func=FromPlain.as_view("from_plain"))
        app.add_url_rule("/from-silent", view_func=FromSilent.as_view("from_silent"))
        app.add_url_rule("/from-setter", view_func=FromSetter.as_view("from_setter"))
        app.add_url_rule("/from-registry", view_func=FromRegistry.as_view("from_registry"))
        app.add_url_rule("/declared", view_func=Declared.as_view("declared"))
        app.add_url_rule("/after-view", view_func=AfterView.as_view("after_view"))
        app.add_url_rule("/own-hook", view_func=OwnHook.as_view("own_hook"))
        app.add_url_rule("/from-gate", view_func=FromGate.as_view("from_gate"))
        app.add_url_rule("/from-forward", view_func=FromForward.as_view("from_forward"))
        app.add_url_rule("/from-assigned", view_func=FromAssigned.as_view("from_assigned"))
        app.add_url_rule("/from-named", view_func=FromNamed.as_view("from_named"))
        app.add_url_rule("/from-mid", view_func=FromMid.as_view("from_mid"))
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("ANY", "/from-written", False, None),
        ("ANY", "/from-plain", False, None),
        ("ANY", "/from-silent", False, None),
        ("ANY", "/from-setter", False, None),
        ("GET", "/from-registry", False, "app.py#FromRegistry.get"),
        ("POST", "/from-registry", False, "app.py#FromRegistry.post"),
        ("PUT", "/declared", False, "app.py#Declared.put"),
        ("GET", "/after-view", False, "app.py#AfterView.get"),
        ("GET", "/own-hook", False, "app.py#OwnHook.get"),
        ("ANY", "/from-gate", False, None),
        ("GET", "/from-forward", False, "app.py#FromForward.get"),
        ("POST", "/from-forward", False, "app.py#FromForward.post"),
        ("ANY", "/from-assigned", False, None),
        ("ANY", "/from-named", False, None),
        ("ANY", "/from-mid", False, None),
    }
    unknown = ["/from-assigned", "/from-gate", "/from-mid", "/from-named", "/from-plain", "/from-setter"]
    assert sorted((text, scope["templates"]) for text, scope in scoped_limitations(document)) == [
        (UNKNOWN_VIEW_METHODS.format(count=1), [path]) for path in [*unknown, "/from-silent", "/from-written"]
    ]


def test_class_view_methods_are_remembered(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 클래스 뷰를 여러 번 등록해도 `methods` 값은 클래스마다 한 번 구한다(등록마다 사슬을 다시 걷지 않는다)."""
    rules = "".join(f'app.add_url_rule("/r{index}", view_func=Leaf.as_view("r{index}"))\n' for index in range(40))
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": "from flask import Flask\nfrom flask.views import MethodView\n"
            "app = Flask(__name__, static_folder=None)\n"
            'class Base(MethodView):\n    methods = ["POST"]\n'
            "class Leaf(Base):\n    def get(self):\n        pass\n" + rules,
        }
    )
    calls: list[str] = []
    original = view_methods_module.ClassViewMethods._compute

    def counting(
        self: view_methods_module.ClassViewMethods, symbol: ProjectSymbol, names: set[str]
    ) -> view_methods_module.MethodsValue:
        """구한 클래스를 기록하고 원래 계산을 돌려준다."""
        calls.append(symbol.qualname)
        return original(self, symbol, names)

    monkeypatch.setattr(view_methods_module.ClassViewMethods, "_compute", counting)
    assert len(fact_rows(routes_document(root))) == 80
    assert calls == ["Leaf"]


def test_method_view_lattice_visits_each_base_once(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """다이아몬드 상속 격자의 클래스를 모두 등록해도 기반에서 물려받는 값은 클래스마다 한 번만 모은다.

    경로마다 내려가면 2^깊이번이고, 등록한 클래스마다 조상 전체를 걸으면 등록 수 × 깊이다. 기대 사실은 Flask 3.1.3
    `url_map`과 같다(`L0b`는 핸들러도 값도 없어 GET, 나머지는 `L0a`의 POST를 물려받는다). 정적 상속 순환도 끝난다.
    """
    lattice = class_lattice(12, "MethodView", 'methods = ["POST"]')
    names = [line.split("(")[0].removeprefix("class ") for line in lattice.splitlines() if line.startswith("class ")]
    rules = "".join(f'app.add_url_rule("/{name}", view_func={name}.as_view("{name}"))\n' for name in names)
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": "from flask import Flask\nfrom flask.views import MethodView\n"
            "app = Flask(__name__, static_folder=None)\n" + lattice + "\n" + rules,
        }
    )
    collected: list[str] = []
    original = view_methods_module.ClassViewMethods._collect

    def counting(self: view_methods_module.ClassViewMethods, symbol: ProjectSymbol) -> view_methods_module.MethodsValue:
        """값을 모은 클래스를 기록하고 원래 결과를 돌려준다."""
        collected.append(symbol.qualname)
        return original(self, symbol)

    monkeypatch.setattr(view_methods_module.ClassViewMethods, "_collect", counting)
    assert fact_rows(routes_document(root)) == {
        ("GET" if name == "L0b" else "POST", f"/{name}", False, None) for name in names
    }
    assert len(names) == 2 * 12 + 3
    assert len(collected) == len(set(collected)) <= len(names)
    cycle = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask
        from flask.views import MethodView
        app = Flask(__name__, static_folder=None)
        class First(Second, MethodView):
            def get(self):
                pass
        class Second(First):
            def post(self):
                pass
        app.add_url_rule("/first", view_func=First.as_view("first"))
        app.add_url_rule("/second", view_func=Second.as_view("second"))
    """,
        }
    )
    # 정적 상속 순환(실행하면 NameError인 모양)은 끝나고, 물려받는 값을 모른다(전에는 한계 없는 GET·POST).
    assert fact_rows(routes_document(cycle)) == {("ANY", "/first", False, None), ("ANY", "/second", False, None)}


def test_hooks_that_may_skip_the_method_view_hook_are_unknown(make_project: Callable[[dict[str, str]], Path]) -> None:
    """다음 훅을 부르기 전에 끝날 수 있는 프로젝트 `__init_subclass__`는 `MethodView`의 합집합을 막으므로 값을 모른다.

    `if skip: return`(클래스 키워드), `try … except: return`, `super(MethodView, cls)`(다음 훅을 건너뛴다), `yield`를
    담은 훅에서 Flask는 GET만 받는데 거짓 POST를 냈다(맨 위 문장의 `super()` 호출만 보았다). 안쪽 함수의 `return`과
    `super(<정의한 클래스>, cls)`는 다음 훅을 부르므로 합집합 그대로다. 기대 사실은 Flask 3.1.3 `url_map`과 같거나
    그것을 덮는 `ANY`다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask
        from flask.views import MethodView
        app = Flask(__name__, static_folder=None)
        class Skip(MethodView):
            def __init_subclass__(cls, skip=False, **kwargs):
                if skip:
                    return
                super().__init_subclass__(**kwargs)
            def get(self):
                pass
        class FromSkip(Skip, skip=True):
            def post(self):
                pass
        class Past(MethodView):
            def __init_subclass__(cls, **kwargs):
                super(MethodView, cls).__init_subclass__(**kwargs)
            def get(self):
                pass
        class FromPast(Past):
            def post(self):
                pass
        def validate(view):
            raise ValueError
        class Tried(MethodView):
            def __init_subclass__(cls, **kwargs):
                try:
                    validate(cls)
                except ValueError:
                    return
                super().__init_subclass__(**kwargs)
            def get(self):
                pass
        class FromTried(Tried):
            def post(self):
                pass
        class Lazy(MethodView):
            def __init_subclass__(cls, **kwargs):
                yield
                super().__init_subclass__(**kwargs)
            def get(self):
                pass
        class FromLazy(Lazy):
            def post(self):
                pass
        class Own(MethodView):
            def __init_subclass__(cls, **kwargs):
                def helper():
                    return None
                super(Own, cls).__init_subclass__(**kwargs)
            def get(self):
                pass
        class FromOwn(Own):
            def post(self):
                pass
        app.add_url_rule("/from-skip", view_func=FromSkip.as_view("from_skip"))
        app.add_url_rule("/from-past", view_func=FromPast.as_view("from_past"))
        app.add_url_rule("/from-tried", view_func=FromTried.as_view("from_tried"))
        app.add_url_rule("/from-lazy", view_func=FromLazy.as_view("from_lazy"))
        app.add_url_rule("/from-own", view_func=FromOwn.as_view("from_own"))
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("ANY", "/from-skip", False, None),
        ("ANY", "/from-past", False, None),
        ("ANY", "/from-tried", False, None),
        ("ANY", "/from-lazy", False, None),
        ("GET", "/from-own", False, "app.py#FromOwn.get"),
        ("POST", "/from-own", False, "app.py#FromOwn.post"),
    }
    assert sorted(scope["templates"][0] for _, scope in scoped_limitations(document)) == [
        "/from-lazy",
        "/from-past",
        "/from-skip",
        "/from-tried",
    ]


def test_class_body_bindings_outside_plain_assignments_are_unknown(
    make_project: Callable[[dict[str, str]], Path],
) -> None:
    """본문 맨 위의 이름 대입·함수 정의 밖에서 `methods`·핸들러·`__init_subclass__`를 묶은 클래스는 값을 모른다.

    블록(`if`·`try`·`for`) 안 대입·정의, 누적 대입, `del`, 풀어 쓰는 대입, 바다코끼리는 클래스 사전의 값을 실행 경로에
    맡긴다. 전에는 맨 위 대입만 읽어 거짓 GET·POST를 냈다(`/guarded`는 Flask에서 POST만, `/dropped`는 GET만). 메서드
    안의 지역 변수 `methods`(`/local`)는 클래스 사전에 묶이지 않으므로 합집합 그대로다. 기대 사실은 Flask 3.1.3
    `url_map`과 같거나 그것을 덮는 `ANY`다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask
        from flask.views import MethodView, View
        app = Flask(__name__, static_folder=None)
        FLAG = True
        class Guarded(MethodView):
            if FLAG:
                methods = ["POST"]
            def get(self):
                pass
            def post(self):
                pass
        class Tried(MethodView):
            try:
                methods = ["PUT"]
            except NameError:
                pass
            def get(self):
                pass
        class Handler(MethodView):
            if FLAG:
                def post(self):
                    pass
        class Plain(View):
            if FLAG:
                methods = ["DELETE"]
            def dispatch_request(self):
                pass
        class Looped(MethodView):
            for methods in (["PATCH"],):
                pass
            def get(self):
                pass
        class Growing(MethodView):
            methods = ["GET"]
            methods += ["POST"]
            def get(self):
                pass
        class Dropped(MethodView):
            methods = ["POST"]
            del methods
            def get(self):
                pass
        class Unpacked(MethodView):
            methods, other = ["POST"], 1
            def get(self):
                pass
        class Walrus(MethodView):
            (methods := ["PUT"])
            def get(self):
                pass
        class Hooked(MethodView):
            if FLAG:
                def __init_subclass__(cls, **kwargs):
                    pass
            def get(self):
                pass
        class FromHooked(Hooked):
            def post(self):
                pass
        class Local(MethodView):
            def get(self):
                methods = ["DELETE"]
                return methods
            def post(self):
                pass
        app.add_url_rule("/guarded", view_func=Guarded.as_view("guarded"))
        app.add_url_rule("/tried", view_func=Tried.as_view("tried"))
        app.add_url_rule("/handler", view_func=Handler.as_view("handler"))
        app.add_url_rule("/plain", view_func=Plain.as_view("plain"))
        app.add_url_rule("/looped", view_func=Looped.as_view("looped"))
        app.add_url_rule("/growing", view_func=Growing.as_view("growing"))
        app.add_url_rule("/dropped", view_func=Dropped.as_view("dropped"))
        app.add_url_rule("/unpacked", view_func=Unpacked.as_view("unpacked"))
        app.add_url_rule("/walrus", view_func=Walrus.as_view("walrus"))
        app.add_url_rule("/from-hooked", view_func=FromHooked.as_view("from_hooked"))
        app.add_url_rule("/local", view_func=Local.as_view("local"))
    """,
        }
    )
    document = routes_document(root)
    unknown = ["/dropped", "/from-hooked", "/growing", "/guarded", "/handler", "/looped", "/plain", "/tried"]
    unknown += ["/unpacked", "/walrus"]
    assert fact_rows(document) == {
        *(("ANY", path, False, None) for path in unknown),
        ("GET", "/local", False, "app.py#Local.get"),
        ("POST", "/local", False, "app.py#Local.post"),
    }
    assert sorted(scope["templates"][0] for _, scope in scoped_limitations(document)) == unknown


def test_irregular_class_body_names_cover_every_binding_form() -> None:
    """맨 위 이름 대입·함수 정의가 아닌 묶음 형태를 모두 찾고, 안쪽 범위(메서드 본문·안쪽 클래스 본문·내포 식·람다)의
    묶음과 값 없는 주석은 세지 않는다. 맨 위 함수 정의 `methods`는 `own_assignment`가 읽지 않으므로 센다."""
    source = """
class A(Base, metaclass=M):
    plain = 1
    annotated: list[str] = ["GET"]
    only_annotated: int
    first = second = 2
    def get(self):
        local = 1
        return local
    async def post(self):
        pass
    def methods(self):
        pass
    class put(Inner, flag=(chosen := 5)):
        hidden = 1
    if FLAG:
        guarded = 1
    for looped in ():
        pass
    with ctx() as managed:
        pass
    try:
        pass
    except Exception as caught:
        pass
    import imported_module.sub
    from pkg import imported_name as alias
    global declared
    match subject:
        case [captured, *rest]:
            pass
        case {"k": 1, **remainder}:
            pass
    counted += 1
    del removed
    unpacked, other = 1, 2
    (walrused := 3)
    comprehension = [inner for inner in range(3)]
    lambda_ = lambda bound=4: bound
"""
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.ClassDef)
    assert class_body_module.irregular_names(node) == {
        "alias",
        "captured",
        "caught",
        "chosen",
        "counted",
        "declared",
        "guarded",
        "imported_module",
        "looped",
        "managed",
        "methods",
        "other",
        "put",
        "remainder",
        "removed",
        "rest",
        "unpacked",
        "walrused",
    }


def test_class_decorators_and_metaclasses_that_may_set_methods_are_unknown(
    make_project: Callable[[dict[str, str]], Path],
) -> None:
    """옵션 속성을 쓰는 프로젝트 클래스 장식자(팩토리 포함)와 `type`·`abc.ABCMeta`가 아닌 메타클래스는 값을 모른다.

    장식자는 클래스를 만든 뒤 `methods`를 바꾸고(`/from-root`는 장식한 기반의 값이 합집합에 들어간다), 메타클래스는
    이름공간을 바꾸며 상속된다. 전에는 둘 다 보지 않아 거짓 GET·POST를 냈다. 옵션을 쓰지 않는 프로젝트 장식자,
    프로젝트 밖 장식자(`dataclass`), `ABCMeta`는 값 그대로다. 본문의 빈 `methods`(`[]`·`set()`)는 거짓 값이라 Flask처럼
    GET이다(전에는 `ANY`와 한계). 기대 사실은 Flask 3.1.3 `url_map`과 같거나 그것을 덮는 `ANY`다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from abc import ABCMeta
        from dataclasses import dataclass
        from flask import Flask
        from flask.views import MethodView
        app = Flask(__name__, static_folder=None)
        REGISTRY = []
        def only_post(cls):
            cls.methods = ["POST"]
            return cls
        def post_only(cls):
            setattr(cls, "methods", ["POST"])
            return cls
        def register(cls):
            REGISTRY.append(cls)
            return cls
        def tagged(name):
            def wrap(cls):
                cls.methods = ["PATCH"]
                return cls
            return wrap
        class Meta(type):
            def __new__(mcs, name, bases, namespace):
                namespace["methods"] = ["PUT"]
                return super().__new__(mcs, name, bases, namespace)
        @only_post
        class Decorated(MethodView):
            def get(self):
                pass
            def post(self):
                pass
        @post_only
        class Set(MethodView):
            def get(self):
                pass
            def post(self):
                pass
        @tagged("x")
        class Factory(MethodView):
            def get(self):
                pass
        @only_post
        class Root(MethodView):
            def get(self):
                pass
        class FromRoot(Root):
            def put(self):
                pass
        class Metaed(MethodView, metaclass=Meta):
            def get(self):
                pass
        class MetaBase(MethodView, metaclass=Meta):
            methods = ["GET"]
        class FromMetaBase(MetaBase):
            def get(self):
                pass
            def post(self):
                pass
        @register
        class Registered(MethodView):
            def get(self):
                pass
        @dataclass
        class Data(MethodView):
            def get(self):
                pass
        class Abstract(MethodView, metaclass=ABCMeta):
            def get(self):
                pass
        class EmptyList(MethodView):
            methods = []
            def post(self):
                pass
        class EmptySet(MethodView):
            methods = set()
            def post(self):
                pass
        app.add_url_rule("/decorated", view_func=Decorated.as_view("decorated"))
        app.add_url_rule("/set", view_func=Set.as_view("set"))
        app.add_url_rule("/factory", view_func=Factory.as_view("factory"))
        app.add_url_rule("/from-root", view_func=FromRoot.as_view("from_root"))
        app.add_url_rule("/metaed", view_func=Metaed.as_view("metaed"))
        app.add_url_rule("/from-meta-base", view_func=FromMetaBase.as_view("from_meta_base"))
        app.add_url_rule("/registered", view_func=Registered.as_view("registered"))
        app.add_url_rule("/data", view_func=Data.as_view("data"))
        app.add_url_rule("/abstract", view_func=Abstract.as_view("abstract"))
        app.add_url_rule("/empty-list", view_func=EmptyList.as_view("empty_list"))
        app.add_url_rule("/empty-set", view_func=EmptySet.as_view("empty_set"))
    """,
        }
    )
    document = routes_document(root)
    unknown = ["/decorated", "/factory", "/from-meta-base", "/from-root", "/metaed", "/set"]
    assert fact_rows(document) == {
        *(("ANY", path, False, None) for path in unknown),
        ("GET", "/registered", False, "app.py#Registered.get"),
        ("GET", "/data", False, "app.py#Data.get"),
        ("GET", "/abstract", False, "app.py#Abstract.get"),
        ("GET", "/empty-list", False, None),
        ("GET", "/empty-set", False, None),
    }
    assert sorted(scope["templates"][0] for _, scope in scoped_limitations(document)) == unknown


def test_class_view_methods_reuse_the_registered_linearization(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """등록한 클래스는 `analyze_class`가 편 사슬을 쓰고, 먼저 등록한 기반의 사슬도 다시 펴지 않는다.

    전에는 등록한 클래스마다 사슬을 세 번 폈다(`analyze_class`, `class_chain`, 핸들러 이름을 위한 `analyze_class`).
    """
    rules = "".join(
        f"class Level{index}(Level{index - 1}):\n    def {verb}(self):\n        pass\n"
        f'app.add_url_rule("/level{index}", view_func=Level{index}.as_view("level{index}"))\n'
        for index, verb in enumerate(["get", "post", "put", "delete", "patch"] * 4)
        if index
    )
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": "from flask import Flask\nfrom flask.views import MethodView\n"
            "app = Flask(__name__, static_folder=None)\n"
            'class Level0(MethodView):\n    methods = ["GET"]\n'
            'app.add_url_rule("/level0", view_func=Level0.as_view("level0"))\n' + rules,
        }
    )
    linearized: list[str] = []
    original = view_methods_module.class_chain

    def counting(symbols: object, symbol: ProjectSymbol) -> object:
        """편 클래스를 기록하고 원래 사슬을 돌려준다."""
        linearized.append(symbol.qualname)
        return original(symbols, symbol)  # type: ignore[arg-type]

    monkeypatch.setattr(view_methods_module, "class_chain", counting)
    rows = fact_rows(routes_document(root))
    assert ("PATCH", "/level19", False, "app.py#Level19.patch") in rows
    assert ("GET", "/level19", False, "app.py#Level19.get") in rows
    assert linearized == []


def test_view_function_options_make_methods_unknown(make_project: Callable[[dict[str, str]], Path]) -> None:
    """뷰 함수 옵션 속성(`methods`·`required_methods`)을 쓰면 프로젝트 함수라도 method를 모른다.

    Flask 문서("View Function Options")대로 `add_url_rule`은 `view_func.methods`·`required_methods`를 읽는다. 모듈의
    `doc_index.methods = ["POST"]`·`setattr(…, "methods", …)`, `required_methods`, `@app.route` 아래에서 옵션을 다는
    장식자(팩토리 포함)를 무시하고 GET만 냈다(Flask는 POST·DELETE·PUT, `required_methods`는 GET·POST). `@app.route` 위의
    장식자는 등록 뒤에 감싸므로 GET이 맞다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        import functools
        from flask import Flask
        app = Flask(__name__, static_folder=None)
        def doc_index():
            pass
        doc_index.methods = ["POST"]
        app.add_url_rule("/docattr", view_func=doc_index)
        def req():
            pass
        req.required_methods = ["POST"]
        app.add_url_rule("/required", view_func=req, methods=["GET"])
        def set_methods(f):
            f.methods = ["POST"]
            return f
        @app.route("/stacked")
        @set_methods
        def stacked():
            pass
        def via_setattr():
            pass
        setattr(via_setattr, "methods", ["DELETE"])
        app.add_url_rule("/setattr", view_func=via_setattr)
        def wraps_methods(verb):
            def decorate(f):
                @functools.wraps(f)
                def wrapper(*args, **kwargs):
                    return f(*args, **kwargs)
                wrapper.methods = [verb]
                return wrapper
            return decorate
        @app.route("/factory")
        @wraps_methods("PUT")
        def factory_decorated():
            pass
        @set_methods
        @app.route("/above")
        def above():
            pass
        def plain():
            pass
        app.add_url_rule("/plain", view_func=plain)
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("ANY", "/docattr", False, "app.py#doc_index"),
        ("GET", "/required", False, "app.py#req"),
        ("ANY", "/stacked", False, "app.py#stacked"),
        ("ANY", "/setattr", False, "app.py#via_setattr"),
        ("ANY", "/factory", False, "app.py#factory_decorated"),
        ("GET", "/above", False, "app.py#above"),
        ("GET", "/plain", False, "app.py#plain"),
    }
    assert sorted(scope["templates"][0] for _, scope in scoped_limitations(document)) == [
        "/docattr",
        "/factory",
        "/required",
        "/setattr",
        "/stacked",
    ]


def test_view_option_targets_and_block_targets() -> None:
    """옵션 속성 쓰기는 대입·주석 대입·누적 대입·`setattr`(이름이 리터럴이 아니어도)·`__dict__`·`vars` 쓰기의 끝
    이름으로 찾고, 프로젝트 함수·클래스에 묶지 못한 쓰기와 그 대상의 `wraps`를 센다. 블록 문장이 묶는 이름
    (`with … as`, 예외 이름, `match` 캡처)은 함수 지역 묶음으로 가린다."""
    tree = ast.parse(
        "a.methods = 1\n"
        "pkg.b.required_methods: list = []\n"
        "c.methods += ['X']\n"
        "setattr(d, name, 1)\n"
        "setattr(e, 'methods', 1)\n"
        "setattr(f, 'other', 1)\n"
        "make().methods = 1\n"
        "g.methods[0] = 'GET'\n"
        "h.other = 1\n"
        "i.__dict__['methods'] = 1\n"
        "vars(j).update(methods=1)\n"
        "k.__dict__.update(other=1)\n"
        "l.__dict__.update(options)\n"
        "self.methods = 1\n"
        "functools.wraps(a)\n"
        "functools.update_wrapper(w, zz)\n"
        "def tied():\n    pass\n"
        "tied.methods = 1\n"
        "def dynamic():\n    pass\n"
        "setattr(dynamic, name, 1)\n"
    )
    writes = flask_extract.scan_option_writes([tree])
    # 이름이 리터럴이 아닌 쓰기(`setattr(d, name, 1)`, `l.__dict__.update(options)`)는 이 모듈의 함수에만 묶는다.
    assert writes.targets == {"a", "b", "c", "e", "i", "j", "tied", "dynamic"}
    assert writes.untied == 8
    decorator, harmless = ast.parse(
        "def deco(f):\n    def wrapper():\n        pass\n    wrapper.required_methods = ['PUT']\n    return wrapper\n"
        "def harmless(f):\n    f.other = 1\n    return f\n"
    ).body
    assert isinstance(decorator, ast.FunctionDef) and isinstance(harmless, ast.FunctionDef)
    assert flask_extract.stores_options(decorator) and not flask_extract.stores_options(harmless)
    blocks = ast.parse(
        "with open(p) as handle:\n    pass\n"
        "try:\n    pass\nexcept ValueError as error:\n    pass\n"
        "match value:\n    case [first, *rest]:\n        pass\n    case {'k': 1, **others}:\n        pass\n"
        "    case Point(x=captured) as whole:\n        pass\n"
    ).body
    names = {
        node.id
        for statement in blocks
        for node in flask_extract._block_targets(statement)
        if isinstance(node, ast.Name)
    }
    assert names == {"handle", "error", "first", "rest", "others", "captured", "whole"}


def test_call_form_registrations_are_emitted(make_project: Callable[[dict[str, str]], Path]) -> None:
    """알려진 앱에 장식자를 직접 부른 등록(`app.route(rule)(view)`, `app.post(rule)(view)`)은 `add_url_rule`처럼 낸다.

    대입·주석 대입의 값이나 문장으로 쓴 모양이 사실도 한계도 없이 사라졌다(Flask는 모두 등록한다). 모르는 수신자의
    `get(…)(…)`은 사전 조회 결과 호출(`handlers.get(name)(request)`)과 같은 모양이라 보지 않는다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask
        app = Flask(__name__, static_folder=None)
        def view():
            pass
        def posted():
            pass
        def third():
            pass
        def annotated():
            pass
        view2 = app.route("/assigned")(view)
        posted_view = app.post("/assigned-post")(posted)
        app.get("/shortcut-expr")(third)
        registered: object = app.route("/ann", methods=["PUT"])(annotated)
        handlers = {}
        def dispatch(name, request):
            return handlers.get(name)(request)
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("GET", "/assigned", False, "app.py#view"),
        ("POST", "/assigned-post", False, "app.py#posted"),
        ("GET", "/shortcut-expr", False, "app.py#third"),
        ("PUT", "/ann", False, "app.py#annotated"),
    }
    assert not any(str(text).startswith("route-coverage:") for text in document["limitations"])  # type: ignore[union-attr]


def test_registrations_inside_other_blocks_are_emitted(make_project: Callable[[dict[str, str]], Path]) -> None:
    """`match`·`while`·`async with`·`try … except*` 블록 안 등록도 `if`·`try` 안처럼 낸다(전에는 한계 없이 사라졌다).

    `except*`는 3.11 문법이라 3.10에서는 그 파일만 파싱하지 못한 소스가 된다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        import os
        from flask import Flask
        app = Flask(__name__, static_folder=None)
        match os.environ.get("MODE"):
            case "a":
                @app.route("/match")
                def matched():
                    pass
            case _:
                pass
        while not app.url_map:
            @app.route("/while")
            def looped():
                pass
        async def setup(lock):
            async with lock:
                @app.route("/asyncwith")
                def guarded():
                    pass
    """,
            "star.py": """
        from app import app
        try:
            @app.route("/trystar")
            def starred():
                pass
        except* ValueError:
            pass
    """,
        }
    )
    expected = {
        ("GET", "/match", False, "app.py#matched"),
        ("GET", "/while", False, "app.py#looped"),
        ("GET", "/asyncwith", False, "app.py#setup.guarded"),
    }
    if sys.version_info >= (3, 11):
        expected.add(("GET", "/trystar", False, "star.py#starred"))
    assert fact_rows(routes_document(root)) == expected


#: 앱·블루프린트로 찾지 못한 수신자나 모델링하지 않은 모양의 등록 한계 문장이다(`{count}` 자리).
UNMODELED_REGISTRATIONS = (
    "route-coverage: {count} route registrations (route, add_url_rule or a method shortcut) go through an object "
    "that is not a known Flask app or blueprint (a parameter, attribute, call result, Flask subclass instance or app "
    "imported from another package) or use a form that is not modeled, so their routes were not emitted"
)


def test_registrations_on_unknown_receivers_are_counted(make_project: Callable[[dict[str, str]], Path]) -> None:
    """앱·블루프린트로 찾지 못한 수신자의 등록은 사실이 아니라 한계로 센다.

    `app = create_app()`(팩토리 결과)에 단 `@app.route`·`@app.post`, 헬퍼 매개변수의 `app.add_url_rule`,
    `current_app.add_url_rule`, 다른 패키지에서 import한 앱, Flask 하위 클래스 인스턴스, 모르는 수신자의
    `x.route(rule)(view)`가 사실도 한계도 없이 사라져 빈 문서가 완전한 결과처럼 읽혔다. 테스트 대역(`mock.patch`)과
    지원하지 않는 서버 프레임워크의 이름·인스턴스(`bottle.route`, 모듈·함수 안의 `FastAPI()`·`APIRouter()`·
    `bottle.Bottle()`·`Quart()`)는 그 import 한계가 이미 세므로 세지 않는다. 알려진 앱의 `route(rule)(view)`는 낸다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "factory.py": """
        from flask import Flask
        def create_app():
            built = Flask(__name__, static_folder=None)
            return built
    """,
            "wsgi.py": """
        from unittest import mock
        import bottle
        from fastapi import APIRouter, FastAPI
        from quart import Quart
        from externalpkg.web import shared_app
        from flask import Flask, current_app
        from factory import create_app
        app = create_app()
        known = Flask(__name__, static_folder=None)
        class MyFlask(Flask):
            pass
        sub = MyFlask(__name__)
        router = APIRouter()
        fast = FastAPI()
        bot = bottle.Bottle()
        quart_app = Quart(__name__)
        @app.route("/health")
        def health():
            pass
        @app.post("/hook")
        def hook():
            pass
        def register(target):
            target.add_url_rule("/helper", view_func=health)
            target.route("/helper-call")(health)
        def late():
            current_app.add_url_rule("/late", view_func=health)
        @shared_app.route("/external-app")
        def external():
            pass
        @sub.route("/sub")
        def subclassed():
            pass
        known.route("/call")(health)
        @known.get("/known")
        def emitted():
            pass
        @mock.patch("os.getcwd")
        def patched(getcwd):
            pass
        @bottle.route("/bottle")
        def bottled():
            pass
        @router.get("/router")
        def routed():
            pass
        @fast.post("/fast")
        def fasted():
            pass
        @bot.route("/bot")
        def botted():
            pass
        @quart_app.route("/quart")
        async def quarted():
            pass
        def local_framework():
            local = FastAPI()
            @local.get("/local")
            def inner():
                pass
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("GET", "/call", False, "wsgi.py#health"),
        ("GET", "/known", False, "wsgi.py#emitted"),
    }
    assert UNMODELED_REGISTRATIONS.format(count=7) in document["limitations"]  # type: ignore[operator]


def test_class_bodies_are_walked(make_project: Callable[[dict[str, str]], Path]) -> None:
    """클래스 본문과 메서드 안 등록도 본다: 알려진 앱이면 사실, 아니면 한계다.

    Flask 확장의 `init_app(self, app)`·`self.app.add_url_rule`, 클래스 본문의 `@app.route`, 메서드 안에서 만든 앱의
    등록이 걷개가 클래스에 들어가지 않아 사실도 한계도 없었다(Flask는 모두 등록한다). 클래스 본문의
    `app = Blueprint(…)`는 메서드에서 이름으로 보이지 않으므로 메서드의 `app`은 모듈 앱이다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Blueprint, Flask
        app = Flask(__name__, static_folder=None)
        def ext_view():
            pass
        class Ext:
            def __init__(self, target=None):
                self.app = target
                if target is not None:
                    self.init_app(target)
            def init_app(self, target):
                target.add_url_rule("/ext", view_func=ext_view)
                self.app.add_url_rule("/selfapp", view_func=ext_view, endpoint="selfapp")
        class Api:
            @app.route("/inclass", methods=["PATCH"])
            def handler():
                pass
        class Builder:
            app = Blueprint("holder", __name__, url_prefix="/holder")
            def build(self):
                local = Flask(__name__, static_folder=None)
                local.add_url_rule("/built", view_func=ext_view)
                app.add_url_rule("/module-app", view_func=ext_view, endpoint="module_app")
        Ext(app)
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("PATCH", "/inclass", False, "app.py#Api.handler"),
        ("GET", "/built", False, "app.py#ext_view"),
        ("GET", "/module-app", False, "app.py#ext_view"),
    }
    assert {fact["pathAnchor"] for fact in document["facts"]} == {"root"}  # type: ignore[union-attr]
    assert UNMODELED_REGISTRATIONS.format(count=2) in document["limitations"]  # type: ignore[operator]


def test_hidden_names_are_not_resolved_to_module_objects(make_project: Callable[[dict[str, str]], Path]) -> None:
    """매개변수·지역 대입·클래스 본문 대입이 가린 이름은 같은 이름의 모듈 변수·앱·블루프린트로 풀지 않는다.

    모듈 `view = Getter.as_view(…)`(get만)을 매개변수·지역 `view`(Poster, post만)에 써서 Flask가 POST만 받는 `/poster`·
    `/poster2`를 `Getter.get`의 GET으로 냈고, 클래스 본문의 `bp`·메서드·함수 매개변수 `bp`를 모듈 `bp`(접두사 `/main`)로
    풀어 Flask의 `/admin/panel`·`/api/items` 대신 `/main/…`을 냈다. 가린 이름의 등록은 세고, 모르는 뷰는 `ANY`다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Blueprint, Flask
        from flask.views import MethodView
        app = Flask(__name__, static_folder=None)
        bp = Blueprint("main", __name__, url_prefix="/main")
        api_bp = Blueprint("api", __name__, url_prefix="/api")
        class Getter(MethodView):
            def get(self):
                pass
        class Poster(MethodView):
            def post(self):
                pass
        def poster():
            pass
        poster.methods = ["POST"]
        def items():
            pass
        view = Getter.as_view("getter")
        app.add_url_rule("/getter", view_func=view)
        def register(rule, view):
            app.add_url_rule(rule, view_func=view)
        def factory():
            view = Poster.as_view("poster2")
            app.add_url_rule("/poster2", view_func=view)
            items = poster
            app.add_url_rule("/function", view_func=items)
        class Admin:
            bp = Blueprint("admin", __name__, url_prefix="/admin")
            @bp.route("/panel")
            def panel():
                pass
        class Api:
            def register(self, bp):
                bp.add_url_rule("/items", view_func=items)
        def attach(bp):
            bp.add_url_rule("/attached", view_func=items)
        app.register_blueprint(bp)
        app.register_blueprint(api_bp)
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("GET", "/getter", False, "app.py#Getter.get"),
        ("ANY", None, True, None),
        ("ANY", "/poster2", False, None),
        ("ANY", "/function", False, None),
    }
    assert UNMODELED_REGISTRATIONS.format(count=3) in document["limitations"]  # type: ignore[operator]


#: 프로젝트 함수·클래스에 묶지 못한 뷰 옵션 속성 쓰기의 한계 문장이다(`{count}` 자리).
UNTIED_OPTION_WRITES = (
    "route-coverage: {count} writes of the Flask view options methods or required_methods go through a name that is "
    "not a project function or class (an alias, loop variable, parameter, __dict__ or vars, functools.wraps or "
    "update_wrapper), so views registered without methods may accept other methods than the emitted GET"
)


def test_decorators_outside_module_level_are_checked(make_project: Callable[[dict[str, str]], Path]) -> None:
    """앱 팩토리 안·클래스 본문·클래스 멤버(`staticmethod`)·클래스 장식자와 매개변수 장식자도 옵션 속성 판정에 든다.

    모듈 수준 함수 장식자만 풀어 Flask가 POST만 받는 뷰를 한계 없이 GET으로 냈다. 프로젝트 밖 장식자와 그 객체의
    메서드(`cache.cached`, 지역 `limiter = Limiter(app)`의 `limiter.limit`)는 그대로 GET이다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        import functools
        from flask import Flask
        from flask_caching import Cache
        from flask_limiter import Limiter
        from somelib import make_decorator
        app = Flask(__name__, static_folder=None)
        cache = Cache(app)
        class Decos:
            @staticmethod
            def post_only(f):
                f.methods = ["POST"]
                return f
        class Plain:
            @staticmethod
            def harmless(f):
                return f
        class PostOnly:
            def __init__(self, f):
                functools.update_wrapper(self, f)
                self.methods = ["POST"]
        def create(deco):
            def local_post(f):
                f.methods = ["POST"]
                return f
            def local_harmless(f):
                return f
            limiter = Limiter(app)
            external_local = make_decorator()
            @app.route("/factory-deco")
            @local_post
            def factory_view():
                pass
            @app.route("/factory-harmless")
            @local_harmless
            def factory_harmless():
                pass
            @app.route("/external-local")
            @external_local
            def external_local_view():
                pass
            @app.route("/param-deco")
            @deco
            def param_view():
                pass
            @app.route("/limited")
            @limiter.limit("1/second")
            def limited():
                pass
        @app.route("/static-deco")
        @Decos.post_only
        def static_view():
            pass
        class Views:
            def body_post(f):
                f.methods = ["POST"]
                return f
            @app.route("/class-deco")
            @body_post
            def class_view():
                pass
        @app.route("/class-based")
        @PostOnly
        def class_based():
            pass
        @app.route("/cached")
        @cache.cached(timeout=5)
        def cached():
            pass
        @app.route("/member")
        @Plain.harmless
        def member():
            pass
        @app.route("/lambda-harmless")
        @(lambda f: f)
        def lambda_harmless():
            pass
        @app.route("/undefined")
        @undefined_decorator
        def undefined():
            pass
    """,
        }
    )
    rows = fact_rows(routes_document(root))
    assert rows == {
        ("ANY", "/factory-deco", False, "app.py#create.factory_view"),
        ("ANY", "/param-deco", False, "app.py#create.param_view"),
        ("GET", "/limited", False, "app.py#create.limited"),
        ("ANY", "/static-deco", False, "app.py#static_view"),
        ("ANY", "/class-deco", False, "app.py#Views.class_view"),
        ("ANY", "/class-based", False, "app.py#class_based"),
        ("GET", "/cached", False, "app.py#cached"),
        ("GET", "/member", False, "app.py#member"),
        ("GET", "/factory-harmless", False, "app.py#create.factory_harmless"),
        ("GET", "/external-local", False, "app.py#create.external_local_view"),
        ("GET", "/lambda-harmless", False, "app.py#lambda_harmless"),
        ("ANY", "/undefined", False, "app.py#undefined"),
    }


def test_indirect_option_writes_are_counted(make_project: Callable[[dict[str, str]], Path]) -> None:
    """프로젝트 함수·클래스 이름에 묶지 못한 옵션 속성 쓰기는 스코프 없는 한계로 세고, 묶을 수 있는 쓰기는 그 뷰를
    모른다.

    별칭(`f = view`)·반복 변수·장식자가 아닌 함수의 매개변수에 쓰는 것과 옵션을 쓴 함수의 `functools.wraps`는 어느 뷰에
    닿는지 몰라 GET 사실에 한계 없이 남았다. 다른 모듈(`tweak.py`)의 `view.methods = …`, `__dict__`·`vars` 쓰기,
    `setattr` 람다 장식자, 클래스 본문 밖 `Hook.methods = …`는 그 뷰를 `ANY`로 만든다(Flask는 모두 다른 method다).
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "views.py": """
        def shared():
            pass
    """,
            "tweak.py": """
        from views import shared
        shared.methods = ["POST"]
    """,
            "app.py": """
        import functools
        from flask import Flask
        from flask.views import MethodView
        from views import shared
        app = Flask(__name__, static_folder=None)
        def alias_target():
            pass
        f = alias_target
        f.methods = ["POST"]
        def index():
            pass
        for fn in (index,):
            fn.methods = ["POST"]
        def helper(target, verb):
            target.methods = [verb]
            return target
        def base():
            pass
        base.methods = ["POST"]
        @functools.wraps(base)
        def wrapped():
            pass
        def via_dict():
            pass
        via_dict.__dict__["methods"] = ["POST"]
        def via_vars():
            pass
        vars(via_vars).update(methods=["POST"])
        class Hook(MethodView):
            def post(self):
                pass
            def put(self):
                pass
        Hook.methods = {"PUT"}
        app.add_url_rule("/alias", view_func=alias_target)
        app.add_url_rule("/loop", view_func=index)
        app.add_url_rule("/wraps", view_func=wrapped)
        app.add_url_rule("/third", view_func=shared)
        app.add_url_rule("/dict", view_func=via_dict)
        app.add_url_rule("/vars", view_func=via_vars)
        app.add_url_rule("/hook", view_func=Hook.as_view("hook"))
        @app.route("/lambda")
        @(lambda target: setattr(target, "methods", ["PATCH"]) or target)
        def lambda_decorated():
            pass
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("GET", "/alias", False, "app.py#alias_target"),
        ("GET", "/loop", False, "app.py#index"),
        ("GET", "/wraps", False, "app.py#wrapped"),
        ("ANY", "/third", False, "views.py#shared"),
        ("ANY", "/dict", False, "app.py#via_dict"),
        ("ANY", "/vars", False, "app.py#via_vars"),
        ("ANY", "/hook", False, None),
        ("ANY", "/lambda", False, "app.py#lambda_decorated"),
    }
    assert UNTIED_OPTION_WRITES.format(count=4) in document["limitations"]  # type: ignore[operator]


def test_decorator_option_checks_are_remembered(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 프로젝트 장식자를 쓰는 등록마다 장식자 본문을 다시 걷지 않는다(등록 수 × 본문 크기였다)."""
    views = "".join(f'@app.route("/r{index}")\n@deco\ndef view_{index}():\n    pass\n' for index in range(40))
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": "from flask import Flask\napp = Flask(__name__, static_folder=None)\n"
            "def deco(f):\n    return f\n" + views,
        }
    )
    calls: list[str] = []
    original = flask_extract.stores_options

    def counting(node: ast.AST) -> bool:
        """부른 정의를 기록하고 원래 판정을 돌려준다."""
        calls.append(getattr(node, "name", "lambda"))
        return original(node)  # type: ignore[arg-type]

    monkeypatch.setattr(flask_extract, "stores_options", counting)
    assert len(fact_rows(routes_document(root))) == 40
    assert calls == ["deco"]


def test_more_registration_shapes_are_emitted_or_counted(make_project: Callable[[dict[str, str]], Path]) -> None:
    """겹친 장식자 호출과 `for … else`는 사실로 내고, 걷개의 모양 밖 등록(`return`·목록 원소·인자·바다코끼리·별칭·
    클래스에 단 `@app.route`)은 센다. 함수 안 `r: APIRouter = APIRouter()`와 메서드 안 `self.router = APIRouter()`의
    등록은 프레임워크 import 한계가 이미 세므로 세지 않는다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from fastapi import APIRouter
        from flask import Flask
        app = Flask(__name__, static_folder=None)
        def view():
            pass
        app.route("/outer")(app.route("/inner")(view))
        for _ in []:
            pass
        else:
            app.add_url_rule("/forelse", "forelse", view)
        def returned():
            return app.route("/returned")(view)
        registered = [app.route("/listed")(view)]
        print(app.add_url_rule("/argument", "argument", view))
        if (walrus := app.route("/walrus")):
            walrus(view)
        alias = app.get
        alias("/alias")(view)
        @app.route("/klass")
        class Klass:
            pass
        def make():
            router: APIRouter = APIRouter()
            @router.get("/annotated")
            def annotated():
                pass
        class Service:
            def __init__(self):
                self.router = APIRouter()
                @self.router.post("/self-router")
                def posted():
                    pass
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("GET", "/outer", False, "app.py#view"),
        ("GET", "/inner", False, "app.py#view"),
        ("GET", "/forelse", False, "app.py#view"),
    }
    assert UNMODELED_REGISTRATIONS.format(count=6) in document["limitations"]  # type: ignore[operator]


def test_decorators_on_factory_objects_and_builtins(make_project: Callable[[dict[str, str]], Path]) -> None:
    """프로젝트 함수가 돌려준 객체의 메서드 장식자(`guard = make_guard()` 뒤 `@guard.allow("POST")`)는 그 객체의
    클래스를 모르므로 옵션을 달 수 있다고 보고, 내장 장식자(`staticmethod`)와 겹친 등록 장식자는 달지 않는다고 본다.

    팩토리 본문만 보고 GET으로 냈다(Flask는 POST·PUT). 반대로 `@app.route` 아래 `@staticmethod`는 풀지 못한 장식자로
    보아 `ANY`였다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask
        app = Flask(__name__, static_folder=None)
        class Guard:
            def allow(self, *verbs):
                def decorate(f):
                    f.methods = list(verbs)
                    return f
                return decorate
        def make_guard():
            return Guard()
        def make_api():
            return Flask("api")
        guard = make_guard()
        api = make_api()
        @app.route("/q3a")
        @guard.allow("POST")
        def module_guarded():
            pass
        def create():
            local_guard = make_guard()
            @app.route("/q3b")
            @local_guard.allow("PUT")
            def factory_guarded():
                pass
        instance = Guard()
        @app.route("/q3c")
        @instance.allow("DELETE")
        def instance_guarded():
            pass
        class Holder:
            @app.route("/static")
            @staticmethod
            def static_view():
                pass
        @app.route("/stacked")
        @api.get("/stacked-api")
        def stacked():
            pass
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("ANY", "/q3a", False, "app.py#module_guarded"),
        ("ANY", "/q3b", False, "app.py#create.factory_guarded"),
        ("ANY", "/q3c", False, "app.py#instance_guarded"),
        ("GET", "/static", False, "app.py#Holder.static_view"),
        ("GET", "/stacked", False, "app.py#stacked"),
    }


def test_dynamic_and_parameter_option_writes(make_project: Callable[[dict[str, str]], Path]) -> None:
    """다른 모듈이 가져온 뷰에 리터럴이 아닌 이름으로 쓰는 옵션(`setattr(index, NAME, …)`, `about.__dict__[KEY] = …`,
    `views.extra.__dict__[KEY]`)은 그 뷰를 모르게 하고, 프로젝트 함수와 이름만 같은 매개변수에 쓰는 것은 묶지 못한
    쓰기로 센다(같은 이름의 뷰는 GET 그대로다).

    같은 모듈에서 정의한 이름만 받아 GET이었고(Flask는 POST·PUT), 매개변수 `handler`를 프로젝트 함수 `handler`에 묶어
    세지 않았다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "views.py": """
        def index():
            pass
        def about():
            pass
        def extra():
            pass
        def handler():
            pass
        def plain():
            pass
    """,
            "tweaks.py": """
        import views
        from views import about, index
        NAME = "methods"
        KEY = "methods"
        setattr(index, NAME, ["POST"])
        about.__dict__[KEY] = ["PUT"]
        views.extra.__dict__[KEY] = ["PATCH"]
        def set_methods(handler, *verbs):
            handler.methods = list(verbs)
        set_methods(views.plain, "POST")
        def touch(user, name, value):
            setattr(user, name, value)
    """,
            "app.py": """
        from flask import Flask
        import tweaks
        from views import about, extra, handler, index, plain
        app = Flask(__name__, static_folder=None)
        app.add_url_rule("/q4a", view_func=index)
        app.add_url_rule("/q4b", view_func=about)
        app.add_url_rule("/extra", view_func=extra)
        app.add_url_rule("/handler", view_func=handler)
        app.add_url_rule("/q4c", view_func=plain)
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("ANY", "/q4a", False, "views.py#index"),
        ("ANY", "/q4b", False, "views.py#about"),
        ("ANY", "/extra", False, "views.py#extra"),
        ("GET", "/handler", False, "views.py#handler"),
        ("GET", "/q4c", False, "views.py#plain"),
    }
    assert UNTIED_OPTION_WRITES.format(count=1) in document["limitations"]  # type: ignore[operator]


def test_bare_references_walrus_and_nonlocal(make_project: Callable[[dict[str, str]], Path]) -> None:
    """등록 메서드의 단순 참조(`if app.get:`, `print(app.route)`, `app.route = …`)는 세지 않고, 바다코끼리 대입과 안쪽
    함수의 `nonlocal` 재대입이 가린 블루프린트 이름은 바깥 객체로 풀지 않고 센다.

    전에는 참조마다 모델링하지 않은 등록을 하나씩 셌고, `if (bp := other): bp.add_url_rule(…)`·
    `nonlocal bp2; bp2 = other`를 모듈·바깥 함수의 블루프린트로 풀어 Flask의 `/other/…` 대신 `/main/walrus`·`/o2/nl`을
    냈다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Blueprint, Flask
        app = Flask(__name__, static_folder=None)
        other = Blueprint("other", __name__, url_prefix="/other")
        bp = Blueprint("main", __name__, url_prefix="/main")
        def view():
            pass
        if app.get:
            pass
        print(app.route)
        def walrus():
            if (bp := other):
                bp.add_url_rule("/walrus", "walrus", view)
        def outer():
            bp2 = Blueprint("o2", __name__, url_prefix="/o2")
            def inner():
                nonlocal bp2
                bp2 = other
            inner()
            bp2.add_url_rule("/nl", "nl", view)
            app.register_blueprint(bp2, name="o2reg")
        app.add_url_rule("/real", "real", view)
        app.register_blueprint(bp)
        app.register_blueprint(other)
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {("GET", "/real", False, "app.py#view")}
    assert UNMODELED_REGISTRATIONS.format(count=2) in document["limitations"]  # type: ignore[operator]


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
    # 인자는 앞 인자에 바로 이어져야 한다(이어지지 않는 뒤 인자가 있으면 거부, 공백뿐이면 인자 없음). Werkzeug
    # 정규식의 `\d+.`은 아무 글자나 받아 `1,,2`의 `1,`도 인자 하나다.
    assert parse_converter_arguments(" 1 ,x = 'a b'") == ConverterArguments((1,), {"x": "a b"})
    assert parse_converter_arguments("1,,2") == ConverterArguments(("1,", 2), {})
    assert parse_converter_arguments("  ") == ConverterArguments((), {})
    for text in ("a b", "'a'x", "x=", "x=1 y=2"):
        with pytest.raises(Unconvertible):
            parse_converter_arguments(text)


def test_overlong_rule_is_too_long_without_tokenizing(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """연속 `/`를 합친 규칙이 `MAX_ROUTE_SOURCE_LENGTH`보다 길면 토큰으로 나누지 않고 템플릿 상한 초과 dynamic이다.

    규칙 길이의 제곱으로 토큰열을 복사한 뒤에야 too-long으로 판정해, 4 MiB 규칙 하나로 명령이 수 시간 멈추던 문제다.
    """
    tokens: list[str] = []
    part_options = rules_module._part_options

    def counting(match: re.Match[str], custom: dict[str, str | None]) -> list[Skeleton]:
        tokens.append(match.group(0))
        return part_options(match, custom)

    monkeypatch.setattr(rules_module, "_part_options", counting)
    rule = "/a" * (MAX_ROUTE_SOURCE_LENGTH // 2 + 1)
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": "from flask import Flask\napp = Flask(__name__, static_folder=None)\n"
            f'@app.route("{rule}")\ndef long():\n    pass\n'
            '@app.route("/ok")\ndef ok():\n    pass\n',
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {("GET", "/ok", False, "app.py#ok"), ("GET", None, True, "app.py#long")}
    assert document["limitations"] == [
        "route-coverage: 1 Flask rules are emitted as dynamic: the path does not form a canonical template (too-long)"
    ]
    assert tokens == ["/", "ok"]
    # 길이는 슬래시를 합친 뒤에 재므로, 합치면 짧아지는 규칙은 그대로 변환한다.
    assert rule_alternatives("/" * MAX_ROUTE_SOURCE_LENGTH + "items", True, {}) == [(Literal("/"), Literal("items"))]
    with pytest.raises(Unconvertible, match=r"\(too-long\)"):
        rule_alternatives("/" * MAX_ROUTE_SOURCE_LENGTH + "items", False, {})


def test_static_length_counts_characters_outside_converters() -> None:
    """변환기 밖 글자(정적 부분과 `/`)만 센다. Werkzeug가 거부할 `<`를 만나면 거기까지만 센다."""
    assert rules_module._static_length("/a/<int:x>/b") == 5
    assert rules_module._static_length("/<x><y>") == 1
    assert rules_module._static_length("/<any(a, b):x>/c") == 3
    assert rules_module._static_length("/a<b/" + "c" * 5000) == 2
    assert rules_module._static_length("/" + "a" * 5000) == 5001


def test_literal_heavy_rules_below_the_source_cap_skip_tokenizing(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """원문 상한 바로 아래 규칙도 변환기 밖 글자만으로 템플릿 상한을 넘으면 토큰으로 나누지 않고 too-long dynamic이다.

    같은 긴 상수를 규칙마다 재사용하면(`@app.route(BIG + "/eN")`) 상한 아래 규칙을 규칙마다 끝까지 변환해, 12 KB
    app.py 하나로 routes가 수 초~수 분 걸렸다. 정적 글자가 템플릿 상한과 같은 규칙은 그대로 변환한다.
    """
    tokens: list[str] = []
    part_options = rules_module._part_options

    def counting(match: re.Match[str], custom: dict[str, str | None]) -> list[Skeleton]:
        tokens.append(match.group(0))
        return part_options(match, custom)

    monkeypatch.setattr(rules_module, "_part_options", counting)
    big = "/a" * ((MAX_ROUTE_SOURCE_LENGTH - 64) // 2)
    views = "".join(f'@app.route(BIG + "/e{index}")\ndef view{index}():\n    pass\n' for index in range(40))
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": f'from flask import Flask\nBIG = "{big}"\napp = Flask(__name__, static_folder=None)\n'
            + views
            + f'@app.route("/{"x" * 2047}")\ndef edge():\n    pass\n'
            + f'@app.route("/{"y" * 2048}")\ndef over():\n    pass\n',
        }
    )
    document = routes_document(root)
    rows = fact_rows(document)
    assert ("GET", "/" + "x" * 2047, False, "app.py#edge") in rows
    assert sum(dynamic for _, _, dynamic, _ in rows) == 41
    assert document["limitations"] == [
        "route-coverage: 41 Flask rules are emitted as dynamic: the path does not form a canonical template (too-long)"
    ]
    assert tokens == ["/", "x" * 2047]


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


def _blueprint_lattice(depth: int, methods: str | None) -> str:
    """블루프린트 등록 격자 앱 소스를 만든다.

    단계마다 블루프린트가 둘이고 각각 다음 단계의 둘을 모두 등록한다. 앱은 첫 단계 둘을 등록하므로 등록 경로는
    2^(depth+2) - 2개다. 앱에 `/home` 규칙 하나, methods를 주면 블루프린트마다 규칙 하나가 있다.

    Args:
        depth: 단계 수 - 1.
        methods: 블루프린트 규칙의 `methods` 목록 원문(None이면 블루프린트에 규칙이 없다).

    Returns:
        `app.py` 소스.
    """
    lines = ["from flask import Flask, Blueprint", "app = Flask(__name__, static_folder=None)"]
    lines += ['@app.get("/home")', "def home():", "    pass"]
    for level in range(depth + 1):
        for side in "ab":
            lines.append(f'b{level}{side} = Blueprint("b{level}{side}", __name__, url_prefix="/{side}")')
            if methods is not None:
                lines += [f'@b{level}{side}.route("/r", methods={methods})', f"def r{level}{side}():", "    pass"]
    for level in range(depth):
        lines += [f"b{level}{side}.register_blueprint(b{level + 1}{child})" for side in "ab" for child in "ab"]
    lines += ["app.register_blueprint(b0a)", "app.register_blueprint(b0b)"]
    return "\n".join(lines) + "\n"


def test_blueprint_registration_cycle_is_a_coverage_gap(make_project: Callable[[dict[str, str]], Path]) -> None:
    """서로(또는 자기)를 등록한 블루프린트는 순환을 닫는 등록을 따라가지 않고 `route-coverage:` 한계로 센다.

    Flask는 그런 앱을 등록하다 실패한다(자기 등록은 ValueError, 서로 등록은 끝없는 재귀). 수정 전에는 하위 등록을 방문
    집합 없이 따라가 8줄짜리 순환 하나로 routes 전체가 내부 오류(RecursionError, 종료 2)와 빈 출력으로 끝났다. 순환에
    닿기 전까지의 등록은 그대로 낸다.
    """
    root = make_project(
        {
            **REQUIREMENTS,
            "app.py": """
        from flask import Flask, Blueprint
        app = Flask(__name__, static_folder=None)
        bp_a = Blueprint("a", __name__, url_prefix="/a")
        bp_b = Blueprint("b", __name__, url_prefix="/b")
        @bp_a.get("/x")
        def ax():
            pass
        @bp_b.get("/y")
        def by():
            pass
        bp_a.register_blueprint(bp_b)
        bp_b.register_blueprint(bp_a)
        bp_b.register_blueprint(bp_b, url_prefix="/again")
        app.register_blueprint(bp_a)
    """,
        }
    )
    document = routes_document(root)
    assert fact_rows(document) == {("GET", "/a/x", False, "app.py#ax"), ("GET", "/a/b/y", False, "app.py#by")}
    assert document["limitations"] == [
        "route-coverage: 2 Flask blueprint registrations close a registration cycle that Flask cannot complete, "
        "so they were not followed"
    ]


def test_blueprint_lattice_stops_at_the_fact_limit(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 블루프린트를 여러 경로로 등록한 격자는 계약 동사 선언이 사실 상한을 넘는 순간 더 펼치지 않고, 지금과 같은
    출력 상한 오류(종료 2)로 끝난다.

    Flask는 등록 경로마다 규칙을 다시 등록하므로(이름 접두사가 달라 허용된다) 깊이 d 격자는 선언이 2^d개다. 수정
    전에는 선언을 모두 만든 뒤에야 상한에 걸려 깊이 20인 4 KB 앱 하나가 수백만 개 선언을 메모리에 만들었다. 상한은
    작게 바꿔 본다.
    """
    monkeypatch.setattr(flask_extract, "MAX_FACTS", 40)
    monkeypatch.setattr(document_module, "MAX_FACTS", 40)
    emitted = 0
    original = flask_extract._emit_rule

    def counting(*arguments: object) -> None:
        nonlocal emitted
        emitted += 1
        original(*arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(flask_extract, "_emit_rule", counting)
    root = make_project({**REQUIREMENTS, "app.py": _blueprint_lattice(5, '["GET", "PURGE"]')})
    code, out, err = run_cli(["routes", "--role", "server", "--project", str(root), "--generated-at", GENERATED_AT])
    assert (code, out) == (2, "")
    assert "more than 40 route-decl facts" in err
    assert emitted <= 42


def test_methods_outside_the_contract_do_not_stop_a_blueprint_lattice(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """계약 밖 동사 선언은 나중에 빠지므로 사실 상한 계산에 넣지 않는다(상한 아래면 지금처럼 문서를 낸다)."""
    monkeypatch.setattr(flask_extract, "MAX_FACTS", 40)
    monkeypatch.setattr(document_module, "MAX_FACTS", 40)
    root = make_project({**REQUIREMENTS, "app.py": _blueprint_lattice(5, '["PURGE"]')})
    document = routes_document(root)
    assert fact_rows(document) == {("GET", "/home", False, "app.py#home")}
    assert any(sentence.startswith(OUTSIDE_METHODS.format(count=1)) for sentence, _ in scoped_limitations(document))
    templates = {template for _, scope in scoped_limitations(document) for template in scope["templates"]}  # type: ignore[attr-defined]
    assert templates == {f"/{path}/r" for path in _lattice_paths(5)}


def test_rule_less_blueprint_lattice_stops_at_the_registration_limit(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """규칙이 없는 블루프린트 격자도 펼친 하위 등록이 상한에 닿으면 멈추고 `route-coverage:` 한계를 한 번 남긴다.

    선언이 늘지 않아 사실 상한에 걸리지 않으므로 수정 전에는 깊이 40 격자(3 KB) 하나로 등록 2^41개를 펼치며 멈췄다.
    그 전까지 낸 선언은 그대로다. 상한은 작게 바꿔 본다.
    """
    monkeypatch.setattr(flask_extract, "MAX_BLUEPRINT_REGISTRATIONS", 20)
    expanded = 0
    original = flask_extract._child_prefix

    def counting(*arguments: object) -> object:
        nonlocal expanded
        expanded += 1
        return original(*arguments)

    monkeypatch.setattr(flask_extract, "_child_prefix", counting)
    root = make_project({**REQUIREMENTS, "app.py": _blueprint_lattice(5, None)})
    document = routes_document(root)
    assert fact_rows(document) == {("GET", "/home", False, "app.py#home")}
    assert document["limitations"] == [
        "route-coverage: Flask blueprint registrations expand to more than 20 registrations (the same blueprints "
        "registered along many paths), so the remaining registrations were not followed"
    ]
    assert expanded == 20


def _lattice_paths(depth: int) -> list[str]:
    """`_blueprint_lattice` 등록 경로의 접두사(`a/b/…`)를 모두 돌려준다.

    Args:
        depth: 단계 수 - 1.

    Returns:
        접두사 목록.
    """
    paths = ["a", "b"]
    found = list(paths)
    for _ in range(depth):
        paths = [f"{path}/{side}" for path in paths for side in "ab"]
        found += paths
    return found


def test_factory_local_view_functions_are_indexed_once(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """앱 팩토리 안 `add_url_rule(view_func=지역 함수)`는 모듈 함수 목록을 한 번만 만들어 찾는다(결과 id는 같다).

    등록마다 모듈 구문 트리 전체를 걸어 등록 수 × 노드 수였다(한 함수 안 등록 2,000개에 3.7초, 4,000개에 9.5초).
    같은 이름의 클래스 안 메서드(`Holder.view0`)는 지역 함수가 아니다.
    """
    original = ast.walk

    def module_walks(count: int) -> int:
        lines = ["from flask import Flask", "def create_app():", "    app = Flask(__name__, static_folder=None)"]
        for index in range(count):
            lines += [
                f"    def view{index}():",
                "        pass",
                f'    app.add_url_rule("/v{index}", view_func=view{index})',
            ]
        lines += ["    class Holder:", "        def view0(self):", "            pass", "    return app"]
        root = make_project({**REQUIREMENTS, "app.py": "\n".join(lines) + "\n"})
        walked = 0

        def counting(node: ast.AST) -> object:
            nonlocal walked
            walked += isinstance(node, ast.Module)
            return original(node)

        monkeypatch.setattr(ast, "walk", counting)
        document = routes_document(root)
        monkeypatch.setattr(ast, "walk", original)
        assert fact_rows(document) == {
            ("GET", f"/v{index}", False, f"app.py#create_app.view{index}") for index in range(count)
        }
        return walked

    assert module_walks(300) == module_walks(3)
