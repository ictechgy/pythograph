"""이름 규칙 벡터용 도서관 모델. 기본 이름·db_column·M2M·상속·프록시·명시 이름을 담는다."""

from django.conf import settings
from django.db import models


class Timestamped(models.Model):
    """추상 기반 모델. 필드는 자식 테이블에 복사된다."""

    created = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField("Updated", "modified_on", auto_now=True)

    class Meta:
        abstract = True


class Author(Timestamped):
    """저자."""

    name = models.CharField(max_length=100)
    account = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)


class Tag(models.Model):
    """태그. 기본 키를 직접 둔다."""

    slug = models.SlugField(primary_key=True)


class Book(Timestamped):
    """책."""

    title = models.CharField(max_length=200)
    isbn = models.CharField(max_length=13, db_column="ISBN_CODE")
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
    editor = models.ForeignKey("library.Author", null=True, on_delete=models.SET_NULL, db_column="editor_ref")
    tags = models.ManyToManyField(Tag, related_name="books")
    shelves = models.ManyToManyField("Shelf", db_table="book_shelf_links")


class Shelf(models.Model):
    """서가. 명시 테이블 이름을 쓴다."""

    label = models.CharField(max_length=10)

    class Meta:
        db_table = "legacy_shelves"


class ArchivedDocument(models.Model):
    """따옴표 네임스페이스 테이블(PostgreSQL 스키마 관례)."""

    body = models.TextField()
    tags = models.ManyToManyField(Tag)

    class Meta:
        db_table = '"archive"."documents"'


class FeaturedBook(Book):
    """프록시 모델. 부모 테이블을 쓴다."""

    class Meta:
        proxy = True


class Place(models.Model):
    """다중 테이블 상속 부모."""

    address = models.CharField(max_length=80)


class Restaurant(Place):
    """다중 테이블 상속 자식. `place_ptr_id`가 기본 키다."""

    serves_pizza = models.BooleanField(default=False)


class Bar(Restaurant):
    """두 단계 상속."""

    happy_hour = models.BooleanField(default=False)


class Kiosk(Place):
    """명시한 부모 연결 필드."""

    base = models.OneToOneField(Place, on_delete=models.CASCADE, parent_link=True, related_name="kiosk")
    number = models.IntegerField()


class Person(models.Model):
    """자기 참조 M2M."""

    name = models.CharField(max_length=40)
    friends = models.ManyToManyField("self")


class Club(models.Model):
    """through 모델 M2M."""

    title = models.CharField(max_length=40)
    members = models.ManyToManyField(Person, through="Membership", related_name="clubs")


class Membership(models.Model):
    """중간 모델."""

    person = models.ForeignKey(Person, on_delete=models.CASCADE)
    club = models.ForeignKey(Club, on_delete=models.CASCADE)
    since = models.DateField()


class AVeryLongModelNameThatWillDefinitelyExceedOracle(models.Model):
    """긴 기본 이름. Oracle에서 잘린다."""

    extraordinarily_long_column_name_over_thirty = models.IntegerField()
    labels = models.ManyToManyField(Tag, related_name="+")
