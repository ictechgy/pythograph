"""상점 앱의 합성 모델이다. 테이블은 `store_<모델>`이다."""

from django.db import models


class Customer(models.Model):
    """주문하는 고객이다."""

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=100)


class Product(models.Model):
    """판매 상품이다."""

    sku = models.CharField(max_length=32, unique=True)
    title = models.CharField(max_length=200)
    price_cents = models.IntegerField()
    active = models.BooleanField(default=True)


class Order(models.Model):
    """고객 주문이다."""

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="orders")
    status = models.CharField(max_length=16, default="open")
    created_at = models.DateTimeField(auto_now_add=True)


class OrderLine(models.Model):
    """주문 줄이다."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.IntegerField()


class AuditEntry(models.Model):
    """주문 상태 변경 감사 기록이다."""

    action = models.CharField(max_length=32)
    order_id = models.IntegerField()
