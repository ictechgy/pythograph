"""상점 앱 URLconf다. viewset은 SimpleRouter로 등록한다."""

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from store import views

# 주문 viewset 라우터다.
router = SimpleRouter()
router.register(r"orders", views.OrderViewSet, basename="order")

# 상점 URL 패턴이다.
urlpatterns = [
    path("products/", views.ProductListView.as_view()),
    path("checkout/", views.CheckoutView.as_view()),
    path("customers/<int:pk>/summary/", views.customer_summary),
    path("", include(router.urls)),
]
