"""Django ORM 사용 → relation-use 사실.

값의 출처를 구문으로 증명할 때만 모델에 귀속한다(형 검사기 없음, 실행 없음):

- 모델 클래스: 모델 클래스 이름, `get_user_model()`, 리터럴 인자의 `apps.get_model(...)`.
- QuerySet: 모델의 매니저(`objects`, 선언한 매니저, `_default_manager`), QuerySet 메서드 사슬, 인스턴스의
  관계 매니저(정방향 M2M, 역방향 FK·M2M), 그런 값을 유일하게 대입한 지역 이름.
- 인스턴스: 모델 생성자, `get`·`first`·`create` 등, `get_object_or_404`, QuerySet 반복 변수, 모델 메서드의 `self`,
  모델로 주석을 단 매개변수, 정방향 FK·일대일 접근.

사실: 매니저·관계 매니저 접근은 관계 사실(다중 테이블 상속이면 부모 테이블도), 조회식(`field__rel__x`)·
`values`·`order_by`·`F`·`Q`·집계 인자·`create`/`update` 키워드는 컬럼 사실과 조인한 테이블의 관계 사실이다.
`raw()`·`RawSQL`·`extra(tables=)`는 SQL로 읽는다. 풀지 못한 모델·매니저·조회식은 dynamic 사실과
`dynamic-relation-names:` 한계로 남긴다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace

from pythograph.persistence.django.catalog import DjangoCatalog, DjangoModel, ResolvedField, ReverseRelation
from pythograph.persistence.location import fact_location
from pythograph.persistence.model import PersistenceExtraction
from pythograph.persistence.scope import ModuleScopes
from pythograph.persistence.sqltext import SqlText, expression_text
from pythograph.routes.model import Location
from pythograph.source.symbols import ExternalSymbol, Symbol, SymbolTable

#: 값 출처를 따라가는 최대 깊이다.
MAX_CLASSIFY_DEPTH = 24

#: QuerySet을 돌려주는 메서드다(`django/db/models/query.py` `QuerySet`).
QUERYSET_METHODS = frozenset(
    {
        "all", "filter", "exclude", "order_by", "values", "values_list", "only", "defer", "select_related",
        "prefetch_related", "annotate", "alias", "distinct", "reverse", "none", "using", "select_for_update",
        "union", "intersection", "difference", "extra", "complex_filter", "dates", "datetimes", "raw",
    }
)  # fmt: skip

#: 인스턴스 하나를 돌려주는 메서드다.
INSTANCE_METHODS = frozenset({"get", "first", "last", "earliest", "latest", "create"})

#: 그 밖에 인자를 읽는 QuerySet 메서드다.
_OTHER_METHODS = frozenset(
    {
        "get_or_create", "update_or_create", "update", "delete", "count", "exists", "aggregate", "in_bulk",
        "bulk_create", "bulk_update", "iterator", "explain", "contains",
    }
)  # fmt: skip

#: 키워드 인자가 조회식인 메서드다(`defaults`·`create_defaults` 제외).
_LOOKUP_KWARGS = frozenset({"filter", "exclude", "get", "get_or_create", "update_or_create"})

#: 키워드 인자가 필드 이름인 메서드다.
_FIELD_KWARGS = frozenset({"create", "update"})

#: 위치 문자열 인자가 필드 경로인 메서드다.
_PATH_ARGS = frozenset(
    {"order_by", "values", "values_list", "only", "defer", "select_related", "prefetch_related", "distinct",
     "latest", "earliest"}
)  # fmt: skip

#: 첫 위치 인자만 필드 경로인 메서드다.
_FIRST_PATH_ARG = frozenset({"dates", "datetimes"})

#: 키워드 인자가 표현식이고 그 이름이 주석(annotation)이 되는 메서드다.
_EXPRESSION_KWARGS = frozenset({"annotate", "alias", "aggregate", "values"})

#: 행(dict·tuple)을 돌려 인스턴스가 아닌 QuerySet을 만드는 메서드다.
_ROW_METHODS = frozenset({"values", "values_list"})

#: 인스턴스에서 테이블을 쓰는 메서드와 컬럼 목록 인자다.
_INSTANCE_METHODS = {"save": "update_fields", "delete": None, "refresh_from_db": "fields"}

#: 첫 위치 문자열만 필드인 식 클래스다(두 번째 문자열은 종류·정렬 이름이다).
_FIRST_ONLY_EXPRESSIONS = frozenset(
    {"Extract", "Trunc", "Collate", "ArrayAgg", "StringAgg", "JSONBAgg", "BitAnd", "BitOr", "BoolAnd", "BoolOr"}
)

#: 필드 인자를 받지 않는 식 클래스다.
_OPAQUE_EXPRESSIONS = frozenset({"Value", "RawSQL", "Subquery", "Exists", "OuterRef"})

#: 문자열을 필드로 읽는 식 키워드 인자다.
_EXPRESSION_KEYWORDS = frozenset({"then", "default", "order_by", "partition_by", "filter", "expression"})

#: 컬럼 규칙을 모르는 외부 필드 한계 문장이다.
_UNKNOWN_FIELD_REASON = "{count} fields use third-party field classes whose column rule is not verified"

#: `extra()` SQL 조각 한계 문장이다.
_EXTRA_FRAGMENTS = "{count} QuerySet.extra() SQL fragments were not read"

#: Django 앱 레지스트리의 외부 점 경로다.
_APPS_REGISTRY = ("django.apps.apps", "django.apps.registry.apps")

#: 필드 뒤에 올 수 있는 조회·변환 이름이다(관계 필드 바로 뒤에서 조인이 아님을 판정한다).
LOOKUPS = frozenset(
    {
        "exact", "iexact", "gt", "gte", "lt", "lte", "in", "contains", "icontains", "startswith", "istartswith",
        "endswith", "iendswith", "range", "isnull", "regex", "iregex", "year", "iso_year", "month", "day",
        "week", "week_day", "iso_week_day", "quarter", "hour", "minute", "second", "date", "time",
    }
)  # fmt: skip


@dataclass(frozen=True)
class ModelRef:
    """모델 클래스 값."""

    key: str


@dataclass(frozen=True)
class QuerySetRef:
    """모델의 QuerySet(매니저 포함) 값. `rows`는 `values()`처럼 인스턴스가 아닌 행을 돌려주는지다."""

    key: str
    rows: bool = False


@dataclass(frozen=True)
class InstanceRef:
    """모델 인스턴스 값."""

    key: str


#: 분류 결과 타입이다.
Ref = ModelRef | QuerySetRef | InstanceRef


class DjangoQueries:
    """모듈 하나의 Django ORM 사용을 사실로 바꾼다."""

    def __init__(
        self,
        catalog: DjangoCatalog,
        symbols: SymbolTable,
        scopes: ModuleScopes,
        extraction: PersistenceExtraction,
        sql: SqlText,
    ) -> None:
        """분석기를 만든다.

        Args:
            catalog: Django 모델 목록.
            symbols: 이름 해석기.
            scopes: 모듈 범위 도우미.
            extraction: 결과.
            sql: SQL 텍스트 수집기.
        """
        self.catalog = catalog
        self.symbols = symbols
        self.scopes = scopes
        self.extraction = extraction
        self.sql = sql
        self.imports_django = _imports_django(scopes.index.module.tree)

    # ------------------------------------------------------------------ 순회

    def run(self) -> None:
        """모듈의 모든 속성 접근과 호출을 살핀다."""
        for node in ast.walk(self.scopes.index.module.tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                self._attribute(node)
            elif isinstance(node, ast.Call):
                self._call(node)

    def _attribute(self, node: ast.Attribute) -> None:
        """매니저·관계 매니저·정방향 관계 접근을 사실로 낸다.

        Args:
            node: 속성 접근.
        """
        base = self.classify(node.value)
        if isinstance(base, ModelRef):
            model = self.catalog.models[base.key]
            if node.attr in model.managers:
                self._emit_model(base.key, node.value)
            return
        if isinstance(base, InstanceRef):
            self._emit_accessor(base.key, node)
            return
        if base is None and node.attr == "objects" and self.imports_django:
            self.extraction.add_dynamic(
                expression_text(node), self._location(node), self.scopes.symbol_for(node),
                "{count} Django manager receivers could not be resolved to a model",
            )  # fmt: skip

    def _call(self, node: ast.Call) -> None:
        """QuerySet·인스턴스 메서드 호출, 모델 생성자, 단축 함수를 사실로 낸다.

        Args:
            node: 호출.
        """
        function = node.func
        if isinstance(function, ast.Attribute):
            receiver = self.classify(function.value)
            method = _method_name(function.attr)
            if isinstance(receiver, QuerySetRef):
                self._queryset_method(receiver.key, method, node, function.value)
            elif isinstance(receiver, InstanceRef) and method in _INSTANCE_METHODS:
                self._instance_method(receiver.key, method, node)
        resolved = self._resolve(function)
        model = self.catalog.model_for_symbol(resolved)
        if model is not None:
            self._constructor(model, node)
            return
        dotted = resolved.dotted if isinstance(resolved, ExternalSymbol) else ""
        if dotted in ("django.shortcuts.get_object_or_404", "django.shortcuts.get_list_or_404") and node.args:
            self._shortcut(node)
        elif dotted.rsplit(".", 1)[-1] == "RawSQL" and dotted.startswith("django.db.models") and node.args:
            self.sql.sink(node.args[0], explicit=True)

    # ------------------------------------------------------------------ 분류

    def classify(self, node: ast.expr, depth: int = 0) -> Ref | None:
        """식의 값을 모델·QuerySet·인스턴스로 분류한다.

        Args:
            node: 식.
            depth: 깊이.

        Returns:
            분류 결과 또는 None(증명하지 못함).
        """
        if depth > MAX_CLASSIFY_DEPTH:
            return None
        if isinstance(node, ast.Await):
            return self.classify(node.value, depth + 1)
        if isinstance(node, ast.Name):
            return self._classify_name(node, depth)
        if isinstance(node, ast.Attribute):
            return self._member(self.classify(node.value, depth + 1), node.attr)
        if isinstance(node, ast.Call):
            return self._call_result(node, depth)
        if isinstance(node, ast.Subscript):
            base = self.classify(node.value, depth + 1)
            if isinstance(base, QuerySetRef):
                return base if isinstance(node.slice, ast.Slice) or base.rows else InstanceRef(base.key)
        return None

    def _classify_name(self, node: ast.Name, depth: int) -> Ref | None:
        """이름을 지역 묶음 또는 모듈 수준 모델 클래스로 분류한다.

        Args:
            node: 이름.
            depth: 깊이.

        Returns:
            분류 결과 또는 None.
        """
        function = self.scopes.enclosing_function(node)
        if function is not None and self.scopes.binds_locally(function, node.id):
            bindings = [
                binding
                for binding in self.scopes.bindings(function, node.id)
                if not (binding.kind == "assign" and binding.value is not None and _self_chain(binding.value, node.id))
            ]
            if len(bindings) != 1:
                return None
            binding = bindings[0]
            if binding.kind == "param":
                if binding.is_self:
                    owner = self.scopes.owner_class(function)
                    key = self._class_model(owner) if owner is not None else None
                    return InstanceRef(key) if key is not None else None
                annotated = self._annotation_model(binding.value)
                return InstanceRef(annotated) if annotated is not None else None
            if binding.value is None:
                return None
            value = self.classify(binding.value, depth + 1)
            if binding.kind == "for":
                return InstanceRef(value.key) if isinstance(value, QuerySetRef) and not value.rows else None
            return value if binding.kind == "assign" else None
        key = self.catalog.model_for_symbol(self.symbols.resolve_name(self.scopes.path, node.id))
        return ModelRef(key) if key is not None else None

    def _class_model(self, owner: ast.ClassDef) -> str | None:
        """메서드를 정의한 클래스가 모델이면 키를 돌려준다.

        Args:
            owner: 클래스 정의.

        Returns:
            모델 키 또는 None.
        """
        key = f"{self.scopes.path}#{self.scopes.index.qualname(owner)}"
        return key if key in self.catalog.models else None

    def _annotation_model(self, annotation: ast.expr | None) -> str | None:
        """매개변수 주석이 모델 클래스(문자열 전방 참조 포함)면 키를 돌려준다.

        Args:
            annotation: 주석 식.

        Returns:
            모델 키 또는 None.
        """
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(annotation, (ast.Name, ast.Attribute)):
            return self.catalog.model_for_symbol(self.symbols.resolve_expr(self.scopes.path, annotation))
        return None

    def _member(self, base: Ref | None, attribute: str) -> Ref | None:
        """값의 속성을 분류한다.

        Args:
            base: 값.
            attribute: 속성 이름.

        Returns:
            분류 결과 또는 None.
        """
        if isinstance(base, ModelRef):
            return QuerySetRef(base.key) if attribute in self.catalog.models[base.key].managers else None
        if not isinstance(base, InstanceRef):
            return None
        field = self.catalog.models[base.key].fields.get(attribute)
        if field is not None and field.target is not None:
            if field.kind == "m2m":
                return QuerySetRef(field.target)
            return InstanceRef(field.target) if field.kind in ("fk", "o2o") else None
        reverse = self.catalog.accessors.get(self.catalog.models[base.key].concrete, {}).get(attribute)
        if reverse is None:
            reverse = self.catalog.accessors.get(base.key, {}).get(attribute)
        if reverse is None:
            return None
        return InstanceRef(reverse.source) if reverse.kind == "ro2o" else QuerySetRef(reverse.source)

    def _call_result(self, node: ast.Call, depth: int) -> Ref | None:
        """호출 결과를 분류한다.

        Args:
            node: 호출.
            depth: 깊이.

        Returns:
            분류 결과 또는 None.
        """
        function = node.func
        if isinstance(function, ast.Attribute):
            receiver = self.classify(function.value, depth + 1)
            method = _method_name(function.attr)
            if isinstance(receiver, QuerySetRef):
                if method in QUERYSET_METHODS:
                    return QuerySetRef(receiver.key, receiver.rows or method in _ROW_METHODS)
                if method in INSTANCE_METHODS and not receiver.rows:
                    return InstanceRef(receiver.key)
                return None
            if function.attr == "get_model":
                return self._get_model(node)
        resolved = self._resolve(function)
        model = self.catalog.model_for_symbol(resolved)
        if model is not None:
            return InstanceRef(model)
        dotted = resolved.dotted if isinstance(resolved, ExternalSymbol) else ""
        if dotted in ("django.contrib.auth.get_user_model",):
            user = self.catalog.model_for_label("AUTH_USER_MODEL", None)
            return ModelRef(user) if user is not None else None
        if dotted == "django.shortcuts.get_object_or_404" and node.args:
            first = self.classify(node.args[0], depth + 1)
            return InstanceRef(first.key) if isinstance(first, (ModelRef, QuerySetRef)) else None
        return None

    def _get_model(self, node: ast.Call) -> Ref | None:
        """`django.apps.apps.get_model("app", "Model")`·`get_model("app.Model")`을 리터럴 인자일 때 푼다.

        Args:
            node: 호출.

        Returns:
            모델 클래스 또는 None.
        """
        assert isinstance(node.func, ast.Attribute)
        receiver = self._resolve(node.func.value)
        if not (isinstance(receiver, ExternalSymbol) and receiver.dotted in _APPS_REGISTRY):
            return None
        values = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
        if len(values) != len(node.args) or not 1 <= len(values) <= 2:
            return None
        label = ".".join(values)
        key = self.catalog.model_for_label(label, None) if "." in label else None
        return ModelRef(key) if key is not None else None

    def _resolve(self, node: ast.expr) -> Symbol | None:
        """모듈 수준 이름·속성을 해석한다. 지역 이름이 가리면 None이다.

        Args:
            node: 식.

        Returns:
            해석 결과 또는 None.
        """
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if not isinstance(root, ast.Name):
            return None
        function = self.scopes.enclosing_function(node)
        if function is not None and self.scopes.binds_locally(function, root.id):
            return None
        return self.symbols.resolve_expr(self.scopes.path, node)

    # ------------------------------------------------------------------ 사실

    def _queryset_method(self, key: str, method: str, node: ast.Call, receiver: ast.expr) -> None:
        """QuerySet 메서드 인자에서 컬럼·조인·SQL 사실을 낸다.

        Args:
            key: 모델 키.
            method: 메서드 이름(비동기 이름은 동기 이름으로 바꾼 것).
            node: 호출.
            receiver: 수신 식(앞선 주석 이름을 모으기 위해).
        """
        if method == "raw" and node.args:
            self.sql.sink(node.args[0], explicit=True)
            return
        if method == "extra":
            self._extra(key, node)
            return
        annotations = self._annotations(receiver, 0)
        for keyword in node.keywords:
            if keyword.arg is None:
                self._dynamic_lookup(keyword.value, "{count} Django lookups are passed with ** and could not be read")
            elif keyword.arg in ("defaults", "create_defaults") and method in ("get_or_create", "update_or_create"):
                self._dict_fields(key, keyword.value)
            elif method in _LOOKUP_KWARGS or method in _FIELD_KWARGS:
                self._emit_path(key, keyword.arg, keyword, annotations, allow_join=method in _LOOKUP_KWARGS)
                self._expressions(key, keyword.value, annotations)
            elif method in _EXPRESSION_KWARGS:
                self._expressions(key, keyword.value, annotations)
            elif keyword.arg in ("update_fields", "unique_fields", "fields", "field_name"):
                self._field_list(key, keyword.value)
        self._positional(key, method, node, annotations)

    def _positional(self, key: str, method: str, node: ast.Call, annotations: set[str]) -> None:
        """QuerySet 메서드의 위치 인자를 읽는다.

        Args:
            key: 모델 키.
            method: 메서드 이름.
            node: 호출.
            annotations: 앞선 주석 이름.
        """
        for position, argument in enumerate(node.args):
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                if method in _PATH_ARGS or (method in _FIRST_PATH_ARG and position == 0):
                    self._emit_path(key, argument.value.lstrip("-"), argument, annotations, allow_join=True)
            elif method == "bulk_update" and position == 1:
                self._field_list(key, argument)
            else:
                self._expressions(key, argument, annotations)

    def _annotations(self, receiver: ast.expr, depth: int) -> set[str]:
        """사슬 앞쪽의 `annotate`·`alias`·`values` 키워드 이름을 모은다.

        Args:
            receiver: 수신 식.
            depth: 깊이.

        Returns:
            주석 이름 집합.
        """
        names: set[str] = set()
        current: ast.expr = receiver
        while depth <= MAX_CLASSIFY_DEPTH:
            depth += 1
            if isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
                if _method_name(current.func.attr) in _EXPRESSION_KWARGS:
                    names.update(keyword.arg for keyword in current.keywords if keyword.arg)
                current = current.func.value
            elif isinstance(current, ast.Name):
                function = self.scopes.enclosing_function(current)
                binding = self.scopes.local(function, current.id) if function is not None else None
                if binding is None or binding.kind != "assign" or binding.value is None:
                    break
                current = binding.value
            else:
                break
        return names

    def _instance_method(self, key: str, method: str, node: ast.Call) -> None:
        """인스턴스의 `save`·`delete`·`refresh_from_db`를 관계·컬럼 사실로 낸다.

        Args:
            key: 모델 키.
            method: 메서드 이름.
            node: 호출.
        """
        assert isinstance(node.func, ast.Attribute)
        self._emit_model(key, node.func)
        fields_argument = _INSTANCE_METHODS[method]
        for keyword in node.keywords:
            if keyword.arg == fields_argument:
                self._field_list(key, keyword.value)

    def _constructor(self, key: str, node: ast.Call) -> None:
        """모델 생성자 키워드 인자를 컬럼 사실로 낸다.

        Args:
            key: 모델 키.
            node: 생성자 호출.
        """
        self._emit_model(key, node.func)
        for keyword in node.keywords:
            if keyword.arg is not None:
                self._emit_path(key, keyword.arg, keyword, set(), allow_join=False)

    def _shortcut(self, node: ast.Call) -> None:
        """`get_object_or_404(Model|QuerySet, **lookups)`를 읽는다.

        Args:
            node: 호출.
        """
        first = self.classify(node.args[0])
        if isinstance(first, ModelRef):
            self._emit_model(first.key, node.args[0])
        if isinstance(first, (ModelRef, QuerySetRef)):
            for keyword in node.keywords:
                if keyword.arg is not None:
                    self._emit_path(first.key, keyword.arg, keyword, set(), allow_join=True)

    def _extra(self, key: str, node: ast.Call) -> None:
        """`extra(tables=[...])`의 테이블을 관계 사실로 낸다. SQL 조각(`where`·`select`)은 읽지 않고 센다.

        Args:
            key: 모델 키.
            node: 호출.
        """
        for keyword in node.keywords:
            if keyword.arg == "tables" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                for element in keyword.value.elts:
                    relation = (
                        self.catalog.explicit_relation(element.value)
                        if isinstance(element, ast.Constant) and isinstance(element.value, str)
                        else None
                    )
                    if relation is not None:
                        self.extraction.add_relation(relation, self._location(element), self.scopes.symbol_for(element))
                    else:
                        self._dynamic_lookup(element, "{count} extra(tables=...) entries are not string literals")
            elif keyword.arg == "order_by":
                self._extra_order(key, keyword.value, node)
            elif keyword.arg in ("where", "select", "select_params", "params"):
                self.extraction.add_gap(
                    "skipped-sql-fragments:", "{count} QuerySet.extra() SQL fragments were not read"
                )

    def _extra_order(self, key: str, value: ast.expr, node: ast.Call) -> None:
        """`extra(order_by=[...])`를 읽는다.

        필드 이름은 경로로 읽고, `select` 별칭은 건너뛰며, `table.column`은 SQL 조각으로 센다.

        Args:
            key: 모델 키.
            value: 목록 식.
            node: `extra` 호출.
        """
        aliases: set[str] = set()
        for keyword in node.keywords:
            if keyword.arg == "select" and isinstance(keyword.value, ast.Dict):
                aliases |= {str(item.value) for item in keyword.value.keys if isinstance(item, ast.Constant)}
        elements = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        for element in elements:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                self.extraction.add_gap("skipped-sql-fragments:", _EXTRA_FRAGMENTS)
                continue
            name = element.value.lstrip("-")
            if "." in name:
                self.extraction.add_gap("skipped-sql-fragments:", _EXTRA_FRAGMENTS)
            elif name not in aliases:
                self._emit_path(key, name, element, set(), allow_join=True)

    def _dict_fields(self, key: str, node: ast.expr) -> None:
        """`defaults={...}` 사전의 키를 필드 이름으로 읽는다.

        Args:
            key: 모델 키.
            node: 사전 식.
        """
        if not isinstance(node, ast.Dict):
            return
        for name, value in zip(node.keys, node.values, strict=False):
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                self._emit_path(key, name.value, name, set(), allow_join=False)
            self._expressions(key, value, set())

    def _field_list(self, key: str, node: ast.expr) -> None:
        """필드 이름 목록(`update_fields=[...]`) 또는 이름 하나를 읽는다.

        Args:
            key: 모델 키.
            node: 목록·문자열 식.
        """
        elements = node.elts if isinstance(node, (ast.List, ast.Tuple, ast.Set)) else [node]
        for element in elements:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                self._emit_path(key, element.value, element, set(), allow_join=False)

    def _expressions(self, key: str, node: ast.expr, annotations: set[str], depth: int = 0) -> None:
        """`F`·`Q`·집계·함수 식 안의 필드 경로를 읽는다. 다른 QuerySet(부분 질의)은 따로 읽힌다.

        Args:
            key: 모델 키.
            node: 식.
            annotations: 주석 이름.
            depth: 깊이.
        """
        if depth > MAX_CLASSIFY_DEPTH:
            return
        if isinstance(node, ast.Call):
            self._expression_call(key, node, annotations, depth)
        elif isinstance(node, (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Tuple, ast.List)):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    self._expressions(key, child, annotations, depth + 1)

    def _expression_call(self, key: str, node: ast.Call, annotations: set[str], depth: int) -> None:
        """식 호출 하나를 읽는다.

        Args:
            key: 모델 키.
            node: 호출.
            annotations: 주석 이름.
            depth: 깊이.
        """
        if isinstance(node.func, ast.Attribute) and not isinstance(node.func.value, (ast.Name, ast.Attribute)):
            # `F("x").desc()`처럼 식 결과의 메서드다. 부분 질의(QuerySet)가 아니면 수신 식을 읽는다.
            if self.classify(node.func.value) is None:
                self._expressions(key, node.func.value, annotations, depth + 1)
            return
        resolved = self._resolve(node.func)
        dotted = resolved.dotted if isinstance(resolved, ExternalSymbol) else ""
        if not dotted.startswith(("django.db.models", "django.contrib.postgres")):
            return
        name = dotted.rsplit(".", 1)[-1]
        if name in _OPAQUE_EXPRESSIONS:
            if name == "OuterRef":
                self.extraction.add_gap(
                    "unresolved-django-lookups:", "{count} OuterRef() references were not attributed to a model"
                )
            return
        if name == "Prefetch":
            # `Prefetch("tags", queryset=...)`의 첫 인자는 조회 경로다. queryset은 따로 읽힌다.
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                self._emit_path(key, node.args[0].value, node.args[0], annotations, allow_join=True)
            return
        if name == "Q":
            for keyword in node.keywords:
                if keyword.arg is not None:
                    self._emit_path(key, keyword.arg, keyword, annotations, allow_join=True)
                self._expressions(key, keyword.value, annotations, depth + 1)
        for position, argument in enumerate(node.args):
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                if name != "Q" and (position == 0 or name not in _FIRST_ONLY_EXPRESSIONS):
                    self._emit_path(key, argument.value, argument, annotations, allow_join=True)
            else:
                self._expressions(key, argument, annotations, depth + 1)
        for keyword in node.keywords:
            if name != "Q" and keyword.arg in _EXPRESSION_KEYWORDS:
                if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    self._emit_path(key, keyword.value.value, keyword.value, annotations, allow_join=True)
                else:
                    self._expressions(key, keyword.value, annotations, depth + 1)

    def _emit_path(self, key: str, path: str, node: ast.AST, annotations: set[str], allow_join: bool) -> None:
        """필드 경로 하나(`author__profile__city`)를 컬럼·조인 사실로 낸다.

        Django 조회식 해석(`Query.names_to_path`)을 따른다: 조각마다 필드(이름·`attname`·`pk`)나 역관계를 찾고,
        관계면 대상 모델로 넘어간다. 컬럼 필드에 닿거나 관계 필드 뒤에 조회 이름이 오면 끝난다. 첫 조각이 주석
        이름이면 사실이 없다. 풀지 못하면 dynamic 사실이다.

        Args:
            key: 시작 모델 키.
            path: 필드 경로.
            node: 위치 노드.
            annotations: 주석 이름.
            allow_join: 관계를 따라 조인할 수 있는지(`create`·생성자 키워드는 조인하지 않는다).
        """
        parts = path.split("__")
        if parts[0] in annotations or not path or path == "?":
            return
        if not allow_join and len(parts) > 1:
            # `create`·`update`·생성자는 관계를 건너는 이름을 받지 않는다(FieldDoesNotExist). 추측하지 않는다.
            self._dynamic_lookup(node, "{count} Django lookups or field names could not be resolved", path)
            return
        current = key
        for index, part in enumerate(parts):
            model = self.catalog.models[current]
            field = _find_field(model, part)
            is_last = index == len(parts) - 1
            if field is not None:
                self._emit_field(model, field, node)
                if field.kind == "m2m" and is_last and allow_join:
                    # `Count("tags")`·`prefetch_related("tags")`는 중간 테이블(과 대상)을 조인한다.
                    self._join(field, node)
                if field.kind not in ("fk", "o2o", "m2m") or is_last or not allow_join:
                    return
                if field.target is None:
                    break
                following = parts[index + 1]
                if field.kind != "m2m" and following in LOOKUPS and not self._has_name(field.target, following):
                    return
                self._join(field, node)
                current = field.target
                continue
            reverse = self.catalog.reverse.get(model.concrete, {}).get(part)
            if reverse is not None and allow_join:
                self._emit_reverse(reverse, node)
                current = reverse.source
                if is_last:
                    return
                continue
            if index > 0 and part in LOOKUPS:
                return
            break
        self._dynamic_lookup(node, "{count} Django lookups or field names could not be resolved", path)

    def _has_name(self, key: str, name: str) -> bool:
        """모델에 그 이름의 필드나 역관계가 있는지 본다.

        Args:
            key: 모델 키.
            name: 이름.

        Returns:
            있으면 True.
        """
        model = self.catalog.models[key]
        return _find_field(model, name) is not None or name in self.catalog.reverse.get(model.concrete, {})

    def _emit_field(self, model: DjangoModel, field: ResolvedField, node: ast.AST) -> None:
        """필드의 컬럼 사실을 낸다. 부모 테이블 필드면 그 테이블 관계 사실도 낸다.

        Args:
            model: 조회 중인 모델.
            field: 필드.
            node: 위치 노드.
        """
        if field.owner != model.concrete:
            self._emit_table(field.owner, node)
        if field.kind == "m2m":
            return
        if field.kind == "unknown":
            self.extraction.add_dynamic(
                field.name, self._location(node), self.scopes.symbol_for(node), _UNKNOWN_FIELD_REASON
            )
            return
        self._emit_column(field.owner, field.column, node)

    def _join(self, field: ResolvedField, node: ast.AST) -> None:
        """관계 필드를 따라 조인할 때 대상(과 M2M 중간) 테이블 사실을 낸다.

        Args:
            field: 관계 필드.
            node: 위치 노드.
        """
        if field.kind == "m2m" and field.m2m is not None:
            self._emit_m2m(field, node)
        if field.target is not None:
            self._emit_table(field.target, node)

    def _emit_m2m(self, field: ResolvedField, node: ast.AST) -> None:
        """M2M 중간 테이블과 두 컬럼 사실을 낸다.

        Args:
            field: M2M 필드.
            node: 위치 노드.
        """
        assert field.m2m is not None
        relation = self.catalog.m2m_relation(field.m2m)
        location, symbol = self._location(node), self.scopes.symbol_for(node)
        if relation is None:
            self.extraction.add_dynamic(
                field.name, location, symbol, "{count} Django many-to-many tables could not be named statically"
            )
            return
        self.extraction.add_relation(relation, location, symbol)
        for column in (field.m2m.source_column, field.m2m.target_column):
            name = self.catalog.column(column)
            if name is not None:
                self.extraction.add_column(relation, name, location, symbol)

    def _emit_reverse(self, reverse: ReverseRelation, node: ast.AST) -> None:
        """역관계를 따라 조인할 때 원본 테이블(과 M2M 중간 테이블)·외래 키 컬럼 사실을 낸다.

        Args:
            reverse: 역관계.
            node: 위치 노드.
        """
        if reverse.kind == "rm2m":
            self._emit_m2m(reverse.field, node)
        else:
            self._emit_column(reverse.field.owner, reverse.field.column, node)
        self._emit_table(reverse.source, node)

    def _emit_accessor(self, key: str, node: ast.Attribute) -> None:
        """인스턴스의 관계 접근(`article.tags`, `article.comment_set`, `comment.article`)을 사실로 낸다.

        Args:
            key: 인스턴스 모델 키.
            node: 속성 접근.
        """
        model = self.catalog.models[key]
        field = model.fields.get(node.attr)
        if field is not None and field.kind in ("fk", "o2o", "m2m") and field.target is not None:
            if field.kind == "m2m":
                self._emit_m2m(field, node)
            self._emit_table(field.target, node)
            return
        reverse = self.catalog.accessors.get(model.concrete, {}).get(node.attr)
        if reverse is not None:
            self._emit_reverse(reverse, node)

    def _emit_model(self, key: str, node: ast.AST) -> None:
        """모델 테이블(다중 테이블 상속이면 부모 테이블들도) 관계 사실을 낸다.

        Args:
            key: 모델 키.
            node: 위치 노드.
        """
        model = self.catalog.models[key]
        if model.abstract:
            return
        self._emit_table(model.concrete, node)
        for parent in self.catalog.ancestors(model.concrete):
            self._emit_table(parent, node)

    def _emit_table(self, key: str, node: ast.AST) -> None:
        """테이블 하나의 관계 사실을 낸다. 이름을 확정하지 못하면 dynamic 사실이다.

        Args:
            key: 모델 키.
            node: 위치 노드.
        """
        relation = self.catalog.relation(key)
        location, symbol = self._location(node), self.scopes.symbol_for(node)
        if relation is None:
            model = self.catalog.models.get(key)
            self.extraction.add_dynamic(
                model.name if model else key, location, symbol,
                "{count} Django model tables depend on an unresolved app label or database backend",
            )  # fmt: skip
            return
        self.extraction.add_relation(relation, location, symbol)

    def _emit_column(self, owner: str, column: str | None, node: ast.AST) -> None:
        """컬럼 사실을 낸다. 테이블을 모르면 테이블 쪽에서 이미 dynamic이므로 넘긴다.

        Args:
            owner: 컬럼을 가진 모델 키.
            column: 컬럼 식별자.
            node: 위치 노드.
        """
        relation = self.catalog.relation(owner)
        if relation is None:
            return
        name = self.catalog.column(column)
        location, symbol = self._location(node), self.scopes.symbol_for(node)
        if name is None:
            self.extraction.add_dynamic(
                column or relation.channel, location, symbol,
                "{count} Django column names depend on the database backend or are not literal",
            )  # fmt: skip
            return
        self.extraction.add_column(relation, name, location, symbol)

    def _dynamic_lookup(self, node: ast.AST, reason: str, text: str | None = None) -> None:
        """풀지 못한 조회식을 dynamic 사실로 남긴다.

        Args:
            node: 위치 노드.
            reason: 한계 문장 틀.
            text: 원문(없으면 식을 되돌린 문자열).
        """
        expression = text if text is not None else expression_text(node) if isinstance(node, ast.expr) else ""
        self.extraction.add_dynamic(expression, self._location(node), self.scopes.symbol_for(node), reason)

    def _location(self, node: ast.AST) -> Location:
        """노드 위치를 만든다. 속성 접근은 속성 이름을 가리킨다.

        Args:
            node: 노드.

        Returns:
            위치.
        """
        return fact_location(self.scopes, node)


def _self_chain(value: ast.expr, name: str) -> bool:
    """`qs = qs.filter(...)`처럼 같은 이름에 QuerySet 메서드를 이어 붙인 재대입인지 본다(값의 종류를 바꾸지 않는다).

    Args:
        value: 대입 값.
        name: 대입 대상 이름.

    Returns:
        그러면 True.
    """
    current = value
    while (
        isinstance(current, ast.Call)
        and isinstance(current.func, ast.Attribute)
        and _method_name(current.func.attr) in QUERYSET_METHODS
    ):
        current = current.func.value
    return current is not value and isinstance(current, ast.Name) and current.id == name


def _find_field(model: DjangoModel, part: str) -> ResolvedField | None:
    """조회 조각 하나에 해당하는 필드를 찾는다(`pk`, 이름, 외래 키 `attname`).

    Args:
        model: 모델.
        part: 조각.

    Returns:
        필드 또는 None.
    """
    if part == "pk" and model.pk is not None:
        return model.fields.get(model.pk)
    found = model.fields.get(part)
    if found is not None:
        return found
    if part.endswith("_id"):
        candidate = model.fields.get(part[:-3])
        if candidate is not None and candidate.kind in ("fk", "o2o"):
            # `attname`(`author_id`)은 관계가 아니라 외래 키 컬럼 자체다(`Options._forward_fields_map`).
            return replace(candidate, kind="column", target=None)
    return None


def _method_name(name: str) -> str:
    """비동기 QuerySet 메서드 이름(`aget`, `afirst`)을 동기 이름으로 바꾼다.

    Args:
        name: 메서드 이름.

    Returns:
        동기 이름.
    """
    if name.startswith("a") and name[1:] in _ASYNC_BASES:
        return name[1:]
    return name


#: 비동기 이름(`a` 접두사)을 가질 수 있는 메서드다.
_ASYNC_BASES = QUERYSET_METHODS | INSTANCE_METHODS | _OTHER_METHODS | frozenset(_INSTANCE_METHODS)


def _imports_django(tree: ast.Module) -> bool:
    """모듈이 django를 import하는지 본다.

    Args:
        tree: 모듈 구문 트리.

    Returns:
        import하면 True.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "django":
            return True
        if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "django" for alias in node.names):
            return True
    return False
