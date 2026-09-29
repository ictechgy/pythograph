"""Django 모델 필드 선언을 정적으로 읽는다.

필드 종류와 컬럼 규칙은 Django 5.2.17 소스로 확인했다(`docs/PERSISTENCE.md`):

- 일반 필드 컬럼은 `db_column` 또는 필드 이름(`Field.get_attname_column`).
- `ForeignKey`·`OneToOneField` 컬럼은 `db_column` 또는 `<이름>_id`(`ForeignKey.get_attname`).
- `ManyToManyField`는 컬럼이 없고 중간 테이블을 쓴다(`ManyToManyField.get_attname_column`은 None).
- `GenericForeignKey`·`GenericRelation`은 컬럼이 없는 가상 필드라 필드로 보지 않는다.

`django.`로 시작하는 외부 클래스 중 이름이 `Field`로 끝나거나 `ForeignKey`·`ForeignObject`인 것, 그리고 그것을
상속한 프로젝트 클래스만 필드로 본다. 다른 외부 필드 클래스는 컬럼 규칙을 확인할 수 없어 `unknown` 종류다.
폼 필드 모듈(점 경로에 `forms` 조각이 있는 `django.forms.CharField`·`django.contrib.postgres.forms.…` 등)의
클래스는 이름이 `Field`로 끝나도 모델 필드가 아니다(`django/forms/fields.py`는 컬럼을 만들지 않는다).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from pythograph.source.evaluate import UNKNOWN, Evaluator
from pythograph.source.symbols import ExternalSymbol, ProjectSymbol, Symbol, SymbolTable

#: 필드 클래스를 따라가는 최대 상속 깊이다.
MAX_FIELD_CLASS_DEPTH = 16

#: 관계 필드 이름(마지막 이름)과 종류다.
_RELATION_FIELDS = {
    "ForeignKey": "fk",
    "ForeignObject": "unknown",
    "OneToOneField": "o2o",
    "ManyToManyField": "m2m",
}


#: 폼 필드 모듈을 가리키는 점 경로 조각이다. ModelForm의 `forms.CharField`를 모델 필드로 오인하지 않기 위해 쓴다.
_FORM_MODULE_PART = "forms"


@dataclass(frozen=True)
class FieldSpec:
    """클래스 본문에서 읽은 필드 선언 하나.

    Attributes:
        name: 필드(속성) 이름.
        kind: `column`·`fk`·`o2o`·`m2m`·`unknown`.
        path: 선언한 모듈 경로(관계 대상 해석용).
        node: 위치로 쓸 대입 대상 노드.
        target: 관계 대상 식(`to` 인자). 관계가 아니면 None.
        options: 리터럴로 읽은 키워드 인자(`db_column`·`primary_key`·`related_name`·`related_query_name`·
            `db_table`·`parent_link`·`through_fields`). 리터럴이 아니면 `...`.
        through: `through` 인자 식(없으면 None).
    """

    name: str
    kind: str
    path: str
    node: ast.AST
    target: ast.expr | None
    options: dict[str, object]
    through: ast.expr | None


#: 리터럴로 읽는 필드 키워드 인자다.
_LITERAL_OPTIONS = (
    "symmetrical",
    "db_column",
    "primary_key",
    "related_name",
    "related_query_name",
    "db_table",
    "parent_link",
    "through_fields",
)


def field_kind(symbols: SymbolTable, symbol: Symbol | None, depth: int = 0) -> str | None:
    """호출 대상이 Django 필드 클래스면 종류를 돌려준다.

    Args:
        symbols: 이름 해석기.
        symbol: 호출 대상 해석 결과.
        depth: 상속 깊이.

    Returns:
        종류, 필드가 아니면 None.
    """
    if isinstance(symbol, ExternalSymbol):
        return _external_field_kind(symbol.dotted)
    if not isinstance(symbol, ProjectSymbol) or not isinstance(symbol.node, ast.ClassDef):
        return None
    if depth > MAX_FIELD_CLASS_DEPTH:
        return None
    for base in symbol.node.bases:
        kind = field_kind(symbols, symbols.resolve_expr(symbol.path, base), depth + 1)
        if kind is not None:
            return kind
    return None


def _external_field_kind(dotted: str) -> str | None:
    """외부 점 경로가 필드 클래스면 종류를 돌려준다.

    Args:
        dotted: 외부 점 경로.

    Returns:
        종류 또는 None.
    """
    module, _, name = dotted.rpartition(".")
    if _FORM_MODULE_PART in module.split("."):
        return None
    if not dotted.startswith("django."):
        return "unknown" if name.endswith("Field") else None
    if name in _RELATION_FIELDS:
        return _RELATION_FIELDS[name]
    return "column" if name.endswith("Field") else None


def read_field(symbols: SymbolTable, path: str, statement: ast.stmt) -> FieldSpec | None:
    """클래스 본문 문장이 필드 선언이면 읽는다.

    Args:
        symbols: 이름 해석기.
        path: 모듈 경로.
        statement: 클래스 본문 문장.

    Returns:
        필드 선언 또는 None.
    """
    target, value = _assignment(statement)
    if target is None or not isinstance(value, ast.Call):
        return None
    kind = field_kind(symbols, symbols.resolve_expr(path, value.func))
    if kind is None:
        return None
    keywords = {keyword.arg: keyword.value for keyword in value.keywords if keyword.arg}
    relation = kind in ("fk", "o2o", "m2m")
    relation_target = (value.args[0] if value.args else keywords.get("to")) if relation else None
    for position, option in _positional_options(kind):
        if len(value.args) > position and option not in keywords:
            keywords[option] = value.args[position]
    evaluator = Evaluator(symbols)
    options = {name: _option(evaluator, path, keywords[name]) for name in _LITERAL_OPTIONS if name in keywords}
    # `Field(verbose_name, name, …)`의 `name`은 필드 이름을 바꾼다(`Field.set_attributes_from_name`).
    renamed = _literal(keywords["name"]) if "name" in keywords else None
    name = renamed if isinstance(renamed, str) and renamed else target.id
    return FieldSpec(name, kind, path, target, relation_target, options, keywords.get("through"))


def _positional_options(kind: str) -> tuple[tuple[int, str], ...]:
    """필드 종류별로 이름을 가진 위치 인자(위치, 키워드 이름)를 돌려준다.

    Django 5.2.17 시그니처: `Field(verbose_name, name, primary_key, …)`,
    `ForeignKey(to, on_delete, related_name, related_query_name, …)`,
    `ManyToManyField(to, related_name, related_query_name, …)`, `OneToOneField(to, on_delete, to_field, …)`.

    Args:
        kind: 필드 종류.

    Returns:
        (위치, 키워드) 목록.
    """
    if kind == "fk":
        return ((2, "related_name"), (3, "related_query_name"))
    if kind == "m2m":
        return ((1, "related_name"), (2, "related_query_name"))
    if kind == "o2o":
        return ()
    return ((1, "name"), (2, "primary_key"))


def _option(evaluator: Evaluator, path: str, node: ast.expr) -> object:
    """필드 옵션 값을 리터럴 또는 모듈 상수로 읽는다.

    Args:
        evaluator: 상수 평가기.
        path: 모듈 경로.
        node: 값 식.

    Returns:
        값, 읽지 못하면 `...`.
    """
    literal = _literal(node)
    if literal is not ...:
        return literal
    value = evaluator.value(path, node)
    return ... if value is UNKNOWN else value


def _assignment(statement: ast.stmt) -> tuple[ast.Name | None, ast.expr | None]:
    """단순 이름 대입이면 (대상, 값)을 돌려준다.

    Args:
        statement: 문장.

    Returns:
        (대상 이름, 값) 또는 (None, None).
    """
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
        return statement.targets[0], statement.value
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target, statement.value
    return None, None


def _literal(node: ast.expr) -> object:
    """리터럴 식만 값으로 바꾼다.

    Args:
        node: 식.

    Returns:
        값, 리터럴이 아니면 `...`.
    """
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return ...


@dataclass(frozen=True)
class ExternalFieldSpec:
    """프로젝트 밖 모델(django.contrib)의 필드 하나.

    Attributes:
        name: 필드 이름.
        kind: `column`·`fk`·`o2o`·`m2m`.
        column: 컬럼 이름(m2m은 None).
        target: 관계 대상 라벨(`auth.Group`, 사용자 모델은 `AUTH_USER_MODEL`).
        related_name: 역방향 접근자 이름(없으면 None).
        related_query_name: 역방향 질의 이름(없으면 None).
        m2m_table: 추상 모델 필드의 중간 테이블 이름 뒤쪽(`groups`). 구체 모델이면 None.
    """

    name: str
    kind: str
    column: str | None = None
    target: str | None = None
    related_name: str | None = None
    related_query_name: str | None = None


def _columns(*names: str) -> tuple[ExternalFieldSpec, ...]:
    """일반 컬럼 필드 목록을 만든다.

    Args:
        names: 필드 이름(컬럼 이름과 같다).

    Returns:
        필드 목록.
    """
    return tuple(ExternalFieldSpec(name, "column", name) for name in names)


#: `AbstractBaseUser`의 필드다(`django/contrib/auth/base_user.py`).
_ABSTRACT_BASE_USER = _columns("password", "last_login")

#: `PermissionsMixin`의 필드다(`django/contrib/auth/models.py`).
_PERMISSIONS_MIXIN = (
    *_columns("is_superuser"),
    ExternalFieldSpec("groups", "m2m", None, "auth.Group", "user_set", "user"),
    ExternalFieldSpec("user_permissions", "m2m", None, "auth.Permission", "user_set", "user"),
)

#: `AbstractUser`의 필드다(`django/contrib/auth/models.py`).
_ABSTRACT_USER = (
    *_ABSTRACT_BASE_USER,
    *_PERMISSIONS_MIXIN,
    *_columns("username", "first_name", "last_name", "email", "is_staff", "is_active", "date_joined"),
)

#: 필드를 물려주는 외부 추상 모델(점 경로)이다. 오라클이 실제 Django 모델과 비교한다.
EXTERNAL_ABSTRACT_MODELS: dict[str, tuple[ExternalFieldSpec, ...]] = {
    "django.contrib.auth.models.AbstractUser": _ABSTRACT_USER,
    "django.contrib.auth.models.AbstractBaseUser": _ABSTRACT_BASE_USER,
    "django.contrib.auth.base_user.AbstractBaseUser": _ABSTRACT_BASE_USER,
    "django.contrib.auth.models.PermissionsMixin": _PERMISSIONS_MIXIN,
}


@dataclass(frozen=True)
class ExternalModel:
    """프로젝트 밖 구체 모델(django.contrib).

    Attributes:
        label: `앱라벨.모델`.
        table: 테이블 이름.
        fields: 필드(기본 키 포함).
        through: m2m 필드 이름 → (중간 테이블, 원본 쪽 컬럼, 대상 쪽 컬럼).
    """

    label: str
    table: str
    fields: tuple[ExternalFieldSpec, ...]
    through: dict[str, tuple[str, str, str]]


#: django.contrib 구체 모델이다. 값은 Django 5.2.17 설치본의 `_meta`와 오라클로 확인했다.
EXTERNAL_MODELS: dict[str, ExternalModel] = {
    model.label: model
    for model in (
        ExternalModel(
            "auth.Permission",
            "auth_permission",
            (
                *_columns("id", "name", "codename"),
                ExternalFieldSpec("content_type", "fk", "content_type_id", "contenttypes.ContentType"),
            ),
            {},
        ),
        ExternalModel(
            "auth.Group",
            "auth_group",
            (*_columns("id", "name"), ExternalFieldSpec("permissions", "m2m", None, "auth.Permission")),
            {"permissions": ("auth_group_permissions", "group_id", "permission_id")},
        ),
        ExternalModel(
            "auth.User",
            "auth_user",
            (*_columns("id"), *_ABSTRACT_USER),
            {
                "groups": ("auth_user_groups", "user_id", "group_id"),
                "user_permissions": ("auth_user_user_permissions", "user_id", "permission_id"),
            },
        ),
        ExternalModel("contenttypes.ContentType", "django_content_type", _columns("id", "app_label", "model"), {}),
        ExternalModel("sessions.Session", "django_session", _columns("session_key", "session_data", "expire_date"), {}),
        ExternalModel("sites.Site", "django_site", _columns("id", "domain", "name"), {}),
        ExternalModel(
            "admin.LogEntry",
            "django_admin_log",
            (
                *_columns("id", "action_time", "object_id", "object_repr", "action_flag", "change_message"),
                ExternalFieldSpec("user", "fk", "user_id", "AUTH_USER_MODEL"),
                ExternalFieldSpec("content_type", "fk", "content_type_id", "contenttypes.ContentType"),
            ),
            {},
        ),
    )
}

#: 외부 모델 클래스 점 경로 → 라벨이다(재수출 표기 포함).
EXTERNAL_MODEL_CLASSES: dict[str, str] = {
    "django.contrib.auth.models.User": "auth.User",
    "django.contrib.auth.models.Group": "auth.Group",
    "django.contrib.auth.models.Permission": "auth.Permission",
    "django.contrib.contenttypes.models.ContentType": "contenttypes.ContentType",
    "django.contrib.sessions.models.Session": "sessions.Session",
    "django.contrib.sites.models.Site": "sites.Site",
    "django.contrib.admin.models.LogEntry": "admin.LogEntry",
}
