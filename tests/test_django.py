"""Django·DRF 추출 규칙 테스트(합성 프로젝트)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests.conftest import FIXTURES, fact_rows, facts_for, routes_document

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
