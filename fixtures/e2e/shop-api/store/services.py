"""주문 쓰기 서비스다. 모듈 수준 인스턴스(`orders`)로 쓴다."""

from store import audit
from store.models import Order, OrderLine


class OrderService:
    """주문을 만들고 취소한다."""

    def place(self, customer_id, items):
        """주문과 주문 줄을 만든다.

        :param customer_id: 고객 id
        :param items: {"product", "quantity"} 목록
        :returns: 만든 주문
        """
        order = Order.objects.create(customer_id=customer_id)
        for item in items:
            OrderLine.objects.create(order=order, product_id=item["product"], quantity=item["quantity"])
        return order

    def cancel(self, order_id):
        """주문을 취소하고 감사 기록을 남긴다.

        :param order_id: 주문 id
        """
        Order.objects.filter(pk=order_id).update(status="cancelled")
        audit.record("cancel", order_id)


# 뷰가 쓰는 서비스 인스턴스다.
orders = OrderService()
