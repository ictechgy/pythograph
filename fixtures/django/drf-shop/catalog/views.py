"""카탈로그 앱의 합성 뷰다. 함수 뷰 데코레이터와 클래스 뷰 상속을 검증한다."""

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.views import View
from django.views.decorators.http import (
    require_GET,
    require_http_methods,
    require_POST,
    require_safe,
)
from django.views.generic import DetailView, ListView


@require_http_methods(["GET", "POST"])
def item_list(request):
    """상품 목록 조회·생성이다(GET, POST)."""
    return HttpResponse("items")


@require_GET
def item_by_key(request, key):
    """키로 상품을 찾는다(GET)."""
    return HttpResponse(f"item {key}")


def featured_items(request):
    """추천 상품이다. 앞선 `items/<str:key>/`에 가려 도달하지 않는다."""
    return HttpResponse("featured")


@require_safe
def tag_detail(request, slug):
    """태그 상세다(GET, HEAD)."""
    return HttpResponse(f"tag {slug}")


@require_POST
@login_required
def submit_review(request):
    """리뷰 제출이다(POST). method 검사가 로그인 검사보다 바깥이다."""
    return HttpResponse("review")


def section_detail(request, pk):
    """두 단계 중첩 include의 뷰다."""
    return HttpResponse(f"section {pk}")


class ItemEditView(View):
    """상품 편집 화면이다. get·post를 직접 정의한다."""

    def get(self, request, pk):
        """편집 폼을 보여 준다."""
        return HttpResponse(f"edit {pk}")

    def post(self, request, pk):
        """편집 내용을 저장한다."""
        return HttpResponse(f"saved {pk}")


class ReadOnlyBaseView(View):
    """프로젝트 안 공통 기반 뷰다. get만 정의한다."""

    def get(self, request, **kwargs):
        """기본 조회 응답이다."""
        return HttpResponse("read")


class BrandView(ReadOnlyBaseView):
    """기반 뷰에서 get을 물려받는 브랜드 뷰다."""


class ProductListView(ListView):
    """상품 목록 제네릭 뷰다. DB 대신 빈 목록을 쓴다."""

    template_name = "catalog/products.html"

    def get_queryset(self):
        """DB를 쓰지 않도록 빈 목록을 돌려준다."""
        return []


class ProductDetailView(DetailView):
    """상품 상세 제네릭 뷰다(get만 받는다)."""

    template_name = "catalog/product.html"


class LimitedView(View):
    """`http_method_names`로 post를 막은 뷰다. get만 남는다."""

    http_method_names = ["get", "head", "options"]

    def get(self, request):
        """조회 응답이다."""
        return HttpResponse("limited")

    def post(self, request):
        """`http_method_names`에 없어 호출되지 않는다."""
        return HttpResponse("never")
