"""DRF 직렬화기다."""

from rest_framework import serializers

from store.models import Order, Product


class OrderSerializer(serializers.ModelSerializer):
    """주문 직렬화기다."""

    class Meta:
        """직렬화 대상이다."""

        model = Order
        fields = ["id", "status", "customer", "created_at"]


class ProductSerializer(serializers.ModelSerializer):
    """상품 직렬화기다."""

    class Meta:
        """직렬화 대상이다."""

        model = Product
        fields = ["id", "sku", "title", "price_cents"]
