"""상점 모델."""

from django.conf import settings
from django.db import models
from django.db.models import Sum


class Category(models.Model):
    """상품 분류(자기 참조)."""

    name = models.CharField(max_length=60)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="children")


class Tag(models.Model):
    """상품 태그."""

    name = models.CharField(max_length=30, unique=True)


class Product(models.Model):
    """상품."""

    name = models.CharField(max_length=120)
    sku = models.CharField(max_length=20, db_column="sku_code")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    price = models.DecimalField(max_digits=8, decimal_places=2)
    stock = models.IntegerField(default=0)
    tags = models.ManyToManyField(Tag, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    def in_category(self, name):
        """같은 분류 이름인지 본다."""
        return self.category.name == name


class Customer(models.Model):
    """고객."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    email = models.EmailField()


class Order(models.Model):
    """주문."""

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="orders")
    status = models.CharField(max_length=20, default="new")
    placed_at = models.DateTimeField(null=True)

    def quantity(self):
        """주문 수량 합계를 구한다."""
        return self.items.aggregate(total=Sum("quantity"))["total"]


class OrderItem(models.Model):
    """주문 항목."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=8, decimal_places=2)
