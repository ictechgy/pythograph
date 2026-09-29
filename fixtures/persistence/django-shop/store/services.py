"""상점 서비스: QuerySet·관계 매니저·원시 SQL 사용."""

from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404

from reviews.models import Review
from store.models import Customer, Order, OrderItem, Product


def product_list():
    """분류 이름과 가격으로 거른 상품 목록."""
    products = (
        Product.objects.filter(category__name="Books", price__gte=10)
        .select_related("category")
        .order_by("-created")
    )
    return list(products.values("name", "category__name"))


def product_detail(pk):
    """상품과 태그·좋은 후기."""
    product = get_object_or_404(Product, pk=pk)
    tags = product.tags.all()
    reviews = product.review_set.filter(rating__gte=4)
    return product, tags, reviews


def best_customers():
    """주문이 많은 고객."""
    return (
        Customer.objects.annotate(order_count=Count("orders"))
        .filter(order_count__gt=5)
        .values("email", "order_count")
    )


def restock(product_id, amount):
    """재고를 늘린다."""
    Product.objects.filter(pk=product_id).update(stock=F("stock") + amount)


def search(term):
    """이름이나 태그로 찾는다."""
    return Product.objects.filter(Q(name__icontains=term) | Q(tags__name=term)).distinct()


def staff_reviews():
    """직원이 쓴 후기."""
    staff = User.objects.filter(is_staff=True)
    return Review.objects.filter(author__in=staff, product__category__parent__isnull=True)


def order_report():
    """원시 SQL 보고서."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT o.id, COUNT(*) FROM store_order o JOIN store_orderitem i ON i.order_id = o.id GROUP BY o.id"
        )
        return cursor.fetchall()


def legacy_lookup(name):
    """raw() 질의."""
    return Product.objects.raw("SELECT * FROM store_product WHERE name = %s", [name])


def count_rows(table):
    """테이블 이름을 인자로 받는 원시 SQL(정적으로 알 수 없다)."""
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        return cursor.fetchone()


def everything(model):
    """모델을 인자로 받는다(정적으로 알 수 없다)."""
    return model.objects.all()


class OrderService:
    """주문 생성."""

    def place(self, customer, lines):
        """주문과 항목을 만든다."""
        order = Order.objects.create(customer=customer, status="new")
        for product, quantity in lines:
            OrderItem.objects.create(order=order, product=product, quantity=quantity, unit_price=product.price)
        order.status = "placed"
        order.save(update_fields=["status"])
        return order.items.count()
