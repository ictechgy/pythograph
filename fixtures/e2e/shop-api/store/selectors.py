"""조회 전용 함수다. 뷰는 이 함수로만 읽는다."""

from store.models import Order, Product


def orders_for_customer(customer_id):
    """고객의 주문을 고객과 함께 읽는다.

    :param customer_id: 고객 id
    :returns: 주문 QuerySet
    """
    return Order.objects.filter(customer_id=customer_id).select_related("customer")


def active_products():
    """판매 중인 상품을 제목 순으로 읽는다.

    :returns: 상품 QuerySet
    """
    return Product.objects.filter(active=True).order_by("title")
