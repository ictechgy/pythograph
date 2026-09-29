"""합성 상점 프로젝트의 루트 URLconf다.

등록 순서·include 중첩·변환기·정규식 경로·조건부 등록을 한곳에 모아 라우트 추출기를 검증한다.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path, register_converter
from django.views.generic import RedirectView, TemplateView

from catalog.converters import FourDigitYearConverter
from shop import views

register_converter(FourDigitYearConverter, "yyyy")

# include()로 넘기는 모듈 수준 목록이다.
health_patterns = [
    path("live/", views.health_live),
    path("ready/", views.health_ready),
]

# 루트 URL 패턴 목록이다. 앞선 패턴이 먼저 맞으면 뒤 패턴은 시도하지 않는다.
urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.home),
    path("about/", TemplateView.as_view(template_name="about.html")),
    path("old-home/", RedirectView.as_view(url="/")),
    path("catalog/", include("catalog.urls")),
    path("orders/", include(("orders.urls", "orders"), namespace="orders")),
    path("health/", include(health_patterns)),
    path(
        "inline/",
        include(
            [
                path("a/", views.inline_a),
                path("b/<int:pk>/", views.inline_b),
            ]
        ),
    ),
    path("reports/<yyyy:year>/", views.yearly_report),
    re_path(r"^legacy/(?P<year>[0-9]{4})/$", views.legacy_year),
    re_path(r"^archive/(?P<slug>[-a-zA-Z0-9_]+)/?$", views.archive),
    re_path(r"^(?:v1|v2)/ping/$", views.ping),
    re_path(r"^(?P<code>(?!admin)[a-z]+)/promo/$", views.promo),
]

urlpatterns += [
    path("files/<str:name>.json", views.file_json),
    path("docs/<path:rest>", views.docs_page),
]

urlpatterns.append(path("pages/<slug:slug>/", views.page_detail))

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if getattr(settings, "ENABLE_BETA", False):
    urlpatterns.append(path("beta/", views.beta))
