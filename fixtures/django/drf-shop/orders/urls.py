"""주문 앱 URLconf다. DRF 라우터와 API 뷰를 검증한다.

루트에서 `include(("orders.urls", "orders"), namespace="orders")`로 포함하므로 여기서는
`app_name`을 두지 않는다.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter, SimpleRouter

from orders import views

# 목록·상세·확장 action과 API 루트·형식 접미사를 만드는 기본 라우터다.
router = DefaultRouter()
router.register(r"orders", views.OrderViewSet, basename="order")
router.register(r"invoices", views.InvoiceViewSet, basename="invoice")
router.register(r"carts", views.CartViewSet, basename="cart")

# 끝 슬래시 없는 단순 라우터다.
simple_router = SimpleRouter(trailing_slash=False)
simple_router.register(r"coupons", views.CouponViewSet, basename="coupon")

# 주문 URL 패턴이다.
urlpatterns = [
    path("summary/", views.order_summary),
    path("ping/", views.api_ping),
    path("status/<int:pk>/", views.OrderStatusView.as_view()),
    path("lines/", views.LineListCreate.as_view()),
    path("lines/<int:pk>/", views.LineDetail.as_view()),
    path("api/", include(router.urls)),
    path("v2/", include(simple_router.urls)),
]
