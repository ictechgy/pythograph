"""API를 쓰는 화면 로직(역방향 영향 대상)."""

from shopcli.api import OrdersApi, checkout, list_products


class OrderScreen:
    """주문 상세 화면."""

    def show(self, order_id):
        """주문을 보여 준다."""
        return OrdersApi().fetch(order_id)

    def tap_cancel(self, order_id):
        """취소 버튼."""
        api = OrdersApi()
        return api.cancel(order_id)


def catalog_refresh():
    """상품 목록 새로 고침."""
    return list_products()


def pay(cart):
    """결제 버튼."""
    return checkout(cart)
