"""주문 앱의 합성 DRF 뷰다. DB를 쓰지 않는다(요청 시 queryset이 필요하면 오류가 난다)."""

from rest_framework import generics, mixins, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response
from rest_framework.views import APIView


@api_view(["GET", "POST"])
def order_summary(request):
    """주문 요약 조회·갱신이다(GET, POST)."""
    return Response({"summary": True})


@api_view()
def api_ping(request):
    """인자 없는 `@api_view()`라 GET만 받는다."""
    return Response({"pong": True})


class OrderStatusView(APIView):
    """주문 상태 API다. get·delete를 정의한다."""

    def get(self, request, pk):
        """상태를 조회한다."""
        return Response({"id": pk})

    def delete(self, request, pk):
        """상태를 지운다."""
        return Response(status=204)


class LineListCreate(generics.ListCreateAPIView):
    """주문 줄 목록·생성 제네릭 뷰다(GET, POST)."""


class LineDetail(generics.RetrieveUpdateDestroyAPIView):
    """주문 줄 상세 제네릭 뷰다(GET, PUT, PATCH, DELETE)."""


class OrderViewSet(viewsets.ModelViewSet):
    """주문 ModelViewSet이다. basename을 주므로 queryset이 없어도 등록된다."""


class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    """송장 읽기 전용 ViewSet이다(list, retrieve)."""


class CartViewSet(viewsets.ViewSet):
    """장바구니 ViewSet이다. lookup 설정과 확장 action을 검증한다."""

    # 상세 경로 파라미터 이름이다.
    lookup_field = "code"
    # 상세 경로 파라미터 정규식이다.
    lookup_value_regex = "[A-Z0-9]{6}"

    def list(self, request):
        """장바구니 목록이다."""
        return Response([])

    def retrieve(self, request, code=None):
        """장바구니 상세다."""
        return Response({"code": code})

    @action(detail=True, methods=["post"], url_path="set-price")
    def set_price(self, request, code=None):
        """상세 확장 action이다(POST)."""
        return Response({"code": code})

    @action(detail=False)
    def recent(self, request):
        """목록 확장 action이다(기본 GET)."""
        return Response([])

    @action(detail=True, methods=["put"], url_path="lock")
    def lock(self, request, code=None):
        """잠금 action이다(PUT)."""
        return Response({"locked": code})

    @lock.mapping.delete
    def unlock(self, request, code=None):
        """같은 경로의 DELETE를 `mapping`으로 더한 action이다."""
        return Response({"unlocked": code})


class CouponViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """쿠폰 ViewSet이다. mixin 조합으로 list·retrieve만 제공한다."""

    # 상세 경로 파라미터 정규식이다.
    lookup_value_regex = "[0-9]+"
