"""Django·DRF 추출 규칙 테스트(합성 프로젝트)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from random import Random

import pytest

from pythograph.exchange.order import order_problem
from pythograph.routes import classes as classes_module
from pythograph.routes.classes import ClassInfo, analyze_class
from pythograph.routes.django import extract as extract_module
from pythograph.routes.django.extract import DEFAULT_CONVERTERS, route_to_regex
from pythograph.routes.pattern import MAX_ROUTE_SOURCE_LENGTH, RegexPieces, Unconvertible
from pythograph.source.project import Project
from pythograph.source.symbols import ExternalSymbol, ProjectSymbol, SymbolTable
from tests.conftest import (
    FIXTURES,
    GENERATED_AT,
    REPOSITORY,
    ROUTE_DECL_METHODS,
    class_lattice,
    fact_rows,
    facts_for,
    routes_document,
    scoped_limitations,
)

#: 합성 Django 프로젝트의 기본 파일이다.
BASE = {
    "manage.py": 'import os\nos.environ.setdefault("DJANGO_SETTINGS_MODULE", "site_pkg.settings")\n',
    "site_pkg/__init__.py": "",
    "requirements.txt": "Django==5.2.17\ndjangorestframework==3.18.1\n",
}

#: 기본 설정 모듈이다.
SETTINGS = (
    'ROOT_URLCONF = "site_pkg.urls"\nINSTALLED_APPS = ["django.contrib.staticfiles", "rest_framework"]\n'
    'STATIC_URL = "static/"\n'
)


def _project(make_project: Callable[[dict[str, str]], Path], files: dict[str, str], settings: str = SETTINGS) -> Path:
    """기본 파일에 더해 합성 Django 프로젝트를 만든다.

    Args:
        make_project: 프로젝트 생성 함수.
        files: 추가 파일.
        settings: 설정 모듈 내용.

    Returns:
        프로젝트 루트.
    """
    return make_project({**BASE, "site_pkg/settings.py": settings, **files})


def test_fixture_registration_order_and_shadowing() -> None:
    """루트 URLconf 순서대로 order를 매기고 가려진 패턴도 선언으로 낸다."""
    document = routes_document(FIXTURES / "django" / "drf-shop")
    assert document["dispatch"] == "registration-order"
    by_key = facts_for(document, "/catalog/items/{}/")[0]
    featured = facts_for(document, "/catalog/items/featured/")[0]
    assert by_key["order"]["index"] < featured["order"]["index"]  # type: ignore[index]
    assert by_key["order"]["group"] == "django:shop.urls"  # type: ignore[index]


def test_dispatch_override_omits_order() -> None:
    """`--dispatch specificity`는 order를 싣지 않는다."""
    document = routes_document(FIXTURES / "django" / "drf-shop", "--dispatch", "specificity")
    assert document["dispatch"] == "specificity"
    assert all("order" not in fact for fact in document["facts"])  # type: ignore[union-attr]


def test_include_forms_and_list_operations(make_project: Callable[[dict[str, str]], Path]) -> None:
    """include(모듈 객체·import한 목록·중첩 튜플)과 목록 연산(insert·extend·+ 연결)을 따른다."""
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": """
            from django.urls import include, path, re_path
            from site_pkg import views, sub_urls
            from site_pkg.sub_urls import extra as extra_patterns
            base = [path("a/", views.a)]
            urlpatterns = base + [path("sub/", include(sub_urls)), path("extra/", include(extra_patterns))]
            urlpatterns.insert(0, path("first/", views.a))
            urlpatterns.extend([re_path(r"^b/$", views.b)])
            urlpatterns += [path("t/", ([path("x/", views.a)], "app", "ns"))]
        """,
            "site_pkg/sub_urls.py": """
            from django.urls import path
            from . import views
            urlpatterns = [path("c/", views.a)]
            extra = [path("d/", views.b)]
        """,
            "site_pkg/views.py": "def a(request):\n    pass\n\n\ndef b(request):\n    pass\n",
        },
    )
    document = routes_document(root)
    channels = [
        fact["channel"]
        for fact in sorted(
            document["facts"],  # type: ignore[arg-type]
            key=lambda fact: fact["order"]["index"],
        )
    ]
    assert channels == ["/first/", "/a/", "/sub/c/", "/extra/d/", "/b/", "/t/x/"]


def test_settings_star_import_and_override(make_project: Callable[[dict[str, str]], Path]) -> None:
    """설정 패키지의 star import를 따르고, `--settings`로 설정 모듈을 바꾼다."""
    root = _project(
        make_project,
        {
            "site_pkg/base.py": 'ROOT_URLCONF = "site_pkg.urls"\nINSTALLED_APPS = []\n',
            "site_pkg/other.py": "from .base import *\n",
            "site_pkg/urls.py": "from django.urls import path\nfrom .views import v\nurlpatterns = [path('x/', v)]\n",
            "site_pkg/views.py": "def v(request):\n    pass\n",
        },
        settings="from .base import *\n",
    )
    assert fact_rows(routes_document(root)) == {("ANY", "/x/", False, "site_pkg/views.py#v")}
    assert fact_rows(routes_document(root, "--settings", "site_pkg.other")) == {
        ("ANY", "/x/", False, "site_pkg/views.py#v")
    }


def test_settings_not_named_hints_settings_option(make_project: Callable[[dict[str, str]], Path]) -> None:
    """진입 파일이 설정 모듈을 이름으로 적지 않으면(.env로 정하는 경우) 사실 0건과 `--settings` 안내를 낸다.

    도그푸딩에서 설정 모듈을 환경 파일로 정하는 앱이 아무 안내 없이 0건이었다.
    """
    root = make_project(
        {
            "manage.py": "from dotenv import load_dotenv\nload_dotenv()\n",
            "site_pkg/__init__.py": "",
            "site_pkg/settings/__init__.py": "",
            "site_pkg/settings/base.py": SETTINGS,
            "site_pkg/urls.py": "from django.urls import path\nurlpatterns = [path('a/', lambda r: r)]\n",
        }
    )
    document = routes_document(root)
    assert document["facts"] == []
    assert any("--settings" in text for text in document["limitations"])  # type: ignore[union-attr]
    assert routes_document(root, "--settings", "site_pkg.settings.base")["facts"]


def test_missing_root_urlconf(make_project: Callable[[dict[str, str]], Path]) -> None:
    """ROOT_URLCONF를 풀지 못하면 사실 0건과 route-coverage 한계다."""
    root = _project(make_project, {}, settings="import os\nROOT_URLCONF = os.environ['X']\n")
    document = routes_document(root, "--framework", "django")
    assert document["facts"] == []
    assert any("ROOT_URLCONF" in text for text in document["limitations"])  # type: ignore[union-attr]


def test_conditional_loop_and_unknown_entries_are_gaps(make_project: Callable[[dict[str, str]], Path]) -> None:
    """조건부·반복문·알 수 없는 패턴은 decl 대신 한계가 되고, 접두사가 정적이면 스코프를 둔다."""
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": """
            from django.conf import settings
            from django.urls import include, path
            from site_pkg import views
            urlpatterns = [path("a/", views.a), path("ext/", include("thirdparty.urls")),
                           path("dj/", include("django.contrib.flatpages.urls")), make_patterns()]
            if settings.DEBUG:
                urlpatterns += [path("debug/", views.a)]
            for name in ["x"]:
                urlpatterns.append(path(name, views.a))
            urlpatterns.pop()
        """,
            "site_pkg/views.py": "def a(request):\n    pass\n",
        },
    )
    document = routes_document(root)
    assert fact_rows(document) == {("ANY", "/a/", False, "site_pkg/views.py#a")}
    limitations = document["limitations"]
    scopes = {scope["limitationIndex"]: scope for scope in document["limitationScopes"]}  # type: ignore[union-attr]
    texts = dict(enumerate(limitations))  # type: ignore[arg-type]
    scoped = {texts[index]: scope for index, scope in scopes.items()}
    assert scoped["route-coverage: 1 URL patterns are registered under a condition"]["templates"] == ["/debug/"]
    assert any(scope.get("templatePrefixes") == ["/ext"] for scope in scoped.values())
    assert any(
        scope.get("templatePrefixes") == ["/dj"]
        for text, scope in scoped.items()
        if text.startswith("framework-provided-routes:")
    )
    assert any("loop" in text for text in limitations)  # type: ignore[union-attr]
    assert any("could not be resolved statically" in text and text not in scoped for text in limitations)  # type: ignore[union-attr]


def test_function_view_decorators_and_wrappers(make_project: Callable[[dict[str, str]], Path]) -> None:
    """메서드 제한 장식자·호출 감싸기와 읽지 못한 인자를 처리한다."""
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": """
            from django.urls import path
            from django.views.decorators.csrf import csrf_exempt
            from django.views.decorators.http import require_POST, require_http_methods
            from site_pkg import views
            urlpatterns = [
                path("wrapped/", csrf_exempt(views.plain)),
                path("inline-post/", require_POST(views.plain)),
                path("both/", require_POST(views.getpost)),
                path("dynamic/", views.dynamic_methods),
                path("unknown/", views.missing_name),
                path("call/", views.factory("x", "y")),
            ]
        """,
            "site_pkg/views.py": """
            from django.views.decorators.http import require_http_methods
            METHODS = ["GET"]
            def plain(request):
                pass
            @require_http_methods(["GET", "POST", "HEAD"])
            def getpost(request):
                pass
            @require_http_methods(compute())
            def dynamic_methods(request):
                pass
        """,
        },
    )
    rows = fact_rows(routes_document(root))
    assert ("ANY", "/wrapped/", False, "site_pkg/views.py#plain") in rows
    assert ("POST", "/inline-post/", False, "site_pkg/views.py#plain") in rows
    assert ("POST", "/both/", False, "site_pkg/views.py#getpost") in rows
    assert ("ANY", "/dynamic/", False, "site_pkg/views.py#dynamic_methods") in rows
    assert ("ANY", "/unknown/", False, None) in rows
    assert ("ANY", "/call/", False, None) in rows


def test_class_views(make_project: Callable[[dict[str, str]], Path]) -> None:
    """클래스 뷰의 method: 상속, http_method_names(클래스·as_view 인자), 알 수 없는 기반, 직접 연결 viewset."""
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": """
            from django.urls import path
            from site_pkg import views
            urlpatterns = [
                path("child/", views.Child.as_view()),
                path("narrow/", views.Child.as_view(http_method_names=["post"])),
                path("foreign/", views.Foreign.as_view()),
                path("manual/<int:pk>/", views.Things.as_view({"get": "retrieve", "head": "retrieve",
                                                              "delete": "destroy"})),
                path("bad-names/", views.BadNames.as_view()),
            ]
        """,
            "site_pkg/views.py": """
            from django.views import View
            from rest_framework import viewsets
            from somewhere import ExternalBase
            class Base(View):
                def get(self, request):
                    pass
            class Child(Base):
                def post(self, request):
                    pass
            class Foreign(ExternalBase):
                def put(self, request):
                    pass
            class Things(viewsets.ModelViewSet):
                pass
            class BadNames(View):
                http_method_names = names()
                def get(self, request):
                    pass
        """,
        },
    )
    document = routes_document(root)
    rows = fact_rows(document)
    assert {
        ("GET", "/child/", False, "site_pkg/views.py#Child.get"),
        ("POST", "/child/", False, "site_pkg/views.py#Child.post"),
        ("POST", "/narrow/", False, "site_pkg/views.py#Child.post"),
        ("PUT", "/foreign/", False, "site_pkg/views.py#Foreign.put"),
        ("GET", "/manual/{}/", False, "site_pkg/views.py#Things.retrieve"),
        ("DELETE", "/manual/{}/", False, "site_pkg/views.py#Things.destroy"),
        ("ANY", "/bad-names/", False, None),
    } <= rows
    assert not any(row[1] == "/manual/{}/" and row[0] == "HEAD" for row in rows)
    scoped_templates = [scope.get("templates") for scope in document["limitationScopes"]]  # type: ignore[union-attr]
    assert ["/foreign/"] in scoped_templates


def test_auth_views(make_project: Callable[[dict[str, str]], Path]) -> None:
    """`django.contrib.auth.views` 클래스는 Django 5.2.17의 핸들러를 쓴다(LogoutView는 http_method_names로 POST만).

    도그푸딩에서 이 클래스들이 모르는 기반이라 `ANY`와 usr 없는 사실로 나왔다(오라클은 GET·POST·PUT 등만 허용).
    """
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": """
            from django.contrib.auth import views as auth_views
            from django.urls import path
            from site_pkg import views
            urlpatterns = [
                path("login/", auth_views.LoginView.as_view()),
                path("logout/", views.Logout.as_view()),
                path("logout-get/", views.LogoutWithGet.as_view()),
                path("reset/done/", auth_views.PasswordResetDoneView.as_view()),
            ]
        """,
            "site_pkg/views.py": """
            from django.contrib.auth.views import LogoutView
            class Logout(LogoutView):
                pass
            class LogoutWithGet(LogoutView):
                http_method_names = ["get", "post"]
                def get(self, request):
                    pass
        """,
        },
    )
    document = routes_document(root)
    rows = fact_rows(document)
    assert {
        ("GET", "/login/", False, None),
        ("POST", "/login/", False, None),
        ("PUT", "/login/", False, None),
        ("POST", "/logout/", False, "site_pkg/views.py#Logout.post"),
        ("GET", "/logout-get/", False, "site_pkg/views.py#LogoutWithGet.get"),
        ("POST", "/logout-get/", False, "site_pkg/views.py#LogoutWithGet.post"),
        ("GET", "/reset/done/", False, None),
    } == rows
    assert not any("views accept methods" in text for text in document["limitations"])  # type: ignore[union-attr]


def test_generic_base_views_and_template_mixins(make_project: Callable[[dict[str, str]], Path]) -> None:
    """`BaseDetailView`·`DeletionMixin`·`SingleObjectTemplateResponseMixin` 조합도 확정한다(Django 5.2.17 조사).

    도그푸딩에서 `Base…View`와 템플릿 믹스인이 표에 없어 GET을 잃고 불확정 한계가 붙었다.
    """
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": """
            from django.urls import path
            from site_pkg import views
            urlpatterns = [path("unlock/<int:pk>/", views.Unlock.as_view()), path("gone/", views.Gone.as_view())]
        """,
            "site_pkg/views.py": """
            from django.views.generic.detail import BaseDetailView, SingleObjectTemplateResponseMixin
            from django.views.generic.edit import DeletionMixin, FormMixin
            class Unlock(FormMixin, SingleObjectTemplateResponseMixin, BaseDetailView):
                def post(self, request, pk):
                    pass
            class Gone(DeletionMixin, BaseDetailView):
                pass
        """,
        },
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("GET", "/unlock/{}/", False, "site_pkg/views.py#Unlock.get"),
        ("POST", "/unlock/{}/", False, "site_pkg/views.py#Unlock.post"),
        ("GET", "/gone/", False, "site_pkg/views.py#Gone.get"),
        ("POST", "/gone/", False, "site_pkg/views.py#Gone.post"),
        ("DELETE", "/gone/", False, "site_pkg/views.py#Gone.delete"),
    }
    assert not any("views accept methods" in text for text in document["limitations"])  # type: ignore[union-attr]


def test_leading_dynamic_include_prefix_is_base(make_project: Callable[[dict[str, str]], Path]) -> None:
    """맨 앞 include의 경로 문자열이 설정값처럼 리터럴이 아니면 하위 패턴은 dynamic이 아니라 base 앵커다.

    도그푸딩에서 `path(settings.BASE_PATH, include(_patterns))` 하나 때문에 모든 경로가 dynamic으로 나왔다. 앞에 리터럴
    접두사가 있는 중간 동적 조각은 base로 표현할 수 없어 그대로 dynamic이다.
    """
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": """
            from django.conf import settings
            from django.urls import include, path
            from site_pkg import views
            _patterns = [
                path("login/", views.login),
                path("api/", include([path(views.dynamic_name(), include([path("x/", views.login)]))])),
                path("ext/", include("thirdparty.urls")),
            ]
            urlpatterns = [
                path("", include([path(settings.BASE_PATH, include(_patterns))])),
            ]
        """,
            "site_pkg/views.py": "def login(request):\n    pass\n",
        },
    )
    document = routes_document(root)
    facts = document["facts"]
    login = [fact for fact in facts if fact["channel"] == "/login/"]  # type: ignore[union-attr, index]
    assert login and login[0]["pathAnchor"] == "base" and not login[0]["dynamic"]
    assert login[0]["order"]["index"] == 0
    nested = [fact for fact in facts if fact["dynamic"]]  # type: ignore[union-attr, index]
    assert len(nested) == 1
    assert any(
        text.startswith("unresolved-route-prefix:") and "not a literal" in text
        for text in document["limitations"]  # type: ignore[union-attr]
    )
    # base 앵커 아래 불투명 include는 템플릿 접두사로 스코프를 둘 수 없다.
    assert "limitationScopes" not in document or all(
        scope.get("templatePrefixes") != ["/ext"]
        for scope in document["limitationScopes"]  # type: ignore[union-attr]
    )


def test_drf_router_modes(make_project: Callable[[dict[str, str]], Path]) -> None:
    """SimpleRouter 경로 모드·빈 prefix·format_suffix_patterns·알 수 없는 viewset을 처리한다."""
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": """
            from django.urls import include, path
            from rest_framework.routers import SimpleRouter, DefaultRouter
            from rest_framework.urlpatterns import format_suffix_patterns
            from site_pkg import views
            paths = SimpleRouter(use_regex_path=False)
            paths.register("notes", views.Notes, basename="note")
            empty = SimpleRouter()
            empty.register("", views.Notes, basename="root-note")
            broken = DefaultRouter()
            broken.register("mystery", views.Unknown, basename="m")
            custom = DefaultRouter(options())
            urlpatterns = [path("p/", include(paths.urls)), path("e/", include(empty.urls)),
                           path("b/", include(broken.urls)), path("c/", include(custom.urls))]
            urlpatterns += format_suffix_patterns([path("fmt/", views.plain)], allowed=["json", "csv"])
            urlpatterns += format_suffix_patterns([path("req/", views.plain)], suffix_required=True)
        """,
            "site_pkg/views.py": """
            from rest_framework import mixins, viewsets
            from rest_framework.decorators import action
            from lib import Unknown
            class Notes(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
                lookup_value_converter = "int"
                @action(detail=False, url_path="latest")
                def latest(self, request):
                    pass
            def plain(request):
                pass
        """,
        },
    )
    document = routes_document(root)
    rows = fact_rows(document)
    assert ("GET", "/p/notes/", False, "site_pkg/views.py#Notes.list") in rows
    assert ("GET", "/p/notes/latest/", False, "site_pkg/views.py#Notes.latest") in rows
    assert ("GET", "/p/notes/{}/", False, "site_pkg/views.py#Notes.retrieve") in rows
    assert ("GET", "/e/", False, "site_pkg/views.py#Notes.list") in rows
    assert ("GET", "/e/{}/", False, "site_pkg/views.py#Notes.retrieve") in rows
    assert ("ANY", "/fmt/", False, "site_pkg/views.py#plain") in rows
    assert ("ANY", "/fmt.json", False, "site_pkg/views.py#plain") in rows
    assert ("ANY", "/fmt.csv", False, "site_pkg/views.py#plain") in rows
    assert ("ANY", "/req/", False, "site_pkg/views.py#plain") not in rows
    assert facts_for(document, "/p/notes/{}/")[0]["paramConstraints"] == [{"kind": "int", "segment": 2}]
    scopes = [scope.get("templatePrefixes") for scope in document["limitationScopes"]]  # type: ignore[union-attr]
    assert ["/b/mystery"] in scopes
    assert any("options that are not modeled" in text for text in document["limitations"])  # type: ignore[union-attr]


def test_i18n_force_script_name_and_converters(make_project: Callable[[dict[str, str]], Path]) -> None:
    """i18n_patterns는 base 앵커, FORCE_SCRIPT_NAME은 전체 base, 알 수 없는 변환기는 dynamic이다."""
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": """
            from django.conf.urls.i18n import i18n_patterns
            from django.urls import path, register_converter
            from site_pkg import views
            register_converter(views.Mystery, "mystery")
            register_converter(views.Known, "known")
            urlpatterns = [path("m/<mystery:x>/", views.a), path("k/<known:y>/", views.a),
                           path("u/<nope:z>/", views.a), path("w/< bad>/", views.a), path(route_name(), views.a)]
            urlpatterns += i18n_patterns(path("about/", views.a))
        """,
            "site_pkg/views.py": """
            from django.urls.converters import StringConverter
            class Mystery:
                regex = compute()
            class Known(StringConverter):
                regex = "[a-z]{2}"
            def a(request):
                pass
        """,
        },
    )
    document = routes_document(root)
    anchors = {fact["channel"]: fact["pathAnchor"] for fact in document["facts"]}  # type: ignore[union-attr]
    assert anchors["/about/"] == "base"
    assert any("i18n_patterns" in text for text in document["limitations"])  # type: ignore[union-attr]
    assert anchors["/k/{}/"] == "root"
    dynamic = [fact for fact in document["facts"] if fact["dynamic"]]  # type: ignore[union-attr]
    assert len(dynamic) == 4
    forced = _project(
        make_project,
        {"site_pkg/urls.py": "urlpatterns = []\n"},
        settings='ROOT_URLCONF = "site_pkg.urls"\nFORCE_SCRIPT_NAME = "/app"\nINSTALLED_APPS = x()\n',
    )
    limitations = routes_document(forced)["limitations"]
    assert any(text.startswith("unresolved-route-prefix:") for text in limitations)  # type: ignore[union-attr]
    assert any("INSTALLED_APPS" in text for text in limitations)  # type: ignore[union-attr]


def test_deep_urlconf_module_is_a_coverage_gap(make_project: Callable[[dict[str, str]], Path]) -> None:
    """패턴 목록을 너무 깊게 이은 URLconf 모듈은 읽지 못한 소스 한계가 되고 다른 패턴은 그대로 낸다.

    `urlpatterns = [...] + [] + …` 1000항이 URLconf 해석 재귀를 넘겨 명령 전체가 내부 오류로 끝나던 회귀다.
    """
    chain = " + ".join(["[]"] * 1000)
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": (
                "from django.urls import include, path\nfrom site_pkg import views\n"
                "urlpatterns = [path('a/', views.a), path('deep/', include('site_pkg.deep_urls'))]\n"
            ),
            "site_pkg/deep_urls.py": (
                "from django.urls import path\nfrom site_pkg import views\n"
                f"urlpatterns = [path('x/', views.a)] + {chain}\n"
            ),
            "site_pkg/views.py": "def a(request):\n    pass\n",
        },
    )
    document = routes_document(root)
    assert fact_rows(document) == {("ANY", "/a/", False, "site_pkg/views.py#a")}
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert "route-coverage: 1 Python files could not be analyzed: source nesting too deep to parse" in limitations


def test_deeply_nested_regex_route_is_dynamic(make_project: Callable[[dict[str, str]], Path]) -> None:
    """묶음을 220단 중첩한 `re_path`는 dynamic 사실과 route-coverage 한계가 되고 다른 패턴은 그대로다.

    정규식 구문 트리를 재귀로 변환하다 RecursionError로 routes 전체가 내부 오류(종료 2)로 끝나던 회귀다.
    """
    pattern = "^" + "(a|" * 220 + "a" + ")" * 220 + "/$"
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": (
                "from django.urls import path, re_path\nfrom site_pkg import views\n"
                f'urlpatterns = [path("ok/", views.a), re_path(r"{pattern}", views.a)]\n'
            ),
            "site_pkg/views.py": "def a(request):\n    pass\n",
        },
    )
    document = routes_document(root)
    assert ("ANY", "/ok/", False, "site_pkg/views.py#a") in fact_rows(document)
    facts = document["facts"]
    assert isinstance(facts, list)
    assert [fact["channel"] for fact in facts if fact["dynamic"]] == ["/" + pattern]
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    reason = "the regular expression nests too deeply"
    assert f"route-coverage: 1 URL patterns are emitted as dynamic: {reason}" in limitations


def test_overlong_route_strings_are_too_long_without_conversion(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """include 접두사까지 이은 `path()`·`re_path()` 문자열이 `MAX_ROUTE_SOURCE_LENGTH`보다 길면 조각을 변환하지 않고
    템플릿 상한 초과 dynamic이다(조각 하나하나는 상한 안이어도).

    `<`만 이어진 경로는 변환기 자리 정규식이 자리마다 끝까지 훑었고(길이의 제곱), 긴 리터럴 경로도 토큰열 복사가
    제곱이라, 짧은 파일 하나로 routes가 수십 분 걸린 뒤에야 같은 too-long dynamic을 냈다.
    """
    converted: list[str] = []
    route_to_regex = extract_module.route_to_regex
    convert_regex = extract_module.convert_regex

    def counting_route(route: str, endpoint: bool, converters: dict[str, str | None]) -> str:
        converted.append("path")
        return route_to_regex(route, endpoint, converters)

    def counting_regex(pattern: str, *, endpoint: bool, anchored_by_fullmatch: bool) -> RegexPieces:
        converted.append("regex")
        return convert_regex(pattern, endpoint=endpoint, anchored_by_fullmatch=anchored_by_fullmatch)

    monkeypatch.setattr(extract_module, "route_to_regex", counting_route)
    monkeypatch.setattr(extract_module, "convert_regex", counting_regex)
    over = MAX_ROUTE_SOURCE_LENGTH + 1
    half = MAX_ROUTE_SOURCE_LENGTH // 2 + 1
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": (
                "from django.urls import include, path, re_path\nfrom site_pkg import views\n"
                f'urlpatterns = [path("ok/", views.a), path("{"<" * over}", views.a), '
                f're_path(r"^{"a" * over}$", views.a), path("{"x" * half}/", include("site_pkg.inner"))]\n'
            ),
            "site_pkg/inner.py": (
                "from django.urls import path\nfrom site_pkg import views\n"
                f'urlpatterns = [path("{"y" * half}", views.a)]\n'
            ),
            "site_pkg/views.py": "def a(request):\n    pass\n",
        },
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("ANY", "/ok/", False, "site_pkg/views.py#a"),
        ("ANY", None, True, "site_pkg/views.py#a"),
    }
    facts = document["facts"]
    assert isinstance(facts, list)
    assert [fact["channel"] for fact in facts if fact["dynamic"]] == [None, None, None]
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    reason = "the path does not form a canonical template (too-long)"
    assert f"route-coverage: 3 URL patterns are emitted as dynamic: {reason}" in limitations
    assert converted == ["path", "regex"]


def test_route_to_regex_finds_the_same_converters_as_django() -> None:
    """변환기 자리는 `<`에서 그 뒤 첫 `>`까지다.

    마지막 `>` 뒤의 `<`, 빈 `<>`, 안에 `<`가 든 자리도 Django와 같게 읽는다(찾는 범위를 마지막 `>`까지로 줄여도 같다).
    """
    converters = dict(DEFAULT_CONVERTERS)
    assert route_to_regex("a<b", True, converters) == "^a<b\\Z"
    assert route_to_regex("<int:pk>/x<", True, converters) == "^(?P<pk>[0-9]+)/x<\\Z"
    assert route_to_regex("<>a<b>", False, converters) == "^<>a(?P<b>[^/]+)"
    with pytest.raises(Unconvertible):
        route_to_regex("<<x>", True, converters)


def test_forbidden_regex_text_never_reaches_the_document(make_project: Callable[[dict[str, str]], Path]) -> None:
    """정규식 원문의 제어 문자·짝 없는 서러게이트는 `pattern`과 dynamic channel에 싣지 않는다.

    제어 문자가 든 `pattern`은 isthmus가 문서 전체를 거부했고, 서러게이트는 출력 쓰기가 UnicodeEncodeError로 실패해
    종료 2·빈 출력이었다(테스트 출력 스트림은 인코딩하지 않으므로 직렬화 결과를 직접 UTF-8로 인코딩해 본다).
    """
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": (
                "from django.urls import re_path\nfrom site_pkg import views\n"
                "urlpatterns = [\n"
                '    re_path("^items/(?P<code>[a-z\\x01]+)/$", views.a),\n'
                '    re_path("^codes/(?P<code>[a-z\\ud800]+)/$", views.a),\n'
                '    re_path("^orders/(?=x)\\ud800/$", views.a),\n'
                "]\n"
            ),
            "site_pkg/views.py": "def a(request, code=None):\n    pass\n",
        },
    )
    document = routes_document(root)
    facts = document["facts"]
    assert isinstance(facts, list)
    assert [(fact["channel"], fact["dynamic"], fact.get("paramConstraints")) for fact in facts] == [
        (None, True, None),
        ("/codes/{}/", False, [{"kind": "regex", "segment": 1}]),
        ("/items/{}/", False, [{"kind": "regex", "segment": 1}]),
    ]
    json.dumps(document, ensure_ascii=False).encode("utf-8")


def test_include_tests_marks_test_sources(make_project: Callable[[dict[str, str]], Path]) -> None:
    """테스트 경로의 URLconf 선언은 기본 제외, `--include-tests`면 testSource로 낸다."""
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": (
                "from django.urls import include, path\nurlpatterns = [path('t/', include('tests.urls'))]\n"
            ),
            "tests/__init__.py": "",
            "tests/urls.py": "from django.urls import path\nfrom tests.views import v\nurlpatterns = [path('x/', v)]\n",
            "tests/views.py": "def v(request):\n    pass\n",
        },
    )
    assert routes_document(root)["facts"] == []
    included = routes_document(root, "--include-tests")
    assert included["sourceSets"] == {"tests": "included"}
    assert [fact.get("testSource") for fact in included["facts"]] == [True]  # type: ignore[union-attr]


def test_methods_outside_the_contract_are_not_emitted(make_project: Callable[[dict[str, str]], Path]) -> None:
    """계약 동사 집합 밖 method는 decl로 내지 않고 그 패턴 템플릿 스코프의 `route-coverage:`로 센다.

    `http_method_names`·직접 연결 viewset·`require_http_methods`·DRF `@action(methods=…)`의 `purge`를 대문자로만
    바꿔 `PURGE` decl을 내 isthmus가 문서 전체를 거부하던 회귀다. 사실을 하나도 내지 않은 등록의 순번은 비어도
    된다(GRAPH-EXCHANGE "디스패치 모델": index는 연속일 필요가 없다).
    """
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": """
            from django.urls import include, path
            from rest_framework.routers import SimpleRouter
            from site_pkg import views
            router = SimpleRouter()
            router.register("notes", views.Notes, basename="note")
            urlpatterns = [
                path("cache/", views.Cache.as_view()),
                path("only/", views.Only.as_view()),
                path("things/<int:pk>/", views.Things.as_view({"get": "retrieve", "purge": "purge"})),
                path("fn/", views.fn),
                path("api/", include(router.urls)),
            ]
        """,
            "site_pkg/views.py": """
            from django.views import View
            from django.views.decorators.http import require_http_methods
            from rest_framework import mixins, viewsets
            from rest_framework.decorators import action
            class Cache(View):
                http_method_names = ["get", "purge"]
                def get(self, request):
                    pass
                def purge(self, request):
                    pass
            class Only(View):
                http_method_names = ["purge"]
                def purge(self, request):
                    pass
            class Things(viewsets.ViewSet):
                def retrieve(self, request, pk):
                    pass
                def purge(self, request, pk):
                    pass
            @require_http_methods(["GET", "PURGE"])
            def fn(request):
                pass
            class Notes(mixins.ListModelMixin, viewsets.GenericViewSet):
                @action(detail=False, methods=["purge"])
                def flush(self, request):
                    pass
        """,
        },
    )
    document = routes_document(root)
    assert fact_rows(document) == {
        ("GET", "/cache/", False, "site_pkg/views.py#Cache.get"),
        ("GET", "/things/{}/", False, "site_pkg/views.py#Things.retrieve"),
        ("GET", "/fn/", False, "site_pkg/views.py#fn"),
        ("GET", "/api/notes/", False, "site_pkg/views.py#Notes.list"),
    }
    assert {fact["method"] for fact in document["facts"]} <= ROUTE_DECL_METHODS  # type: ignore[union-attr]
    assert order_problem(document) is None
    message = (
        "route-coverage: 1 route registrations accept HTTP methods the exchange format does not allow; "
        "declarations for those methods were not emitted"
    )
    templates = [scope.get("templates") for sentence, scope in scoped_limitations(document) if sentence == message]
    assert templates == [["/api/notes/flush/"], ["/cache/"], ["/fn/"], ["/only/"], ["/things/{}/"]]


def test_expansion_cap_uses_the_capped_prefix(make_project: Callable[[dict[str, str]], Path]) -> None:
    """템플릿 펼침이 16개를 넘은 패턴(정규식 안·include 조각 곱)은 `route-template-expansion-capped:`, 그 밖의 dynamic은
    `route-coverage:`다.

    접두사를 이유 문구의 부분 문자열("more than 16")이 아니라 펼침 상한 표식으로 고른 뒤에도 Django 출력이 그대로인지
    본다.
    """
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": """
            from django.urls import include, re_path
            from site_pkg import views
            urlpatterns = [
                re_path(r"^(a|b|c|d|e)/(f|g|h|i|j)/$", views.a),
                re_path(r"^(?:k|l|m|n|o)/", include("site_pkg.inner")),
                re_path(r"^two/(?P<x>[0-9]+)-(?P<y>[0-9]+)/$", views.a),
            ]
        """,
            "site_pkg/inner.py": """
            from django.urls import re_path
            from site_pkg import views
            urlpatterns = [re_path(r"^(p|q|r|s)/$", views.a)]
        """,
            "site_pkg/views.py": "def a(request, **kwargs):\n    pass\n",
        },
    )
    document = routes_document(root)
    assert {fact["dynamic"] for fact in document["facts"]} == {True}  # type: ignore[union-attr]
    assert document["limitations"] == [
        "framework-provided-routes: django.contrib.staticfiles serves STATIC_URL files in DEBUG",
        "route-coverage: 1 URL patterns are emitted as dynamic: a path segment holds more than one parameter",
        "route-template-expansion-capped: 1 URL patterns are emitted as dynamic: URL pattern alternatives expand to "
        "more than 16 templates",
        "route-template-expansion-capped: 1 URL patterns are emitted as dynamic: optional parts expand to more than "
        "16 templates",
    ]


#: 무작위 계층에서 기반으로 고르는 계층 밖 이름이다(표에 있는 프레임워크 클래스, 표에 없는 외부 클래스, 해석 실패).
_FOREIGN_BASES = ("View", "TemplateView", "ExternalBase", "Missing")

#: 무작위 계층의 클래스 본문 재료다. 같은 이름을 여러 클래스가 정의해 사슬 순서가 속성 값을 가른다.
_BODY_LINES = (
    "def get(self, request):\n        pass",
    "def post(self, request):\n        pass",
    'http_method_names = ["get"]',
    'http_method_names = ["post", "options"]',
    'template_name = "first"',
    'template_name = "second"',
)

#: 손으로 고른 계층이다(모듈 경로 → 소스). 경로마다 다시 펴는 구현과 메모가 갈리기 쉬운 모양이다.
_HAND_HIERARCHIES = {
    # 같은 깊이에서 다시 만나는 X의 사슬이 경로에 따라 다르다(A가 조상이면 X 아래의 A를 건너뛴다).
    "pkg/hand_cycle.py": """
class R(B1, A):
    pass

class B1(X):
    pass

class A(X):
    template_name = "a"

class X(A):
    template_name = "x"
""",
    # 상속 깊이 상한(32)을 넘는 긴 순환에 가지가 붙었다.
    "pkg/hand_ring.py": "\n".join(
        [f"class C{index}(C{(index + 1) % 40}):\n    template_name = 'c{index}'\n" for index in range(40)]
        + ["class D(C5, C20):\n    pass\n", "class E(C30, D, C30):\n    pass\n"]
    ),
    # 깊이 상한을 넘는 사슬 위의 작은 다이아몬드다(두 경로 모두 같은 깊이에서 잘린다).
    "pkg/hand_chain.py": "from django.views import View\n\n"
    + "class Base0(View):\n    def get(self, request):\n        pass\n\n"
    + "".join(f"class Base{index}(Base{index - 1}):\n    pass\n\n" for index in range(1, 41))
    + "class Left(Base40):\n    pass\n\nclass Right(Base40):\n    template_name = 'r'\n\n"
    + "class Join(Left, Right):\n    pass\n",
    # 이름을 가린 기반(`class View(View)`는 정적으로 자기 자신)과 그 위의 다이아몬드다.
    "pkg/hand_shadow.py": """
from django.views import View

class View(View):
    template_name = "base"

class A(View):
    pass

class B(View):
    template_name = "b"

class C(A, B, Missing):
    pass
""",
    # 바닥이 꼭대기를 다시 상속하는 순환 격자와 다른 모듈의 순환 계층을 섞는다.
    "pkg/hand_lattice.py": "from django.views import View\nfrom pkg.hand_cycle import R, X\n\n"
    + class_lattice(6, "View, X", 'template_name = "bottom"', back_edge=True)
    + "\nclass Mixed(Top, R, L3b):\n    pass\n",
}


def _reference_order(
    symbols: SymbolTable, symbol: ProjectSymbol, depth: int = 0, seen: frozenset[str] = frozenset()
) -> list[ProjectSymbol | ExternalSymbol | None]:
    """수정 전 `routes.classes._linearize`를 옮긴 오라클이다. 경로마다 기반 사슬을 다시 펴고 뒤에 다시 나온 것만 남긴다.

    Args:
        symbols: 이름 해석기.
        symbol: 시작 클래스.
        depth: 현재 깊이.
        seen: 지금 경로의 조상 id.

    Returns:
        클래스 목록(해석 실패는 None).
    """
    if depth > classes_module.MAX_CLASS_DEPTH or symbol.id in seen:
        return [None] if depth > classes_module.MAX_CLASS_DEPTH else []
    node = symbol.node
    assert isinstance(node, ast.ClassDef)
    order: list[ProjectSymbol | ExternalSymbol | None] = [symbol]
    for base in node.bases:
        resolved = symbols.resolve_expr(symbol.path, base)
        if isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef):
            order.extend(_reference_order(symbols, resolved, depth + 1, seen | {symbol.id}))
        elif isinstance(resolved, ExternalSymbol):
            order.append(resolved)
        else:
            order.append(None)
    keys = [entry.id if isinstance(entry, ProjectSymbol) else getattr(entry, "dotted", None) for entry in order]
    return [entry for index, entry in enumerate(order) if keys[index] is None or keys[index] not in keys[index + 1 :]]


def _reference_info(symbols: SymbolTable, symbol: ProjectSymbol) -> ClassInfo:
    """오라클 사슬로 `analyze_class`와 같은 방식의 분석 결과를 만든다.

    Args:
        symbols: 이름 해석기.
        symbol: 클래스.

    Returns:
        분석 결과.
    """
    info = ClassInfo(symbol=symbol)
    for entry in _reference_order(symbols, symbol):
        if isinstance(entry, ProjectSymbol):
            classes_module._add_project(symbols, info, entry)
        elif isinstance(entry, ExternalSymbol):
            classes_module._add_external(info, entry.dotted)
        else:
            info.unknown_bases = True
    return info


def _random_hierarchy(random: Random) -> str:
    """순환·다이아몬드·외부 기반·해석 실패가 섞인 작은 클래스 계층 모듈 소스를 만든다.

    Args:
        random: 난수 생성기(시드 고정).

    Returns:
        모듈 소스.
    """
    lines = ["from django.views import View", "from django.views.generic import TemplateView"]
    lines += ["from somewhere import ExternalBase", ""]
    names = [f"K{index}" for index in range(random.randint(4, 7))]
    for name in names:
        bases = random.sample(names + list(_FOREIGN_BASES), random.choice((0, 1, 1, 2, 2, 3)))
        lines.append(f"class {name}({', '.join(bases)}):" if bases else f"class {name}:")
        lines += [f"    {line}" for line in random.sample(_BODY_LINES, random.randint(0, 2))] or ["    pass"]
        lines.append("")
    return "\n".join(lines)


def _module_classes(symbols: SymbolTable) -> list[ProjectSymbol]:
    """프로젝트의 모든 모듈 수준 클래스를 돌려준다.

    Args:
        symbols: 이름 해석기.

    Returns:
        클래스 목록(파일 순서).
    """
    found: list[ProjectSymbol] = []
    for path in symbols.project.python_files():
        index = symbols.index(path)
        assert index is not None
        found += [
            ProjectSymbol(path, index.qualname(node), node)
            for node in index.module.tree.body
            if isinstance(node, ast.ClassDef)
        ]
    return found


def test_class_analysis_matches_the_path_by_path_reference(make_project: Callable[[dict[str, str]], Path]) -> None:
    """상속 사슬을 (클래스, 깊이)마다 한 번 펴도 경로마다 다시 펴던 구현과 같은 분석 결과다.

    다이아몬드·순환(자기 자신, 같은 깊이에서 경로마다 갈리는 순환, 깊이 상한을 넘는 순환)·깊이 절단·모르는 기반이 섞인
    무작위 계층과 손으로 고른 계층의 모든 모듈 수준 클래스를 비교한다. 클래스마다 새로 분석하므로 앞선 분석의 메모가
    뒤 분석을 바꾸지 않는지도 본다.
    """
    random = Random(20261019)
    files = {"pkg/__init__.py": "", **_HAND_HIERARCHIES}
    files |= {f"pkg/random{number}.py": _random_hierarchy(random) for number in range(150)}
    root = make_project(files)
    symbols = SymbolTable(Project.open(root.resolve()))
    found = _module_classes(symbols)
    assert len(found) > 800
    for symbol in found:
        assert analyze_class(symbols, symbol) == _reference_info(symbols, symbol), symbol.id


def test_class_lattice_resolves_each_class_once(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """다이아몬드 상속 격자를 분석할 때 (클래스, 깊이)마다 기반을 한 번만 푼다(순환 격자, 자기 자신을 상속한 바닥 포함).

    경로마다 사슬을 다시 펴서 깊이 d 격자 하나에 2^d번 재귀하던 문제다. 1 KB 남짓한 모듈 하나로 graph·schema·routes가
    깊이 20에서 수십 초, 깊이 32에서 수십 시간을 썼다(graph는 모듈 수준 클래스마다 이 분석을 부른다). 자기 자신에 걸린
    순환 가드는 그 클래스 안쪽 일이라 위 단계가 기억한 결과를 다시 쓰는 데 방해가 되지 않아야 한다.
    """
    sources = {
        "pkg/plain.py": "from django.views import View\n"
        + class_lattice(12, "View", "def get(self, request):\n        pass"),
        "pkg/ring.py": "from django.views import View\n" + class_lattice(12, "View", back_edge=True),
        "pkg/shadowed.py": "from django.views import View\n" + class_lattice(12, "View, L0a"),
    }
    root = make_project({"pkg/__init__.py": "", **sources})
    symbols = SymbolTable(Project.open(root.resolve()))
    resolved = 0
    original = SymbolTable.resolve_expr

    def counting(self: SymbolTable, path: str, expr: ast.expr, hops: int = 0) -> object:
        nonlocal resolved
        resolved += 1
        return original(self, path, expr, hops)

    monkeypatch.setattr(SymbolTable, "resolve_expr", counting)
    for path, source in sources.items():
        resolved = 0
        info = analyze_class(symbols, symbols.resolve_name(path, "Top"))
        assert info is not None and info.kinds == {"django-view"}
        bases = sum(len(node.bases) for node in ast.parse(source).body if isinstance(node, ast.ClassDef))
        assert resolved <= 2 * bases, path
        assert ("get" in info.names) == (path == "pkg/plain.py")


def _dense_cycles(levels: int) -> str:
    """모든 클래스가 아래 단계 둘과 위 단계의 모든 클래스를 상속하는 촘촘한 정적 순환 계층을 만든다(바닥은 `View`도).

    순환 가드가 지나온 경로에 따라 다른 판정을 내 기억한 결과를 거의 다시 쓸 수 없는 모양이다.

    Args:
        levels: 단계 수(단계마다 클래스 둘).

    Returns:
        모듈 소스.
    """
    lines = ["from django.views import View", ""]
    for level in range(levels):
        for side in "ab":
            bases = ["View"] if level == 0 else [f"D{level - 1}{other}" for other in "ab"]
            bases += [f"D{upper}{other}" for upper in range(level + 1, levels) for other in "ab"]
            lines += [f"class D{level}{side}({', '.join(bases)}):", "    pass", ""]
    return "\n".join(lines)


def test_dense_cyclic_class_hierarchy_stops_relinearizing(
    make_project: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """경로마다 순환 가드 판정이 갈리는 촘촘한 순환 계층은 다시 펴는 상한에서 멈추고 남은 기반을 해석 실패로 둔다.

    실제 파이썬으로는 만들 수 없는 계층이라 정확한 사슬이 없다. 해석 실패로 두면 뷰 method는 확정하지 못한 쪽(`ANY`와
    한계)으로 간다. 상한이 없으면 클래스 수의 계승만큼 경로를 편다(단계 8이면 수정 전처럼 끝나지 않는다). 상한은 작게
    바꿔 본다.
    """
    monkeypatch.setattr(classes_module, "MAX_RELINEARIZATIONS", 5)
    source = _dense_cycles(4)
    root = make_project({"pkg/__init__.py": "", "pkg/dense.py": source})
    symbols = SymbolTable(Project.open(root.resolve()))
    resolved = 0
    original = SymbolTable.resolve_expr

    def counting(self: SymbolTable, path: str, expr: ast.expr, hops: int = 0) -> object:
        nonlocal resolved
        resolved += 1
        return original(self, path, expr, hops)

    monkeypatch.setattr(SymbolTable, "resolve_expr", counting)
    info = analyze_class(symbols, symbols.resolve_name("pkg/dense.py", "D0a"))
    assert info is not None and info.unknown_bases and info.kinds == {"django-view"}
    classes = [node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)]
    expansions = len(classes) * (len(classes) + 1) + 5
    assert resolved <= expansions * max(len(node.bases) for node in classes)


def test_view_class_lattice_routes_finish(make_project: Callable[[dict[str, str]], Path]) -> None:
    """상속 깊이 상한(32)까지 닿는 다이아몬드 격자 뷰도 routes가 끝나고 바닥에서 물려받은 핸들러를 낸다.

    별도 프로세스의 제한 시간은 멈춤을 실패로 바꾸는 안전장치일 뿐 성능 측정이 아니다(수정 전에는 2^32번 재귀).
    """
    root = _project(
        make_project,
        {
            "site_pkg/urls.py": "from django.urls import path\nfrom site_pkg.views import Top\n"
            'urlpatterns = [path("x/", Top.as_view())]\n',
            "site_pkg/views.py": "from django.views import View\n"
            + class_lattice(31, "View", "def get(self, request):\n        pass"),
        },
    )
    command = ["routes", "--role", "server", "--project", str(root), "--generated-at", GENERATED_AT]
    result = subprocess.run(
        [sys.executable, "-m", "pythograph", *command],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPOSITORY,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert fact_rows(json.loads(result.stdout)) == {("GET", "/x/", False, "site_pkg/views.py#Top.get")}
