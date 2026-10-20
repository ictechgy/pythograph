"""Flask 추출 규칙 테스트(합성 프로젝트)."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.exchange import document as document_module
from pythograph.routes.flask import extract as flask_extract
from pythograph.routes.flask import rules as rules_module
from pythograph.routes.flask.rules import ConverterArguments, parse_converter_arguments, rule_alternatives
from pythograph.routes.pattern import MAX_ROUTE_SOURCE_LENGTH, Literal, Skeleton, Unconvertible
from tests.conftest import (
    FIXTURES,
    GENERATED_AT,
    ROUTE_DECL_METHODS,
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
