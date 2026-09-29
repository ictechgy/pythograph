"""카탈로그 앱 URLconf다. `app_name`으로 이름공간을 선언한다."""

from django.urls import include, path

from catalog import views

# 이 URLconf의 앱 이름공간이다.
app_name = "catalog"

# 두 번째 단계 중첩 include로 넘기는 목록이다.
section_patterns = [
    path("<int:pk>/", views.section_detail),
]

# 카탈로그 URL 패턴이다. `items/<str:key>/`가 `items/featured/`보다 먼저라 뒤 패턴은 가려진다.
urlpatterns = [
    path("items/", views.item_list),
    path("items/<str:key>/", views.item_by_key),
    path("items/featured/", views.featured_items),
    path("items/<int:pk>/edit/", views.ItemEditView.as_view()),
    path("products/", views.ProductListView.as_view()),
    path("products/<uuid:id>/", views.ProductDetailView.as_view()),
    path("brands/<name>/", views.BrandView.as_view()),
    path("tags/<slug:slug>/", views.tag_detail),
    path("reviews/", views.submit_review),
    path("limited/", views.LimitedView.as_view()),
    path("sections/", include(section_patterns)),
]
