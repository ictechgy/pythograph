"""Django 모델 목록: 모델 클래스 → 테이블, 필드 → 컬럼, 관계와 역관계.

규칙은 Django 5.2.17 소스로 확인했다(`docs/PERSISTENCE.md`):

- 기본 테이블 이름은 `<app_label>_<모델 이름 소문자>`를 `truncate_name(…, max_name_length)`로 자른 것이다
  (`Options.contribute_to_class`). `Meta.db_table`은 자르지 않는다.
- 추상 모델(`Meta.abstract`)은 테이블이 없고 필드를 자식에 복사한다. 프록시(`Meta.proxy`)는 구체 부모의 테이블을
  쓴다(`Options.setup_proxy`). 구체 부모를 상속하면(다중 테이블 상속) 자식 테이블에 `<부모 이름>_ptr_id`
  기본 키가 생기고 부모 필드는 부모 테이블에 남는다(`ModelBase.__new__`).
- `Meta`가 없으면 클래스 속성 `Meta`를 상속한다(추상 부모의 `Meta`만 남아 있다). `class Meta(Base.Meta)`는 파이썬
  상속으로 속성을 물려받는다.
- 기본 키가 없으면 `id` 자동 필드가 생긴다.
- M2M 중간 테이블은 `through` 모델의 테이블, `db_table`, 또는 `truncate_name("<모델 테이블>_<필드>", …)`이고
  자동 중간 모델의 컬럼은 `<모델 이름>_id`·`<대상 이름>_id`(자기 참조면 `from_`·`to_` 접두사)다
  (`create_many_to_many_intermediary_model`, `ManyToManyField._get_m2m_db_table`).
- 역관계 질의 이름은 `related_query_name` → `related_name` → 모델 이름 소문자, 인스턴스 접근자는 `related_name` →
  `<모델 이름>_set`(일대일은 모델 이름)이다. `+`로 끝나는 `related_name`은 역관계가 없다.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from pythograph.persistence.django.apps import DjangoApps
from pythograph.persistence.django.fields import (
    EXTERNAL_ABSTRACT_MODELS,
    EXTERNAL_MODEL_CLASSES,
    EXTERNAL_MODELS,
    ExternalFieldSpec,
    FieldSpec,
    read_field,
)
from pythograph.persistence.django.settings import DjangoProjectSettings
from pythograph.persistence.model import RelationName
from pythograph.persistence.names import (
    DjangoBackend,
    django_final_segments,
    django_generated_name,
    django_strip_quotes,
)
from pythograph.source.evaluate import UNKNOWN, Evaluator
from pythograph.source.project import SourceModule
from pythograph.source.symbols import ExternalSymbol, ProjectSymbol, Symbol, SymbolTable

#: 모델 상속 사슬을 따라가는 최대 깊이다.
MAX_MODEL_DEPTH = 32

#: Django 모델 뿌리 클래스의 외부 점 경로다.
_MODEL_ROOTS = frozenset(
    {"django.db.models.Model", "django.db.models.base.Model", "django.contrib.gis.db.models.Model"}
)

#: 읽는 `Meta` 속성이다.
_META_OPTIONS = ("db_table", "app_label", "abstract", "proxy", "default_related_name")

#: 백엔드 → 식별자 함수 타입이다.
_IdentifierFor = Callable[[DjangoBackend], "str | None"]


@dataclass(frozen=True)
class M2MInfo:
    """M2M 필드의 중간 테이블.

    Attributes:
        declaring: 필드를 가진 구체 모델 키(자동 이름의 앞부분).
        field_name: 필드 이름.
        explicit_table: `db_table`(없으면 None, 리터럴이 아니면 `...`).
        through: `through` 모델 키(없으면 None, 모르면 `?`).
        source_column: 중간 테이블의 원본 쪽 컬럼(모르면 None).
        target_column: 중간 테이블의 대상 쪽 컬럼(모르면 None).
    """

    declaring: str
    field_name: str
    explicit_table: object
    through: str | None
    source_column: str | None
    target_column: str | None


@dataclass(frozen=True)
class ResolvedField:
    """모델에서 보이는 필드 하나.

    Attributes:
        name: 필드 이름.
        kind: `column`·`fk`·`o2o`·`m2m`·`unknown`.
        column: 컬럼 식별자(m2m·unknown은 None, 리터럴이 아니면 None).
        owner: 컬럼을 가진 구체 모델 키.
        target: 관계 대상 모델 키(관계가 아니거나 모르면 None).
        m2m: M2M 중간 테이블 정보.
        node: 선언 위치 노드(자동 필드는 None).
        path: 선언한 모듈 경로.
        related_name: 역방향 접근자 이름(없으면 None).
        related_query_name: 역방향 질의 이름(없으면 None).
        primary_key: `primary_key=True`로 선언했는지.
        external: 외부 추상 모델에서 받은 필드 명세(프로젝트 선언이면 None).
    """

    name: str
    kind: str
    column: str | None
    owner: str
    target: str | None = None
    m2m: M2MInfo | None = None
    node: ast.AST | None = None
    path: str | None = None
    related_name: str | None = None
    related_query_name: str | None = None
    primary_key: bool = False
    external: ExternalFieldSpec | None = None


@dataclass(frozen=True)
class ReverseRelation:
    """역관계 하나(대상 모델에서 본 이름).

    Attributes:
        kind: `rfk`·`ro2o`·`rm2m`.
        source: 관계 필드를 가진 모델 키.
        field: 관계 필드.
    """

    kind: str
    source: str
    field: ResolvedField


@dataclass
class DjangoModel:
    """해석한 모델 하나(프로젝트 또는 django.contrib).

    Attributes:
        key: 프로젝트 모델은 심볼 id, 외부 모델은 라벨(`auth.User`).
        name: 클래스 이름.
        label: (앱 라벨, 모델 이름 소문자). 앱 라벨을 모르면 None.
        abstract: 추상 모델인지.
        proxy: 프록시 모델인지.
        concrete: 테이블을 가진 모델 키(프록시면 구체 부모).
        explicit_table: `Meta.db_table`(없으면 None, 리터럴이 아니면 `...`).
        fields: 필드 이름 → 필드(부모에서 받은 것 포함).
        pk: 기본 키 필드 이름.
        managers: 매니저 속성 이름.
        complete: 필드 목록이 완전한지(모르는 외부 기반 클래스가 없는지).
        parents: 다중 테이블 상속 구체 부모 모델 키.
        node: 클래스 정의(외부 모델은 None).
        path: 모듈 경로(외부 모델은 None).
    """

    key: str
    name: str
    label: tuple[str, str] | None
    abstract: bool
    proxy: bool
    concrete: str
    explicit_table: object
    fields: dict[str, ResolvedField] = field(default_factory=dict)
    pk: str | None = None
    managers: set[str] = field(default_factory=set)
    complete: bool = True
    parents: list[str] = field(default_factory=list)
    node: ast.ClassDef | None = None
    path: str | None = None
    default_related_name: object = None

    @property
    def model_name(self) -> str:
        """Django `model_name`(클래스 이름 소문자)을 돌려준다.

        Returns:
            모델 이름.
        """
        return self.name.lower()


@dataclass
class _ModelClass:
    """클래스 본문에서 읽은 모델 원재료."""

    symbol: ProjectSymbol
    module: str
    specs: list[FieldSpec]
    meta: dict[str, object]
    project_bases: list[ProjectSymbol]
    external_fields: list[ExternalFieldSpec]
    unknown_base: bool
    managers: set[str]


class DjangoCatalog:
    """프로젝트의 Django 모델 목록과 이름 계산기."""

    def __init__(
        self,
        symbols: SymbolTable,
        settings: DjangoProjectSettings,
        modules: list[SourceModule],
    ) -> None:
        """목록을 만든다.

        Args:
            symbols: 이름 해석기.
            settings: 읽은 Django 설정.
            modules: 모델을 찾을 모듈(테스트·마이그레이션 제외 정책을 적용한 것).
        """
        self.symbols = symbols
        self.settings = settings
        self.evaluator = Evaluator(symbols)
        self.apps = DjangoApps(symbols, settings)
        self.backends: tuple[DjangoBackend, ...] = settings.backends
        self.models: dict[str, DjangoModel] = {}
        self.by_label: dict[tuple[str, str], str] = {}
        self.reverse: dict[str, dict[str, ReverseRelation]] = {}
        self.accessors: dict[str, dict[str, ReverseRelation]] = {}
        self.unresolved_labels = 0
        self._raw: dict[str, _ModelClass] = {}
        self._model_roots: dict[str, bool] = {}
        self._add_external_models()
        for module in modules:
            self._collect_module(module)
        self._register_labels()
        for key in list(self._raw):
            self._resolve(key, 0)
        self._link_reverse()

    # ------------------------------------------------------------------ 이름 계산

    def relation(self, key: str) -> RelationName | None:
        """모델의 테이블 이름을 후보 백엔드 모두에서 같은 경우에만 돌려준다.

        Args:
            key: 모델 키.

        Returns:
            관계 이름 또는 None(추상·미해석·백엔드마다 다름).
        """
        return self._agreed(lambda backend: self._table_identifier(key, backend), generated=False)

    def explicit_relation(self, identifier: str) -> RelationName | None:
        """코드에 쓴 테이블 식별자(`extra(tables=[...])`)의 실제 이름을 후보 백엔드 모두에서 같을 때만 돌려준다.

        Args:
            identifier: 식별자.

        Returns:
            관계 이름 또는 None.
        """
        return self._agreed(lambda backend: identifier, generated=False)

    def m2m_relation(self, info: M2MInfo) -> RelationName | None:
        """M2M 중간 테이블 이름을 돌려준다.

        Args:
            info: 중간 테이블 정보.

        Returns:
            관계 이름 또는 None.
        """
        if info.through == "?":
            return None
        if info.through is not None:
            return self.relation(info.through)
        return self._agreed(lambda backend: self._m2m_identifier(info, backend), generated=False)

    def column(self, identifier: str | None) -> str | None:
        """컬럼 식별자의 실제 이름을 후보 백엔드 모두에서 같은 경우에만 돌려준다.

        Args:
            identifier: 컬럼 식별자.

        Returns:
            컬럼 이름 또는 None.
        """
        if identifier is None:
            return None
        found = self._agreed(lambda backend: identifier, generated=False)
        return found.segments[-1] if found is not None and len(found.segments) == 1 else None

    def _agreed(self, identifier_for: _IdentifierFor, generated: bool) -> RelationName | None:
        """후보 백엔드마다 식별자를 구해 실제 이름이 모두 같으면 돌려준다.

        Args:
            identifier_for: 백엔드 → 식별자(모르면 None).
            generated: 식별자가 아직 자르지 않은 기본 이름인지.

        Returns:
            관계 이름 또는 None.
        """
        results: set[tuple[str, ...] | None] = set()
        for backend in self.backends:
            identifier = identifier_for(backend)
            if identifier is None:
                return None
            if generated:
                identifier = django_generated_name(identifier, backend)
            results.add(django_final_segments(identifier, backend))
        if len(results) != 1:
            return None
        segments = next(iter(results))
        return RelationName(segments) if segments else None

    def _table_identifier(self, key: str, backend: DjangoBackend) -> str | None:
        """백엔드에서 Django가 SQL에 넣는 테이블 식별자를 구한다.

        Args:
            key: 모델 키.
            backend: 백엔드.

        Returns:
            식별자 또는 None.
        """
        model = self.models.get(key)
        if model is None or model.abstract:
            return None
        concrete = self.models.get(model.concrete)
        if concrete is None:
            return None
        if concrete.explicit_table is not None:
            return concrete.explicit_table if isinstance(concrete.explicit_table, str) else None
        if concrete.label is None:
            return None
        return django_generated_name(f"{concrete.label[0]}_{concrete.label[1]}", backend)

    def _m2m_identifier(self, info: M2MInfo, backend: DjangoBackend) -> str | None:
        """자동 중간 테이블의 식별자를 구한다.

        Args:
            info: 중간 테이블 정보.
            backend: 백엔드.

        Returns:
            식별자 또는 None.
        """
        if info.explicit_table is not None:
            return info.explicit_table if isinstance(info.explicit_table, str) else None
        owner = self._table_identifier(info.declaring, backend)
        if owner is None:
            return None
        return django_generated_name(f"{django_strip_quotes(owner)}_{info.field_name}", backend)

    # ------------------------------------------------------------------ 모델 찾기

    def ancestors(self, key: str) -> list[str]:
        """다중 테이블 상속 조상 모델 키를 가까운 것부터 돌려준다.

        Args:
            key: 구체 모델 키.

        Returns:
            조상 키 목록.
        """
        found: list[str] = []
        pending = list(self.models[key].parents) if key in self.models else []
        while pending and len(found) <= MAX_MODEL_DEPTH:
            current = pending.pop(0)
            if current not in found:
                found.append(current)
                pending.extend(self.models[current].parents if current in self.models else [])
        return found

    def model_for_symbol(self, symbol: Symbol | None) -> str | None:
        """해석한 이름이 모델 클래스면 모델 키를 돌려준다.

        Args:
            symbol: 해석 결과.

        Returns:
            모델 키 또는 None.
        """
        if isinstance(symbol, ProjectSymbol) and symbol.id in self.models:
            return symbol.id
        if isinstance(symbol, ExternalSymbol):
            label = EXTERNAL_MODEL_CLASSES.get(symbol.dotted)
            return label if label in self.models else None
        return None

    def model_for_label(self, label: str, context: DjangoModel | None) -> str | None:
        """`앱.모델`·`모델`·`self` 문자열 참조를 모델 키로 푼다.

        Args:
            label: 참조 문자열.
            context: 참조를 담은 모델(상대 참조·`self`용).

        Returns:
            모델 키 또는 None.
        """
        if label == "self":
            return context.key if context is not None else None
        if label == "AUTH_USER_MODEL":
            user = self.settings.auth_user_model
            return self.model_for_label(user, None) if user else None
        app, _, name = label.rpartition(".")
        if not app:
            if context is None or context.label is None:
                return None
            app = context.label[0]
        return self.by_label.get((app, name.lower()))

    def _add_external_models(self) -> None:
        """django.contrib 구체 모델을 목록에 넣는다."""
        for label, external in EXTERNAL_MODELS.items():
            app, _, name = label.partition(".")
            model = DjangoModel(label, name, (app, name.lower()), False, False, label, external.table)
            for spec in external.fields:
                model.fields[spec.name] = self._external_field(model, spec, external.through.get(spec.name))
            model.pk = "session_key" if label == "sessions.Session" else "id"
            model.managers = {"objects", "_default_manager", "_base_manager"}
            self.models[label] = model
            self.by_label[(app, name.lower())] = label

    def _external_field(
        self, model: DjangoModel, spec: ExternalFieldSpec, through: tuple[str, str, str] | None
    ) -> ResolvedField:
        """외부 필드 명세를 필드로 바꾼다. 관계 대상은 나중에(`_link_reverse` 전) 라벨로 푼다.

        Args:
            model: 필드를 가진 모델.
            spec: 명세.
            through: 구체 외부 모델 M2M의 (중간 테이블, 원본 컬럼, 대상 컬럼).

        Returns:
            필드.
        """
        m2m = None
        if spec.kind == "m2m" and through is not None:
            m2m = M2MInfo(model.key, spec.name, through[0], None, through[1], through[2])
        elif spec.kind == "m2m":
            target_name = (spec.target or "").rpartition(".")[2].lower()
            m2m = M2MInfo(model.key, spec.name, None, None, f"{model.model_name}_id", f"{target_name}_id")
        return ResolvedField(
            spec.name,
            spec.kind,
            spec.column,
            model.key,
            spec.target,
            m2m,
            related_name=spec.related_name,
            related_query_name=spec.related_query_name,
            external=spec,
        )

    def _collect_module(self, module: SourceModule) -> None:
        """모듈 수준 클래스 중 Django 모델(추상 포함)을 모은다.

        Args:
            module: 모듈.
        """
        for statement in module.tree.body:
            if not isinstance(statement, ast.ClassDef):
                continue
            symbol = ProjectSymbol(module.path, statement.name, statement)
            raw = self._read_class(symbol, module.name)
            if raw is not None:
                self._raw[symbol.id] = raw

    def _read_class(self, symbol: ProjectSymbol, module: str) -> _ModelClass | None:
        """클래스가 모델이면 원재료를 읽는다.

        Args:
            symbol: 클래스.
            module: 모듈 점 경로.

        Returns:
            원재료 또는 None(모델이 아님).
        """
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        project_bases: list[ProjectSymbol] = []
        external_fields: list[ExternalFieldSpec] = []
        unknown_base = False
        proven = False
        for base in node.bases:
            resolved = self.symbols.resolve_expr(symbol.path, base)
            if isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef):
                if self._is_model_class(resolved, 0):
                    project_bases.append(resolved)
                    proven = True
            elif isinstance(resolved, ExternalSymbol) and resolved.dotted in _MODEL_ROOTS:
                proven = True
            elif isinstance(resolved, ExternalSymbol) and resolved.dotted in EXTERNAL_ABSTRACT_MODELS:
                external_fields.extend(EXTERNAL_ABSTRACT_MODELS[resolved.dotted])
                proven = True
            elif not (isinstance(resolved, ExternalSymbol) and resolved.dotted in ("builtins.object", "object")):
                unknown_base = True
        specs = [spec for statement in node.body if (spec := read_field(self.symbols, symbol.path, statement))]
        # 모르는 외부 기반이면 Django 필드(`django.` 모듈)를 선언했을 때만 모델로 본다. DRF 직렬화기처럼 `…Field`를
        # 쓰는 다른 클래스를 모델로 오인하지 않기 위해서다.
        django_fields = any(spec.kind != "unknown" for spec in specs)
        if not proven and not (unknown_base and django_fields):
            return None
        return _ModelClass(
            symbol, module, specs, self._meta(symbol), project_bases, external_fields, unknown_base,
            self._managers(symbol),
        )  # fmt: skip

    def _register_labels(self) -> None:
        """모든 구체 모델의 라벨을 먼저 등록한다. 뒤에 정의한 모델을 가리키는 문자열 참조(`through="M"`)를 푼다."""
        for key, raw in self._raw.items():
            if raw.meta.get("abstract") is True:
                continue
            app_label = raw.meta.get("app_label")
            if app_label is None:
                app_label = self.apps.label_for_module(raw.module)
            if isinstance(app_label, str):
                self.by_label.setdefault((app_label, raw.symbol.qualname.lower()), key)

    def _is_model_class(self, symbol: ProjectSymbol, depth: int) -> bool:
        """프로젝트 클래스가 Django 모델 뿌리를 상속하는지 본다(캐시).

        Args:
            symbol: 클래스.
            depth: 상속 깊이.

        Returns:
            모델이면 True.
        """
        if symbol.id in self._model_roots:
            return self._model_roots[symbol.id]
        self._model_roots[symbol.id] = False
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        result = False
        if depth <= MAX_MODEL_DEPTH:
            for base in node.bases:
                resolved = self.symbols.resolve_expr(symbol.path, base)
                if isinstance(resolved, ExternalSymbol) and (
                    resolved.dotted in _MODEL_ROOTS or resolved.dotted in EXTERNAL_ABSTRACT_MODELS
                ):
                    result = True
                elif isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef):
                    result = result or self._is_model_class(resolved, depth + 1)
        self._model_roots[symbol.id] = result
        return result

    def _meta(self, symbol: ProjectSymbol) -> dict[str, object]:
        """모델의 `Meta` 속성을 읽는다. 자기 `Meta`가 없으면 부모 클래스의 `Meta`(추상 부모만 남긴다)를 쓴다.

        Django는 추상 모델의 `Meta`를 설치하기 전에 `abstract = False`로 바꾸므로 물려받은 `Meta`의 abstract는
        버린다(`ModelBase.__new__`).

        Args:
            symbol: 모델 클래스.

        Returns:
            속성 이름 → 리터럴 값(리터럴이 아니면 `...`).
        """
        found = self._meta_class(symbol, 0)
        if found is None:
            return {}
        meta_path, meta_node = found
        values = self._meta_values(meta_path, meta_node, 0)
        own = isinstance(symbol.node, ast.ClassDef) and meta_node in symbol.node.body
        if not own:
            values.pop("abstract", None)
        return values

    def _meta_class(self, symbol: ProjectSymbol, depth: int) -> tuple[str, ast.ClassDef] | None:
        """클래스 속성 `Meta`를 찾는다. 구체 부모의 `Meta`는 Django가 지우므로 추상 부모에서만 찾는다.

        Args:
            symbol: 클래스.
            depth: 상속 깊이.

        Returns:
            (모듈 경로, Meta 클래스) 또는 None.
        """
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        for statement in node.body:
            if isinstance(statement, ast.ClassDef) and statement.name == "Meta":
                return symbol.path, statement
        if depth > MAX_MODEL_DEPTH:
            return None
        for base in node.bases:
            resolved = self.symbols.resolve_expr(symbol.path, base)
            if not (isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef)):
                continue
            meta = self._meta_class(resolved, depth + 1)
            if meta is not None and self._meta_values(meta[0], meta[1], 0).get("abstract") is True:
                return meta
            if meta is not None:
                return None
        return None

    def _meta_values(self, path: str, meta: ast.ClassDef, depth: int) -> dict[str, object]:
        """Meta 클래스 본문과 그 부모 Meta(`class Meta(Base.Meta)`)의 속성을 읽는다.

        Args:
            path: 모듈 경로.
            meta: Meta 클래스.
            depth: 상속 깊이.

        Returns:
            속성 이름 → 값.
        """
        values: dict[str, object] = {}
        if depth <= MAX_MODEL_DEPTH:
            for base in reversed(meta.bases):
                parent = self._meta_base(path, base)
                if parent is not None:
                    inherited = self._meta_values(parent[0], parent[1], depth + 1)
                    # Django는 추상 모델의 Meta를 설치하기 전에 abstract를 False로 바꾼다.
                    inherited.pop("abstract", None)
                    values.update(inherited)
        for statement in meta.body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name) and target.id in _META_OPTIONS:
                        values[target.id] = self._meta_value(path, statement.value)
        return values

    def _meta_value(self, path: str, node: ast.expr) -> object:
        """Meta 속성 값을 리터럴 또는 모듈 상수(`TABLE_PREFIX + "users"`)로 읽는다.

        Args:
            path: 모듈 경로.
            node: 값 식.

        Returns:
            값, 읽지 못하면 `...`.
        """
        literal = _literal(node)
        if literal is not ...:
            return literal
        value = self.evaluator.value(path, node)
        return ... if value is UNKNOWN else value

    def _meta_base(self, path: str, base: ast.expr) -> tuple[str, ast.ClassDef] | None:
        """`Base.Meta` 식을 Meta 클래스로 푼다.

        Args:
            path: 모듈 경로.
            base: 기반 식.

        Returns:
            (모듈 경로, Meta 클래스) 또는 None.
        """
        if not (isinstance(base, ast.Attribute) and base.attr == "Meta"):
            return None
        owner = self.symbols.resolve_expr(path, base.value)
        if not (isinstance(owner, ProjectSymbol) and isinstance(owner.node, ast.ClassDef)):
            return None
        for statement in owner.node.body:
            if isinstance(statement, ast.ClassDef) and statement.name == "Meta":
                return owner.path, statement
        return None

    def _managers(self, symbol: ProjectSymbol) -> set[str]:
        """클래스 본문에서 매니저 속성을 모은다.

        매니저 클래스(이름이 `Manager`로 끝나는 외부 클래스나 그런 클래스를 상속한 프로젝트 클래스)의 호출,
        `QuerySet.as_manager()`, `Manager.from_queryset(Q)()`를 매니저로 본다.

        Args:
            symbol: 모델 클래스.

        Returns:
            속성 이름 집합.
        """
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        names: set[str] = set()
        for statement in node.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Call)
                and self._is_manager_call(symbol.path, statement.value)
            ):
                names.add(statement.targets[0].id)
        return names

    def _is_manager_call(self, path: str, call: ast.Call) -> bool:
        """호출이 매니저 인스턴스를 만드는지 본다.

        Args:
            path: 모듈 경로.
            call: 호출 식.

        Returns:
            매니저면 True.
        """
        function = call.func
        if isinstance(function, ast.Attribute) and function.attr == "as_manager":
            return True
        if isinstance(function, ast.Call):
            return isinstance(function.func, ast.Attribute) and function.func.attr == "from_queryset"
        return self._is_manager_class(self.symbols.resolve_expr(path, function), 0)

    def _is_manager_class(self, symbol: Symbol | None, depth: int) -> bool:
        """클래스가 매니저인지 본다.

        Args:
            symbol: 해석 결과.
            depth: 상속 깊이.

        Returns:
            매니저면 True.
        """
        if isinstance(symbol, ExternalSymbol):
            return symbol.dotted.endswith("Manager")
        if not (isinstance(symbol, ProjectSymbol) and isinstance(symbol.node, ast.ClassDef)):
            return False
        if depth > MAX_MODEL_DEPTH:
            return False
        return any(
            self._is_manager_class(self.symbols.resolve_expr(symbol.path, base), depth + 1)
            for base in symbol.node.bases
        )

    # ------------------------------------------------------------------ 해석

    def _resolve(self, key: str, depth: int) -> DjangoModel | None:
        """원재료를 해석한 모델로 바꾼다(부모 먼저, 캐시).

        Args:
            key: 모델 키.
            depth: 상속 깊이.

        Returns:
            모델 또는 None.
        """
        if key in self.models:
            return self.models[key]
        raw = self._raw.get(key)
        if raw is None or depth > MAX_MODEL_DEPTH:
            return None
        parents = [self._resolve(base.id, depth + 1) for base in raw.project_bases]
        model = self._new_model(raw)
        self.models[key] = model
        if model.label is not None and not model.abstract:
            self.by_label.setdefault(model.label, key)
        self._inherit(model, raw, [parent for parent in parents if parent is not None])
        return model

    def _new_model(self, raw: _ModelClass) -> DjangoModel:
        """원재료에서 필드 없는 모델을 만든다.

        Args:
            raw: 원재료.

        Returns:
            모델.
        """
        meta = raw.meta
        abstract = meta.get("abstract") is True
        app_label = meta.get("app_label")
        if app_label is None:
            app_label = self.apps.label_for_module(raw.module)
            if app_label is None and not abstract:
                self.unresolved_labels += 1
        label = (app_label, raw.symbol.qualname.lower()) if isinstance(app_label, str) else None
        return DjangoModel(
            key=raw.symbol.id,
            name=raw.symbol.qualname,
            label=label,
            abstract=abstract,
            proxy=meta.get("proxy") is True,
            concrete=raw.symbol.id,
            explicit_table=meta.get("db_table"),
            complete=not raw.unknown_base,
            node=raw.symbol.node if isinstance(raw.symbol.node, ast.ClassDef) else None,
            path=raw.symbol.path,
            default_related_name=meta.get("default_related_name"),
        )

    def _inherit(self, model: DjangoModel, raw: _ModelClass, parents: list[DjangoModel]) -> None:
        """부모 필드·매니저를 받고 자기 필드를 더한 뒤 기본 키를 정한다.

        Args:
            model: 채울 모델.
            raw: 원재료.
            parents: 해석한 프로젝트 부모 모델.
        """
        local_names = {spec.name for spec in raw.specs}
        concrete_parents = [parent for parent in parents if not parent.abstract]
        if model.proxy and concrete_parents:
            model.concrete = concrete_parents[0].concrete
            model.fields.update(self.models[model.concrete].fields)
            model.pk = self.models[model.concrete].pk
        links: list[str] = []
        for parent in parents:
            model.complete = model.complete and parent.complete
            model.managers |= parent.managers
            if parent.abstract:
                self._copy_abstract(model, parent, local_names)
            elif not model.proxy:
                links.append(self._add_parent(model, parent, raw))
                model.parents.append(parent.concrete)
        for external in raw.external_fields:
            if external.name not in local_names:
                model.fields.setdefault(external.name, self._external_field(model, external, None))
        for spec in raw.specs:
            model.fields[spec.name] = self._field(model, spec)
        model.managers |= raw.managers
        if not model.managers:
            model.managers.add("objects")
        model.managers |= {"_default_manager", "_base_manager"}
        if model.pk is None:
            self._choose_pk(model, links)

    def _copy_abstract(self, model: DjangoModel, parent: DjangoModel, local_names: set[str]) -> None:
        """추상 부모의 필드를 자식 소유로 복사한다. 관계 대상·자동 이름은 자식 기준으로 다시 푼다.

        Args:
            model: 자식.
            parent: 추상 부모.
            local_names: 자식이 직접 선언한 필드 이름.
        """
        for name, inherited in parent.fields.items():
            if name in local_names or name in model.fields or (inherited.node is None and inherited.external is None):
                continue
            raw_spec = self._spec_of(parent.key, name)
            if raw_spec is not None:
                model.fields[name] = self._field(model, raw_spec)
            elif inherited.external is not None:
                model.fields[name] = self._external_field(model, inherited.external, None)

    def _spec_of(self, key: str, name: str) -> FieldSpec | None:
        """모델 사슬에서 필드 선언 원재료를 찾는다(추상 부모 복사용).

        Args:
            key: 모델 키.
            name: 필드 이름.

        Returns:
            선언 또는 None.
        """
        raw = self._raw.get(key)
        if raw is None:
            return None
        for spec in raw.specs:
            if spec.name == name:
                return spec
        for base in raw.project_bases:
            found = self._spec_of(base.id, name)
            if found is not None:
                return found
        return None

    def _add_parent(self, model: DjangoModel, parent: DjangoModel, raw: _ModelClass) -> str:
        """다중 테이블 상속 부모의 필드(부모 소유)와 `<부모>_ptr` 연결 필드를 더한다.

        Args:
            model: 자식.
            parent: 구체 부모.
            raw: 자식 원재료.

        Returns:
            부모 연결 필드 이름.
        """
        for name, inherited in parent.fields.items():
            model.fields.setdefault(name, inherited)
        explicit = next(
            (
                spec
                for spec in raw.specs
                if spec.kind == "o2o" and spec.options.get("parent_link") is True
                and self._target_key(spec, model) == parent.key
            ),
            None,
        )  # fmt: skip
        if explicit is not None:
            return explicit.name
        ptr = f"{parent.model_name}_ptr"
        model.fields[ptr] = ResolvedField(ptr, "o2o", f"{ptr}_id", model.key, parent.key)
        return ptr

    @staticmethod
    def _choose_pk(model: DjangoModel, links: list[str]) -> None:
        """기본 키를 정한다(`Options._prepare`): 선언한 `primary_key=True`(추상 부모에서 받은 것 포함), 없으면 첫
        부모 연결 필드, 그것도 없으면 `id` 자동 필드를 더한다.

        Args:
            model: 모델.
            links: 다중 테이블 상속 부모 연결 필드 이름.
        """
        for item in model.fields.values():
            if item.owner == model.key and item.primary_key:
                model.pk = item.name
                return
        if links:
            model.pk = links[0]
            return
        if "id" not in model.fields:
            model.fields["id"] = ResolvedField("id", "column", "id", model.key)
        model.pk = "id"

    def _field(self, model: DjangoModel, spec: FieldSpec) -> ResolvedField:
        """필드 선언을 모델 소유 필드로 바꾼다.

        Args:
            model: 필드를 가진(복사받은) 모델.
            spec: 선언.

        Returns:
            필드.
        """
        db_column = spec.options.get("db_column")
        # `related_name`이 없으면 `Meta.default_related_name`을 쓴다(`RelatedField.contribute_to_class`).
        related_name = _placeholder(spec.options.get("related_name", model.default_related_name), model)
        related_query_name = _placeholder(spec.options.get("related_query_name"), model)
        target = self._target_key(spec, model) if spec.kind in ("fk", "o2o", "m2m") else None
        if spec.kind == "m2m" and target == model.key and spec.options.get("symmetrical") is not False:
            # 대칭 자기 참조 M2M은 역관계가 숨겨진다(`related_name = "<이름>_rel_+"`).
            related_name = f"{spec.name}_rel_+"
        if spec.kind == "m2m":
            return ResolvedField(
                spec.name, "m2m", None, model.key, target, self._m2m_info(model, spec, target),
                spec.node, spec.path, related_name, related_query_name, False,
            )  # fmt: skip
        if spec.kind == "unknown":
            column = None
        elif db_column is not None:
            column = db_column if isinstance(db_column, str) else None
        else:
            column = f"{spec.name}_id" if spec.kind in ("fk", "o2o") else spec.name
        return ResolvedField(
            spec.name, spec.kind, column, model.key, target, None, spec.node, spec.path, related_name,
            related_query_name, spec.options.get("primary_key") is True,
        )  # fmt: skip

    def _m2m_info(self, model: DjangoModel, spec: FieldSpec, target: str | None) -> M2MInfo:
        """M2M 중간 테이블 정보를 만든다.

        Args:
            model: 필드를 가진 모델.
            spec: 선언.
            target: 대상 모델 키.

        Returns:
            중간 테이블 정보.
        """
        if spec.through is not None:
            through = self._reference(spec.through, spec.path, model)
            if through is None:
                return M2MInfo(model.key, spec.name, None, "?", None, None)
            source, destination = self._through_columns(through, model.key, target, spec.options.get("through_fields"))
            return M2MInfo(model.key, spec.name, None, through, source, destination)
        source_name = model.model_name
        target_name = self._model_name(target)
        if target_name is None:
            return M2MInfo(model.key, spec.name, spec.options.get("db_table"), None, f"{source_name}_id", None)
        if target_name == source_name:
            source_name, target_name = f"from_{source_name}", f"to_{target_name}"
        return M2MInfo(
            model.key, spec.name, spec.options.get("db_table"), None, f"{source_name}_id", f"{target_name}_id"
        )

    def _through_columns(
        self, through: str, source: str, target: str | None, through_fields: object
    ) -> tuple[str | None, str | None]:
        """명시 중간 모델에서 원본·대상 쪽 외래 키 컬럼을 찾는다.

        Args:
            through: 중간 모델 키.
            source: 원본 모델 키.
            target: 대상 모델 키.
            through_fields: `through_fields` 리터럴.

        Returns:
            (원본 컬럼, 대상 컬럼). 확정하지 못한 쪽은 None.
        """
        through_model = self._resolve(through, 0) if through in self._raw else self.models.get(through)
        if through_model is None:
            return None, None
        if isinstance(through_fields, (tuple, list)) and len(through_fields) == 2:
            first, second = (through_model.fields.get(str(name)) for name in through_fields)
            return (first.column if first else None), (second.column if second else None)
        return self._unique_fk(through_model, source), self._unique_fk(through_model, target)

    def _model_name(self, key: str | None) -> str | None:
        """모델 키의 `model_name`을 돌려준다(아직 해석하지 않은 모델 포함).

        Args:
            key: 모델 키.

        Returns:
            모델 이름 또는 None.
        """
        if key is None:
            return None
        if key in self.models:
            return self.models[key].model_name
        raw = self._raw.get(key)
        return raw.symbol.qualname.lower() if raw is not None else None

    @staticmethod
    def _unique_fk(model: DjangoModel, target: str | None) -> str | None:
        """대상을 가리키는 외래 키가 하나뿐이면 그 컬럼을 돌려준다.

        Args:
            model: 중간 모델.
            target: 대상 모델 키.

        Returns:
            컬럼 또는 None.
        """
        found = [field for field in model.fields.values() if field.kind == "fk" and field.target == target]
        return found[0].column if len(found) == 1 and target is not None else None

    def _target_key(self, spec: FieldSpec, model: DjangoModel) -> str | None:
        """관계 필드의 대상 모델 키를 푼다.

        Args:
            spec: 선언.
            model: 참조를 담은(복사받은) 모델.

        Returns:
            모델 키 또는 None.
        """
        return self._reference(spec.target, spec.path, model) if spec.target is not None else None

    def _reference(self, node: ast.expr, path: str, context: DjangoModel) -> str | None:
        """모델 참조 식(클래스·문자열·`settings.AUTH_USER_MODEL`)을 모델 키로 푼다.

        Args:
            node: 참조 식.
            path: 식이 있는 모듈 경로.
            context: 참조를 담은 모델.

        Returns:
            모델 키 또는 None.
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self.model_for_label(node.value, context)
        if isinstance(node, ast.Attribute) and node.attr == "AUTH_USER_MODEL":
            base = self.symbols.resolve_expr(path, node.value)
            if isinstance(base, ExternalSymbol) and base.dotted == "django.conf.settings":
                return self.model_for_label("AUTH_USER_MODEL", None)
        resolved = self.symbols.resolve_expr(path, node) if isinstance(node, (ast.Name, ast.Attribute)) else None
        if isinstance(resolved, ProjectSymbol) and resolved.id in self._raw:
            return resolved.id
        return self.model_for_symbol(resolved)

    def _link_reverse(self) -> None:
        """외부 모델의 라벨 대상을 풀고 모든 관계 필드의 역관계를 대상 모델에 단다."""
        for model in list(self.models.values()):
            for name, item in list(model.fields.items()):
                if item.path is None and item.target is not None and item.target not in self.models:
                    model.fields[name] = self._relabel(model, item)
        for model in self.models.values():
            if model.abstract:
                continue
            for item in model.fields.values():
                if item.owner == model.key and item.target is not None and item.kind in ("fk", "o2o", "m2m"):
                    self._add_reverse(model, item)

    def _relabel(self, model: DjangoModel, item: ResolvedField) -> ResolvedField:
        """외부 명세의 라벨 대상을 모델 키로 바꾼다.

        Args:
            model: 필드를 가진 모델.
            item: 필드.

        Returns:
            대상을 푼 필드.
        """
        target = self.model_for_label(item.target or "", model)
        return replace(item, target=target)

    def _add_reverse(self, model: DjangoModel, item: ResolvedField) -> None:
        """관계 필드 하나의 역관계를 대상 모델에 단다.

        Args:
            model: 필드를 가진 모델.
            item: 관계 필드.
        """
        if item.target is None or (item.related_name or "").endswith("+"):
            return
        kind = {"fk": "rfk", "o2o": "ro2o", "m2m": "rm2m"}[item.kind]
        relation = ReverseRelation(kind, model.key, item)
        model_name = model.model_name
        query_name = item.related_query_name or item.related_name or model_name
        accessor = item.related_name or (model_name if kind == "ro2o" else f"{model_name}_set")
        self.reverse.setdefault(item.target, {}).setdefault(query_name, relation)
        self.accessors.setdefault(item.target, {}).setdefault(accessor, relation)


def _placeholder(value: object, model: DjangoModel) -> str | None:
    """`related_name`의 `%(class)s`·`%(app_label)s`를 자식 모델 기준으로 채운다.

    Args:
        value: 리터럴 값.
        model: 필드를 가진(복사받은) 모델.

    Returns:
        채운 문자열, 문자열이 아니면 None.
    """
    if not isinstance(value, str):
        return None
    if model.label is None and "%(" in value:
        return None
    app = model.label[0] if model.label else ""
    name = model.model_name
    return value.replace("%(class)s", name).replace("%(model_name)s", name).replace("%(app_label)s", app.lower())


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
