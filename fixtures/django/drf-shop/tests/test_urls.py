"""테스트 전용 URLconf다. 기본 추출에서 제외되어야 한다."""

from django.http import HttpResponse
from django.urls import path


def fake_endpoint(request):
    """테스트에서만 쓰는 가짜 뷰다."""
    return HttpResponse("fake")


# 테스트용 URL 패턴이다. ROOT_URLCONF가 참조하지 않는다.
urlpatterns = [
    path("test-only/", fake_endpoint),
]


def test_fake_endpoint_is_callable():
    """가짜 뷰가 호출 가능한지만 본다."""
    assert callable(fake_endpoint)
