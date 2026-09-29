"""Django persistence 규칙 단위 테스트(합성 프로젝트)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests.conftest import relation_rows, schema_document

#: 합성 프로젝트를 만드는 함수 타입이다.
MakeProject = Callable[[dict[str, str]], Path]

#: 기본 설정이다.
SETTINGS = """
INSTALLED_APPS = ["django.contrib.auth", "app"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "db"}}
"""

#: 기본 모델이다.
MODELS = """
from django.conf import settings
from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=10)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_query_name="writer")


class Tag(models.Model):
    label = models.CharField(max_length=10)


class Book(models.Model):
    title = models.CharField(max_length=10)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
    tags = models.ManyToManyField(Tag)
    created = models.DateTimeField()
    stock = models.IntegerField()

    def tag_labels(self):
        return self.tags.values_list("label", flat=True)

    def writer(self):
        return self.author.name

    def siblings(self):
        return self.author.books.exclude(pk=self.pk)
"""


def _project(make_project: MakeProject, views: str, models: str = MODELS, settings: str = SETTINGS) -> Path:
    """Django 합성 프로젝트를 만든다.

    Args:
        make_project: 프로젝트 생성 fixture.
        views: `app/views.py` 내용.
        models: `app/models.py` 내용.
        settings: 설정 모듈 내용.

    Returns:
        프로젝트 루트.
    """
    return make_project(
        {
            "manage.py": 'import os\nos.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")\n',
            "proj/__init__.py": "",
            "proj/settings.py": settings,
            "app/__init__.py": "",
            "app/models.py": models,
            "app/views.py": views,
            "requirements.txt": "Django==5.2.17\n",
        }
    )


def _rows(make_project: MakeProject, views: str, **kwargs: str) -> set[tuple[object, ...]]:
    """views 함수의 사실만 (channel, method, dynamic) 집합으로 돌려준다.

    Args:
        make_project: 프로젝트 생성 fixture.
        views: `app/views.py` 내용.
        kwargs: `_project` 추가 인자.

    Returns:
        요약 집합.
    """
    document = schema_document(_project(make_project, views, **kwargs))
    return {row[:3] for row in relation_rows(document) if str(row[3] or "").startswith("app/views.py")}


def test_lookups_follow_joins_and_reverse_relations(make_project: MakeProject) -> None:
    """조회식은 외래 키·역관계·M2M을 따라 조인하고 attname·pk·조회 이름에서 멈춘다."""
    rows = _rows(
        make_project,
        """
from django.contrib.auth.models import User

from app.models import Author, Book


def run(user):
    Book.objects.filter(author__name="x", author_id=1, pk=2, author__in=[1], tags__label="t")
    Author.objects.filter(books__title__startswith="a", user__username="u")
    User.objects.filter(writer__name="w")
""",
    )
    assert {
        ("app_book", None, False),
        ("app_book", "author_id", False),
        ("app_book", "id", False),
        ("app_author", "name", False),
        ("app_book_tags", "book_id", False),
        ("app_book_tags", "tag_id", False),
        ("app_tag", "label", False),
        ("app_author", None, False),
        ("app_book", "title", False),
        ("auth_user", "username", False),
    } <= rows


def test_values_order_and_expression_arguments(make_project: MakeProject) -> None:
    """values·order_by·F·Q·집계·Case/When·Extract의 필드 경로를 읽고 주석 이름은 건너뛴다."""
    rows = _rows(
        make_project,
        """
from django.db import models
from django.db.models import Case, Count, Extract, F, OuterRef, Q, Value, When
from django.db.models.functions import Coalesce

from app.models import Book


def run():
    qs = Book.objects.annotate(n=Count("tags")).filter(n__gt=1)
    qs.values("title", total=models.Sum("stock")).order_by("-created", "?", F("stock").desc())
    Book.objects.filter(~Q(title="a") | Q(author__name="b")).distinct("title").only("stock").defer("created")
    Book.objects.annotate(
        kind=Case(When(stock__gt=0, then="title"), default=Value("none")),
        year=Extract("created", "year"),
        label=Coalesce("title", Value("-")),
        outer=OuterRef("pk"),
    ).dates("created", "month")
    Book.objects.select_related("author").prefetch_related("tags").latest("created")
    Book.objects.aggregate(models.Max("stock"))
""",
    )
    assert {
        ("app_book_tags", None, False),
        ("app_book", "title", False),
        ("app_book", "stock", False),
        ("app_book", "created", False),
        ("app_author", "name", False),
        ("app_book", "author_id", False),
        ("app_tag", None, False),
    } <= rows
    assert not any(row[1] in ("n", "total", "year", "month") for row in rows)


def test_write_methods_and_field_lists(make_project: MakeProject) -> None:
    """create·update·get_or_create(defaults)·bulk 목록 인자·in_bulk·인스턴스 save/refresh를 읽는다."""
    rows = _rows(
        make_project,
        """
from django.db.models import F

from app.models import Author, Book


async def run(author):
    book = Book.objects.create(title="t", author=author, stock=1)
    Book.objects.filter(stock=0).update(stock=F("stock") + 1)
    Book.objects.get_or_create(title="a", defaults={"stock": 3})
    Book.objects.update_or_create(title="b", create_defaults={"created": None})
    Book.objects.bulk_update([book], ["stock"])
    Book.objects.bulk_create([book], update_fields=["title"], unique_fields=["id"])
    Book.objects.in_bulk([1], field_name="title")
    book.save(update_fields=["title"])
    book.refresh_from_db(fields=["stock"])
    book.delete()
    first = await Book.objects.aget(pk=1)
    first.author.name
    Author(name="x")
""",
    )
    assert {
        ("app_book", "title", False),
        ("app_book", "author_id", False),
        ("app_book", "stock", False),
        ("app_book", "created", False),
        ("app_book", "id", False),
        ("app_author", None, False),
        ("app_author", "name", False),
    } <= rows


def test_instances_and_related_managers(make_project: MakeProject) -> None:
    """self·주석 매개변수·반복 변수·get_object_or_404·첨자·재대입으로 인스턴스를 증명한다."""
    rows = _rows(
        make_project,
        """
from django.shortcuts import get_object_or_404

from app.models import Author, Book


def run(author: "Author", other: Author, pk):
    author.books.all()
    for book in Book.objects.filter(stock__gt=0):
        book.tags.add(1)
    found = get_object_or_404(Book.objects.select_related("author"), pk=pk)
    found.author
    get_object_or_404(Author, name="x")
    first = Book.objects.all()[0]
    first.tags.clear()
    qs = Book.objects.all()
    qs = qs.filter(title="x")
    qs.exclude(stock=1)
    if pk:
        ambiguous = Book.objects.all()
    else:
        ambiguous = Author.objects.all()
    ambiguous.filter(created=None)
""",
    )
    assert {
        ("app_book", "author_id", False),
        ("app_book_tags", "tag_id", False),
        ("app_tag", None, False),
        ("app_author", None, False),
        ("app_author", "name", False),
        ("app_book", "title", False),
        ("app_book", "stock", False),
    } <= rows
    assert ("app_book", "created", False) not in rows


def test_model_methods_use_self(make_project: MakeProject) -> None:
    """모델 메서드의 self로 관계 매니저·정방향 관계를 읽는다."""
    document = schema_document(_project(make_project, "x = 1\n"))
    rows = relation_rows(document)
    assert ("app_book_tags", "book_id", False, "app/models.py#Book.tag_labels") in rows
    assert ("app_tag", "label", False, "app/models.py#Book.tag_labels") in rows
    assert ("app_author", None, False, "app/models.py#Book.writer") in rows
    assert ("app_book", "author_id", False, "app/models.py#Book.siblings") in rows


def test_unresolved_models_lookups_and_sql_become_dynamic(make_project: MakeProject) -> None:
    """모르는 매니저·조회식·** 인자·원시 SQL 조각은 dynamic 사실과 한계다."""
    document = schema_document(
        _project(
            make_project,
            """
from django.db import connection
from django.db.models.expressions import RawSQL

from app.models import Book


def run(model, filters, table, sql):
    model.objects.all()
    Book.objects.filter(nickname="x", author__missing="y")
    Book.objects.filter(**filters)
    Book.objects.raw(sql)
    Book.objects.annotate(x=RawSQL("SELECT 1 FROM app_tag", []))
    Book.objects.extra(tables=["app_tag", table], where=["1=1"])
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM app_book WHERE id = %s", [1])
        cursor.execute(sql)
""",
        )
    )
    rows = {row[:3] for row in relation_rows(document)}
    assert {
        ("model.objects", None, True),
        ("nickname", None, True),
        ("author__missing", None, True),
        ("filters", None, True),
        ("sql", None, True),
        ("table", None, True),
        ("app_tag", None, False),
        ("app_book", None, False),
    } <= rows
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(item.startswith("skipped-sql-fragments:") for item in limitations)
    assert any(item.startswith("unresolved-sql-arguments:") for item in limitations)


def test_managers_user_model_and_app_registry(make_project: MakeProject) -> None:
    """선언한 매니저·as_manager·from_queryset·get_user_model·리터럴 apps.get_model을 푼다."""
    models = """
from django.db import models


class BookQuerySet(models.QuerySet):
    pass


class PublishedManager(models.Manager):
    pass


class Book(models.Model):
    title = models.CharField(max_length=10)
    published = PublishedManager()
    live = BookQuerySet.as_manager()
    custom = models.Manager.from_queryset(BookQuerySet)()
"""
    rows = _rows(
        make_project,
        """
from django.apps import apps
from django.contrib.auth import get_user_model

from app.models import Book


def run(name):
    Book.published.filter(title="a")
    Book.live.all()
    Book.custom.all()
    Book.objects.all()
    get_user_model().objects.filter(email="e")
    apps.get_model("app", "Book").objects.all()
    apps.get_model("app.Book").objects.all()
    apps.get_model(name).objects.all()
""",
        models=models,
    )
    assert {
        ("app_book", "title", False),
        ("auth_user", "email", False),
        ("apps.get_model(name).objects", None, True),
    } <= rows
    assert ("Book.objects", None, True) not in rows


def test_app_labels_follow_installed_apps(make_project: MakeProject) -> None:
    """AppConfig 자동 선택·default·label·클래스 경로·+=·append·조건부 원소로 앱 라벨을 푼다."""
    root = make_project(
        {
            "manage.py": 'import os\nos.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")\n',
            "proj/__init__.py": "",
            "proj/settings.py": """
import os

INSTALLED_APPS = ["one", "django.contrib.admin.apps.SimpleAdminConfig"]
INSTALLED_APPS += ["two.apps.TwoConfig"]
INSTALLED_APPS.append("three")
INSTALLED_APPS.insert(0, "four")
if os.environ.get("X"):
    INSTALLED_APPS = INSTALLED_APPS + ["five"]
DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql"}}
""",
            "one/__init__.py": "",
            "one/apps.py": """
from django.apps import AppConfig


class Base(AppConfig):
    label = "first"
    default = False


class OneConfig(Base):
    name = "one"
    default = True


class Other(AppConfig):
    name = "one"
    default = False
""",
            "one/models.py": "from django.db import models\n\nclass A(models.Model):\n    x = models.IntegerField()\n",
            "two/__init__.py": "",
            "two/apps.py": "from django.apps import AppConfig\n\nclass TwoConfig(AppConfig):\n    name = 'two'\n",
            "two/models.py": "from django.db import models\n\nclass B(models.Model):\n    pass\n",
            "three/__init__.py": "",
            "three/apps.py": """
from django.apps import AppConfig


class C1(AppConfig):
    name = "three"
    label = "tri"
    default = True


class C2(AppConfig):
    name = "three"
""",
            "three/models.py": "from django.db import models\n\nclass C(models.Model):\n    pass\n",
            "four/__init__.py": "",
            "four/models.py": "from django.db import models\n\nclass D(models.Model):\n    pass\n",
            "five/__init__.py": "",
            "five/models.py": (
                "from django.db import models\n\nclass E(models.Model):\n    class Meta:\n        app_label = 'fifth'\n"
            ),
            "stray/models.py": "from django.db import models\n\nclass F(models.Model):\n    pass\n",
        }
    )
    document = schema_document(root)
    channels = {row[0] for row in relation_rows(document)}
    assert {"first_a", "two_b", "tri_c", "four_d", "fifth_e", "F"} <= channels
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(item.startswith("django-app-label-unresolved: 1 ") for item in limitations)
    assert any(item.startswith("persistence-framework-version-unknown:") for item in limitations)


def test_incomplete_installed_apps_only_trust_models_modules(make_project: MakeProject) -> None:
    """목록이 불완전하면 알려진 앱의 `models` 모듈만 인정한다."""
    root = _project(
        make_project,
        "x = 1\n",
        settings="""
import environ

INSTALLED_APPS = ["app"] + environ.extra_apps()
DATABASES = {"default": environ.db()}
""",
        models=(
            "from django.db import models\n\nclass A(models.Model):\n"
            "    long_field_name_for_the_oracle_backend_limit = models.IntegerField()\n"
        ),
    )
    (root / "app" / "sub").mkdir()
    (root / "app" / "sub" / "__init__.py").write_text("")
    (root / "app" / "sub" / "models.py").write_text(
        "from django.db import models\n\nclass S(models.Model):\n    pass\n"
    )
    document = schema_document(root)
    rows = {row[:3] for row in relation_rows(document)}
    assert ("app_a", None, False) in rows
    assert ("S", None, True) in rows
    assert ("long_field_name_for_the_oracle_backend_limit", None, True) in rows
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(item.startswith("django-database-backend-unknown:") for item in limitations)


def test_backends_from_multiple_databases(make_project: MakeProject) -> None:
    """여러 데이터베이스의 백엔드가 모두 같은 이름만 정적이다."""
    models = """
from django.db import models


class AnExtremelyLongModelNameForTruncation(models.Model):
    pass
"""
    settings = """
INSTALLED_APPS = ["app"]
DATABASES = {
    "default": {"ENGINE": "django.db.backends.postgresql"},
    "replica": {"ENGINE": "django.contrib.gis.db.backends.postgis"},
}
"""
    rows = {
        row[0]
        for row in relation_rows(schema_document(_project(make_project, "x = 1\n", models=models, settings=settings)))
    }
    assert "app_anextremelylongmodelnamefortruncation" in rows
    oracle = settings.replace("django.contrib.gis.db.backends.postgis", "django.db.backends.oracle")
    rows = {
        row[:3]
        for row in relation_rows(schema_document(_project(make_project, "x = 1\n", models=models, settings=oracle)))
    }
    assert ("AnExtremelyLongModelNameForTruncation", None, True) in rows
    changed = settings + 'DATABASES["default"]["ENGINE"] = "django.db.backends.mysql"\n'
    document = schema_document(_project(make_project, "x = 1\n", models=models, settings=changed))
    assert any(str(item).startswith("django-database-backend-unknown:") for item in document["limitations"])  # type: ignore[attr-defined]


def test_field_declarations_and_inheritance(make_project: MakeProject) -> None:
    """추상 Meta 상속·프록시의 프록시·다중 테이블 상속·사용자 필드·외부 필드·M2M through를 선언 사실로 낸다."""
    models = """
from django.db import models
from thirdparty.fields import FancyField
from thirdparty.models import TimeStampedModel

COLUMN = "dyn"
PREFIX = "kids_"


class LowerCaseField(models.CharField):
    pass


class Base(models.Model):
    note = models.TextField()

    class Meta:
        abstract = True
        db_table = "ignored_for_children_via_meta_inheritance"


class Child(Base):
    class Meta(Base.Meta):
        db_table = PREFIX + "children"


class Plain(Base):
    code = LowerCaseField(max_length=3, primary_key=True)
    fancy = FancyField()
    computed = models.IntegerField(db_column=COLUMN)
    opaque = models.IntegerField(db_column=column_name())
    ref = models.ForeignKey("Child", related_name="%(app_label)s_%(class)s_refs", on_delete=models.CASCADE)


class ProxyChild(Child):
    class Meta:
        proxy = True


class ProxyOfProxy(ProxyChild):
    class Meta:
        proxy = True


class Group(models.Model):
    members = models.ManyToManyField(Plain, through="Membership", through_fields=("group", "plain"))
    unknown = models.ManyToManyField(Plain, through=make_through())
    targets = models.ManyToManyField("missing.Model")


class Membership(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE)
    plain = models.ForeignKey(Plain, on_delete=models.CASCADE)


class Stamped(TimeStampedModel):
    title = models.CharField(max_length=5)
"""
    document = schema_document(_project(make_project, "x = 1\n", models=models))
    rows = {row[:3] for row in relation_rows(document)}
    assert {
        ("kids_children", "note", False),
        ("ignored_for_children_via_meta_inheritance", "note", False),
        ("ignored_for_children_via_meta_inheritance", "code", False),
        ("ignored_for_children_via_meta_inheritance", "ref_id", False),
        ("app_membership", "group_id", False),
        ("ignored_for_children_via_meta_inheritance", "dyn", False),
        ("opaque", None, True),
        ("unknown", None, True),
        ("app_group_targets", "group_id", False),
        ("app_stamped", "title", False),
    } <= rows
    assert ("ignored_for_children_via_meta_inheritance", "fancy", False) not in rows
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(item.startswith("unmodeled-orm-mappings:") for item in limitations)


def test_missing_settings_reports_unresolved_labels(make_project: MakeProject) -> None:
    """설정 모듈이 없으면 기본 이름 테이블은 dynamic이고 명시 이름은 정적이다."""
    root = make_project(
        {
            "app/__init__.py": "",
            "app/models.py": """
from django.db import models


class Named(models.Model):
    class Meta:
        db_table = "named"


class Unnamed(models.Model):
    pass
""",
        }
    )
    document = schema_document(root)
    rows = {row[:3] for row in relation_rows(document)}
    assert ("named", None, False) in rows
    assert ("Unnamed", None, True) in rows


def test_default_related_name_and_symmetrical_self_relation(make_project: MakeProject) -> None:
    """`Meta.default_related_name`은 역관계 이름이 되고, 대칭 자기 참조 M2M에는 역관계가 없다."""
    models = """
from django.db import models


class Shelf(models.Model):
    pass


class Item(models.Model):
    shelf = models.ForeignKey(Shelf, on_delete=models.CASCADE)
    peers = models.ManyToManyField("self")
    followers = models.ManyToManyField("self", symmetrical=False, related_name="following")

    class Meta:
        default_related_name = "items"
"""
    rows = _rows(
        make_project,
        """
from app.models import Item, Shelf


def run(shelf: Shelf, item: Item):
    Shelf.objects.filter(items__id=1)
    shelf.items.all()
    Item.objects.filter(item__id=2)
    item.following.all()
""",
        models=models,
    )
    assert {("app_item", "shelf_id", False), ("app_item_followers", "from_item_id", False)} <= rows
    assert ("item__id", None, True) in rows


def test_starred_apps_unpacked_settings_and_non_model_fields(make_project: MakeProject) -> None:
    """`*LOCAL_APPS` 원소를 풀고, 구조 분해 재대입은 목록을 불완전하게 만들며, DRF 직렬화기는 모델이 아니다."""
    settings = """
LOCAL_APPS = ["app"]
INSTALLED_APPS = ["django.contrib.auth", *LOCAL_APPS]
INSTALLED_APPS, MIDDLEWARE = setup(INSTALLED_APPS, [])
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3"}}
"""
    root = _project(make_project, "x = 1\n", settings=settings)
    (root / "app" / "extra").mkdir()
    (root / "app" / "extra" / "__init__.py").write_text("")
    (root / "app" / "extra" / "models.py").write_text(
        "from django.db import models\n\nclass Nested(models.Model):\n    pass\n"
    )
    (root / "app" / "serializers.py").write_text(
        "from rest_framework import serializers\n\n"
        "class BookSerializer(serializers.Serializer):\n    title = serializers.CharField()\n\n"
        "def build():\n    return BookSerializer(data={}, title='x')\n"
    )
    rows = {row[:3] for row in relation_rows(schema_document(root))}
    assert ("app_book", "title", False) in rows
    assert ("Nested", None, True) in rows
    assert not any("BookSerializer" in str(row[0]) or row[0] in ("data", "title") for row in rows)


def test_review_findings_annotated_meta_prefetch_extra_and_unknown_fields(make_project: MakeProject) -> None:
    """주석 대입 Meta·AppConfig, Prefetch 경로, extra(order_by=), 외부 필드, create의 관계 건너기 이름을 처리한다."""
    models = """
from django.db import models
from thirdparty.fields import FancyField


class Tag(models.Model):
    label = models.CharField(max_length=5)


class Entry(models.Model):
    tags = models.ManyToManyField(Tag)
    fancy = FancyField()
    title = models.CharField(max_length=5)

    class Meta:
        db_table: str = "legacy_entries"


class Base(models.Model):
    class Meta:
        abstract: bool = True
"""
    document = schema_document(
        _project(
            make_project,
            """
from django.db.models import Prefetch

from app.models import Entry, Tag


def run():
    Entry.objects.prefetch_related(Prefetch("tags", queryset=Tag.objects.all()))
    Entry.objects.extra(select={"n": "1"}, order_by=["-title", "n", "legacy_entries.title"])
    Entry.objects.filter(fancy="x")
    Entry.objects.create(tags__label="x")
""",
            models=models,
        )
    )
    rows = {row[:3] for row in relation_rows(document)}
    assert {
        ("legacy_entries", "title", False),
        ("legacy_entries_tags", "entry_id", False),
        ("app_tag", None, False),
        ("fancy", None, True),
        ("tags__label", None, True),
    } <= rows
    assert not any(row[0] == "app_base" for row in rows)
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert "skipped-sql-fragments: 2 QuerySet.extra() SQL fragments were not read" in limitations
