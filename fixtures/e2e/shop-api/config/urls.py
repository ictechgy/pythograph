"""루트 URLconf다. API는 `/api/` 아래에 둔다."""

from django.urls import include, path

# 루트 URL 패턴이다.
urlpatterns = [
    path("api/", include("store.urls")),
]
