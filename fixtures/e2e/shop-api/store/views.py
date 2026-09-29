"""상점 API 뷰다. viewset·제네릭 뷰·APIView·함수 뷰를 한 앱에 둔다."""

from rest_framework import generics, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response
from rest_framework.views import APIView

from store import selectors, services
from store.serializers import OrderSerializer, ProductSerializer


class OrderViewSet(viewsets.ModelViewSet):
    """주문 viewset이다. 조회 범위는 `get_queryset`이 정한다."""

    serializer_class = OrderSerializer

    def get_queryset(self):
        """요청한 고객의 주문만 돌려준다."""
        return selectors.orders_for_customer(self.request.user.id)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """주문을 취소한다(POST)."""
        services.orders.cancel(pk)
        return Response(status=204)


class ProductListView(generics.ListAPIView):
    """판매 중인 상품 목록이다(GET)."""

    serializer_class = ProductSerializer

    def get_queryset(self):
        """판매 중인 상품만 돌려준다."""
        return selectors.active_products()


class CheckoutView(APIView):
    """결제(주문 생성) API다(POST)."""

    def post(self, request):
        """요청한 상품으로 주문을 만든다."""
        order = services.orders.place(request.user.id, request.data["items"])
        return Response(OrderSerializer(order).data, status=201)


@api_view(["GET"])
def customer_summary(request, pk):
    """고객의 주문 수를 돌려준다(GET)."""
    return Response({"orders": selectors.orders_for_customer(pk).count()})
