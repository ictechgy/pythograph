"""래퍼 호출부."""

from shopclient.api import Endpoint, Gateway, HttpMethod, send


def gateway_order_create():
    """enum 동사."""
    return send(HttpMethod.POST, "/orders")


def gateway_order(order_id):
    """리터럴 동사."""
    return send("GET", f"/orders/{order_id}")


def cart_delete(cart_id):
    """생성자 래퍼, 키워드 동사."""
    return Endpoint(f"/carts/{cart_id}", method="DELETE").perform()


def cart_list():
    """생성자 래퍼, 기본 동사."""
    return Endpoint(path="/carts").perform()


def gateway_items():
    """메서드 래퍼, 기본 동사."""
    return Gateway().call("/items")


def gateway_item_remove(item_id):
    """메서드 래퍼, enum 키워드 동사."""
    return Gateway().call(f"/items/{item_id}", method=HttpMethod.DELETE)
