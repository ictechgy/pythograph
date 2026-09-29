"""SQLAlchemy 2.x·Flask-SQLAlchemy 3 매핑 목록: Declarative 클래스·Core `Table` → 테이블·컬럼.

규칙은 SQLAlchemy 2.0.54·Flask-SQLAlchemy 3.1.1 소스로 확인했다(`docs/PERSISTENCE.md`):

- Declarative 기반: `DeclarativeBase`·`DeclarativeBaseNoMeta` 하위 클래스, `declarative_base()`,
  `registry().generate_base()`, Flask-SQLAlchemy `db.Model`.
- 테이블 이름: 자기 `__table__`(Core `Table`), 자기 `__tablename__`, 매핑되지 않은 믹스인·추상 클래스의
  `__tablename__` 순서다. 매핑된 부모의 이름은 물려받지 않는다(`_ClassScanMapperConfig._scan_attributes`).
  이름이 없고 매핑된 부모가 있으면 단일 테이블 상속이라 부모 테이블을 쓴다.
- Flask-SQLAlchemy는 이름이 없으면 `camel_to_snake_case(클래스 이름)`을 붙인다(`should_set_tablename`). 매핑된
  부모가 있을 때 자기 기본 키 컬럼이 없으면 단일 테이블 상속으로 부모 테이블을 쓴다(`__table_cls__`).
  `disable_autonaming=True`거나 `model_class`가 이미 만든 declarative 클래스면 자동 이름이 없다.
- 컬럼 이름: `Column`·`mapped_column`의 첫 위치 문자열 또는 `name=`, 없으면 속성 이름. `x: Mapped[T]`만 있는
  주석도 컬럼이다. 믹스인 컬럼은 각 매핑 클래스에 복사된다.
- 스키마: `__table_args__`의 `schema`, 없으면 기반 `MetaData(schema=...)`.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field

from pythograph.persistence.model import RelationName
from pythograph.persistence.names import FSA_NAMING
from pythograph.source.evaluate import Evaluator
from pythograph.source.project import SourceModule
from pythograph.source.symbols import ExternalSymbol, ProjectSymbol, Symbol, SymbolTable, ValueSymbol

#: 상속 사슬을 따라가는 최대 깊이다.
MAX_CLASS_DEPTH = 32

#: Declarative 기반 클래스의 외부 점 경로다.
_DECLARATIVE_BASES = frozenset(
    {
        "sqlalchemy.orm.DeclarativeBase",
        "sqlalchemy.orm.DeclarativeBaseNoMeta",
        "sqlalchemy.orm.decl_api.DeclarativeBase",
        "sqlalchemy.orm.decl_api.DeclarativeBaseNoMeta",
    }
)

#: `declarative_base()` 함수의 외부 점 경로다.
_DECLARATIVE_FACTORIES = frozenset(
    {
        "sqlalchemy.orm.declarative_base",
        "sqlalchemy.orm.decl_api.declarative_base",
        "sqlalchemy.ext.declarative.declarative_base",
    }
)

#: Flask-SQLAlchemy 확장 클래스의 외부 점 경로다.
_FSA_CLASSES = frozenset({"flask_sqlalchemy.SQLAlchemy", "flask_sqlalchemy.extension.SQLAlchemy"})

#: SQLAlchemy가 공개하는 이름을 찾을 모듈이다. Flask-SQLAlchemy `db.<이름>`도 같은 이름으로 본다.
_SQLALCHEMY_MODULES = ("sqlalchemy.", "sqlalchemy.orm.", "sqlalchemy.schema.", "sqlalchemy.sql.")


@dataclass(frozen=True)
class BaseInfo:
    """Declarative 기반의 이름 규칙.

    Attributes:
        autonaming: Flask-SQLAlchemy 자동 이름을 쓰는지(모르면 None).
        schema: 기반 `MetaData`의 스키마(없으면 None).
        fsa: Flask-SQLAlchemy `db.Model`인지.
    """

    autonaming: bool | None
    schema: str | None
    fsa: bool


@dataclass(frozen=True)
class SaColumn:
    """매핑 클래스의 컬럼 속성.

    Attributes:
        key: 속성 이름.
        column: 컬럼 이름(모르면 None).
        owner: 컬럼을 가진 테이블의 클래스 키.
        primary_key: 기본 키인지.
        node: 선언 위치.
        path: 선언 모듈.
    """

    key: str
    column: str | None
    owner: str
    primary_key: bool
    node: ast.AST | None
    path: str


@dataclass(frozen=True)
class SaRelationship:
    """매핑 클래스의 관계 속성.

    Attributes:
        key: 속성 이름.
        target: 대상 클래스 키(모르면 None).
        secondary: 연결 테이블 이름(없으면 None, 모르면 `...`).
    """

    key: str
    target: str | None
    secondary: object


@dataclass
class SaModel:
    """매핑 클래스 하나.

    Attributes:
        key: 심볼 id.
        name: 클래스 이름.
        path: 모듈 경로.
        node: 클래스 정의.
        base: 기반 이름 규칙.
        table_owner: 테이블을 가진 클래스 키(단일 테이블 상속이면 조상, 모르면 None).
        table: 자기 테이블 이름(테이블 소유자일 때, 모르면 None).
        columns: 컬럼 속성.
        relationships: 관계 속성.
        parents: 매핑된 부모 클래스 키.
        abstract: `__abstract__`인지.
    """

    key: str
    name: str
    path: str
    node: ast.ClassDef
    base: BaseInfo
    table_owner: str | None = None
    table: RelationName | None = None
    columns: dict[str, SaColumn] = field(default_factory=dict)
    relationships: dict[str, SaRelationship] = field(default_factory=dict)
    parents: list[str] = field(default_factory=list)
    abstract: bool = False


@dataclass
class SaTable:
    """Core `Table`·`table()` 하나.

    Attributes:
        name: 테이블 이름(모르면 None).
        columns: 컬럼 이름 집합(`c.<이름>` 접근용).
        node: `Table(...)` 호출.
        path: 모듈 경로.
    """

    name: RelationName | None
    columns: set[str]
    node: ast.Call
    path: str


@dataclass
class _ClassBody:
    """클래스 본문에서 읽은 매핑 재료."""

    tablename: object = None
    table: ast.expr | None = None
    table_args: ast.expr | None = None
    abstract: bool = False
    columns: list[tuple[str, ast.Call | None, ast.AST]] = field(default_factory=list)
    relationships: list[tuple[str, ast.Call]] = field(default_factory=list)


class SqlAlchemyCatalog:
    """프로젝트의 SQLAlchemy 매핑 목록."""

    def __init__(self, symbols: SymbolTable, modules: list[SourceModule], fsa_majors: tuple[int, ...] = (3,)) -> None:
        """목록을 만든다.

        Args:
            symbols: 이름 해석기.
            modules: 매핑을 찾을 모듈.
            fsa_majors: Flask-SQLAlchemy 자동 이름 후보 메이저(버전을 모르면 2와 3).
        """
        self.symbols = symbols
        self.fsa_majors = fsa_majors
        self.naming_disagreements = 0
        self.evaluator = Evaluator(symbols)
        self.models: dict[str, SaModel] = {}
        self.tables: dict[int, SaTable] = {}
        self.unknown_tablenames = 0
        self.unsupported: set[str] = set()
        self._bodies: dict[str, _ClassBody] = {}
        self._bases: dict[str, BaseInfo | None] = {}
        self._classes: dict[str, ProjectSymbol] = {}
        for module in modules:
            self._collect_module(module)
        for key in list(self._classes):
            self._resolve(key, 0)
        for model in list(self.models.values()):
            self._link_relationships(model)

    # ------------------------------------------------------------------ 조회

    def relation(self, key: str) -> RelationName | None:
        """매핑 클래스의 테이블 이름을 돌려준다.

        Args:
            key: 클래스 키.

        Returns:
            관계 이름 또는 None.
        """
        model = self.models.get(key)
        if model is None or model.table_owner is None:
            return None
        owner = self.models.get(model.table_owner)
        return owner.table if owner is not None else None

    def ancestors(self, key: str) -> list[str]:
        """조인 상속으로 함께 읽는 조상 테이블 소유 클래스를 돌려준다.

        Args:
            key: 클래스 키.

        Returns:
            조상 키 목록(자기 테이블 소유자 제외).
        """
        found: list[str] = []
        model = self.models.get(key)
        own = model.table_owner if model else None
        pending = list(model.parents) if model else []
        while pending and len(found) <= MAX_CLASS_DEPTH:
            current = self.models.get(pending.pop(0))
            if current is None:
                continue
            owner = current.table_owner
            if owner is not None and owner != own and owner not in found:
                found.append(owner)
            pending.extend(current.parents)
        return found

    def model_for_symbol(self, symbol: Symbol | None) -> str | None:
        """해석 결과가 매핑 클래스면 키를 돌려준다.

        Args:
            symbol: 해석 결과.

        Returns:
            클래스 키 또는 None.
        """
        if isinstance(symbol, ProjectSymbol) and symbol.id in self.models and not self.models[symbol.id].abstract:
            return symbol.id
        return None

    def table_for_symbol(self, symbol: Symbol | None) -> SaTable | None:
        """해석 결과가 모듈 수준 Core 테이블이면 돌려준다.

        Args:
            symbol: 해석 결과.

        Returns:
            테이블 또는 None.
        """
        if isinstance(symbol, ValueSymbol) and isinstance(symbol.node, ast.Call):
            return self.tables.get(id(symbol.node))
        return None

    # ------------------------------------------------------------------ 모으기

    def _collect_module(self, module: SourceModule) -> None:
        """모듈 수준 클래스와 Core 테이블 대입을 모은다.

        Args:
            module: 모듈.
        """
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call) and self.is_table_call(module.path, node):
                self.tables[id(node)] = self._core_table(module.path, node)
        for statement in module.tree.body:
            if isinstance(statement, ast.ClassDef):
                symbol = ProjectSymbol(module.path, statement.name, statement)
                self._classes[symbol.id] = symbol

    def sqlalchemy_name(self, path: str, node: ast.expr) -> str | None:
        """식이 SQLAlchemy 공개 이름(또는 Flask-SQLAlchemy `db.<이름>`)이면 마지막 이름을 돌려준다.

        Args:
            path: 모듈 경로.
            node: 식.

        Returns:
            이름(`Column`, `select` 등) 또는 None.
        """
        if isinstance(node, ast.Attribute) and self.fsa_instance(path, node.value) is not None:
            return node.attr
        resolved = self.symbols.resolve_expr(path, node) if isinstance(node, (ast.Name, ast.Attribute)) else None
        if isinstance(resolved, ExternalSymbol) and resolved.dotted.startswith(_SQLALCHEMY_MODULES):
            return resolved.dotted.rsplit(".", 1)[-1]
        return None

    def fsa_instance(self, path: str, node: ast.expr) -> ast.Call | None:
        """식이 Flask-SQLAlchemy 확장 객체(`db = SQLAlchemy(...)`)면 생성 호출을 돌려준다.

        Args:
            path: 모듈 경로.
            node: 식.

        Returns:
            생성 호출 또는 None.
        """
        resolved = self.symbols.resolve_expr(path, node) if isinstance(node, (ast.Name, ast.Attribute)) else None
        if not (isinstance(resolved, ValueSymbol) and isinstance(resolved.node, ast.Call)):
            return None
        factory = self.symbols.resolve_expr(resolved.path, resolved.node.func)
        if isinstance(factory, ExternalSymbol) and factory.dotted in _FSA_CLASSES:
            return resolved.node
        return None

    def is_table_call(self, path: str, node: ast.Call) -> bool:
        """호출이 Core `Table(...)`·`table(...)`인지 본다.

        Args:
            path: 모듈 경로.
            node: 호출.

        Returns:
            그러면 True.
        """
        return self.sqlalchemy_name(path, node.func) in ("Table", "table") and bool(node.args)

    def _core_table(self, path: str, node: ast.Call) -> SaTable:
        """Core 테이블 호출을 읽는다.

        Args:
            path: 모듈 경로.
            node: `Table("name", metadata, Column(...), schema=...)` 호출.

        Returns:
            테이블.
        """
        name = self.evaluator.string(path, node.args[0])
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        schema = self.evaluator.string(path, keywords["schema"]) if "schema" in keywords else None
        if "schema" in keywords and schema is None:
            name = None
        elif "schema" not in keywords:
            schema = self._table_metadata_schema(path, node)
        columns: set[str] = set()
        for argument in node.args[1:]:
            if isinstance(argument, ast.Call) and self.sqlalchemy_name(path, argument.func) in ("Column", "column"):
                column = self.column_name(path, argument, None)
                if column is not None:
                    columns.add(column)
        relation = None if name is None else RelationName(((schema,) if schema else ()) + (name,))
        return SaTable(relation, columns, node, path)

    def _table_metadata_schema(self, path: str, node: ast.Call) -> str | None:
        """스키마 인자가 없는 Core 테이블의 `MetaData(schema=...)` 기본 스키마를 구한다.

        `Table(name, Base.metadata, ...)`는 기반의 MetaData, `Table(name, metadata, ...)`는 그 MetaData 대입,
        Flask-SQLAlchemy `db.Table(name, ...)`은 `SQLAlchemy(metadata=...)`의 스키마를 쓴다.

        Args:
            path: 모듈 경로.
            node: 테이블 호출.

        Returns:
            스키마 또는 None.
        """
        if isinstance(node.func, ast.Attribute):
            fsa = self.fsa_instance(path, node.func.value)
            if fsa is not None:
                return self._metadata_schema(path, fsa)
        if len(node.args) < 2:
            return None
        metadata = node.args[1]
        if isinstance(metadata, ast.Attribute) and metadata.attr == "metadata":
            info = self.base_info(path, metadata.value)
            return info.schema if info is not None else None
        if isinstance(metadata, ast.Call):
            return self._schema_keyword(path, metadata)
        resolved = self.symbols.resolve_expr(path, metadata) if isinstance(metadata, ast.Name) else None
        if isinstance(resolved, ValueSymbol) and isinstance(resolved.node, ast.Call):
            return self._schema_keyword(resolved.path, resolved.node)
        return None

    def column_name(self, path: str, call: ast.Call, key: str | None) -> str | None:
        """`Column`·`mapped_column` 호출의 컬럼 이름을 구한다.

        Args:
            path: 모듈 경로.
            call: 호출.
            key: 속성 이름(이름 인자가 없을 때 쓴다).

        Returns:
            컬럼 이름 또는 None.
        """
        for keyword in call.keywords:
            if keyword.arg == "name":
                return self.evaluator.string(path, keyword.value)
        if call.args and isinstance(call.args[0], (ast.Constant, ast.JoinedStr)):
            first = self.evaluator.value(path, call.args[0])
            if isinstance(first, str):
                return first
        return key

    # ------------------------------------------------------------------ 기반

    def base_info(self, path: str, node: ast.expr) -> BaseInfo | None:
        """기반 클래스 식이 Declarative 기반이면 이름 규칙을 돌려준다.

        Args:
            path: 모듈 경로.
            node: 기반 식.

        Returns:
            이름 규칙 또는 None.
        """
        if isinstance(node, ast.Attribute) and node.attr == "Model":
            call = self.fsa_instance(path, node.value)
            if call is not None:
                return self._fsa_base(path, call)
        resolved = self.symbols.resolve_expr(path, node) if isinstance(node, (ast.Name, ast.Attribute)) else None
        if isinstance(resolved, ValueSymbol) and isinstance(resolved.node, ast.Call):
            return self._factory_base(resolved.path, resolved.node)
        if isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef):
            return self._class_base(resolved)
        return None

    def _factory_base(self, path: str, call: ast.Call) -> BaseInfo | None:
        """`declarative_base(...)`·`registry().generate_base()` 호출이면 기반 규칙을 돌려준다.

        Args:
            path: 대입이 있는 모듈 경로.
            call: 호출.

        Returns:
            이름 규칙 또는 None.
        """
        factory = (
            self.symbols.resolve_expr(path, call.func) if isinstance(call.func, (ast.Name, ast.Attribute)) else None
        )
        generated = isinstance(call.func, ast.Attribute) and call.func.attr == "generate_base"
        if not generated and not (isinstance(factory, ExternalSymbol) and factory.dotted in _DECLARATIVE_FACTORIES):
            return None
        return BaseInfo(False, self._metadata_schema(path, call), False)

    def _class_base(self, symbol: ProjectSymbol) -> BaseInfo | None:
        """`class Base(DeclarativeBase)`면 기반 규칙을 돌려준다.

        Args:
            symbol: 클래스.

        Returns:
            이름 규칙 또는 None.
        """
        if symbol.id in self._bases:
            return self._bases[symbol.id]
        self._bases[symbol.id] = None
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        found = None
        for base in node.bases:
            resolved = self.symbols.resolve_expr(symbol.path, base)
            if isinstance(resolved, ExternalSymbol) and resolved.dotted in _DECLARATIVE_BASES:
                found = BaseInfo(False, self._class_metadata_schema(symbol), False)
        self._bases[symbol.id] = found
        return found

    def _fsa_base(self, path: str, call: ast.Call) -> BaseInfo:
        """Flask-SQLAlchemy `db.Model`의 이름 규칙을 구한다.

        Args:
            path: 모듈 경로(해석 기준).
            call: `SQLAlchemy(...)` 생성 호출.

        Returns:
            이름 규칙.
        """
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        disabled = keywords.get("disable_autonaming")
        schema = self._metadata_schema(path, call)
        if disabled is not None and not (isinstance(disabled, ast.Constant) and disabled.value is False):
            autonaming = None if not isinstance(disabled, ast.Constant) else not bool(disabled.value)
            return BaseInfo(autonaming, schema, True)
        model_class = keywords.get("model_class")
        if model_class is None:
            return BaseInfo(True, schema, True)
        resolved = (
            self.symbols.resolve_expr(path, model_class) if isinstance(model_class, (ast.Name, ast.Attribute)) else None
        )
        if isinstance(resolved, ValueSymbol) and isinstance(resolved.node, ast.Call):
            made = self._factory_base(resolved.path, resolved.node)
            # 이미 만든 declarative 클래스는 그대로 쓴다(자동 이름 없음).
            return BaseInfo(False if made else None, schema, True)
        if isinstance(resolved, ProjectSymbol):
            return BaseInfo(True, schema, True)
        return BaseInfo(None, schema, True)

    def _metadata_schema(self, path: str, call: ast.Call) -> str | None:
        """`metadata=MetaData(schema=...)` 키워드의 스키마를 읽는다.

        Args:
            path: 모듈 경로.
            call: 기반을 만드는 호출.

        Returns:
            스키마 또는 None.
        """
        for keyword in call.keywords:
            if keyword.arg == "metadata" and isinstance(keyword.value, ast.Call):
                return self._schema_keyword(path, keyword.value)
        return None

    def _class_metadata_schema(self, symbol: ProjectSymbol) -> str | None:
        """`class Base(DeclarativeBase): metadata = MetaData(schema=...)`의 스키마를 읽는다.

        Args:
            symbol: 기반 클래스.

        Returns:
            스키마 또는 None.
        """
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        for statement in node.body:
            target, value = _assignment(statement)
            if target == "metadata" and isinstance(value, ast.Call):
                return self._schema_keyword(symbol.path, value)
        return None

    def _schema_keyword(self, path: str, call: ast.Call) -> str | None:
        """호출의 `schema=` 문자열을 읽는다.

        Args:
            path: 모듈 경로.
            call: 호출.

        Returns:
            스키마 또는 None.
        """
        for keyword in call.keywords:
            if keyword.arg == "schema":
                return self.evaluator.string(path, keyword.value)
        return None

    # ------------------------------------------------------------------ 해석

    def _resolve(self, key: str, depth: int) -> SaModel | None:
        """클래스가 매핑 클래스(또는 추상 매핑 클래스)면 해석한다(부모 먼저, 캐시).

        Args:
            key: 클래스 키.
            depth: 깊이.

        Returns:
            매핑 클래스 또는 None.
        """
        if key in self.models:
            return self.models[key]
        symbol = self._classes.get(key)
        if symbol is None or depth > MAX_CLASS_DEPTH or self._class_base(symbol) is not None:
            return None
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        base: BaseInfo | None = None
        parents: list[SaModel] = []
        mixins: list[ProjectSymbol] = []
        for expression in node.bases:
            info = self.base_info(symbol.path, expression)
            resolved = self.symbols.resolve_expr(symbol.path, expression)
            parent = self._resolve(resolved.id, depth + 1) if isinstance(resolved, ProjectSymbol) else None
            if info is not None:
                base = base or info
            elif parent is not None:
                parents.append(parent)
                base = base or parent.base
            elif isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef):
                mixins.append(resolved)
            elif isinstance(resolved, ExternalSymbol) and resolved.dotted.startswith("sqlmodel"):
                self.unsupported.add("sqlmodel")
        if base is None:
            return None
        body = self._body(symbol)
        model = SaModel(symbol.id, symbol.qualname, symbol.path, node, base, abstract=body.abstract)
        model.parents = [parent.key for parent in parents if not parent.abstract]
        self.models[key] = model
        self._place_table(model, body, parents, mixins)
        self._collect_columns(model, body, parents, mixins)
        return model

    def _body(self, symbol: ProjectSymbol) -> _ClassBody:
        """클래스 본문의 매핑 재료를 읽는다(캐시).

        Args:
            symbol: 클래스.

        Returns:
            본문 재료.
        """
        if symbol.id in self._bodies:
            return self._bodies[symbol.id]
        node = symbol.node
        assert isinstance(node, ast.ClassDef)
        body = _ClassBody()
        for statement in node.body:
            self._read_statement(symbol, statement, body)
        self._bodies[symbol.id] = body
        return body

    def _read_statement(self, symbol: ProjectSymbol, statement: ast.stmt, body: _ClassBody) -> None:
        """클래스 본문 문장 하나를 읽는다.

        Args:
            symbol: 클래스.
            statement: 문장.
            body: 채울 재료.
        """
        target, value = _assignment(statement)
        if target is None:
            if isinstance(statement, ast.FunctionDef) and statement.name == "__tablename__":
                body.tablename = ...  # @declared_attr로 계산하는 이름이다.
            return
        if target == "__tablename__":
            body.tablename = self.evaluator.string(symbol.path, value) if value is not None else None
            if body.tablename is None:
                body.tablename = ...
        elif target == "__table__":
            body.table = value
        elif target == "__table_args__":
            body.table_args = value
        elif target == "__abstract__":
            body.abstract = isinstance(value, ast.Constant) and value.value is True
        elif isinstance(value, ast.Call):
            self._read_attribute_call(symbol, target, value, statement, body)
        elif (
            value is None and isinstance(statement, ast.AnnAssign) and self._mapped_column_annotation(symbol, statement)
        ):
            body.columns.append((target, None, statement.target))

    def _read_attribute_call(
        self, symbol: ProjectSymbol, target: str, value: ast.Call, statement: ast.stmt, body: _ClassBody
    ) -> None:
        """속성 대입 호출이 컬럼·관계면 기록한다.

        Args:
            symbol: 클래스.
            target: 속성 이름.
            value: 호출.
            statement: 문장.
            body: 채울 재료.
        """
        name = self.sqlalchemy_name(symbol.path, value.func)
        if name in ("column_property", "deferred") and value.args and isinstance(value.args[0], ast.Call):
            inner = value.args[0]
            if self.sqlalchemy_name(symbol.path, inner.func) in ("Column", "mapped_column"):
                body.columns.append((target, inner, _target_node(statement)))
        elif name in ("Column", "mapped_column"):
            body.columns.append((target, value, _target_node(statement)))
        elif name == "relationship":
            body.relationships.append((target, value))

    def _mapped_column_annotation(self, symbol: ProjectSymbol, statement: ast.AnnAssign) -> bool:
        """값 없는 `x: Mapped[T]` 주석이 컬럼인지 본다. T가 매핑 클래스나 컬렉션이면 관계라 컬럼이 아니다.

        Args:
            symbol: 클래스.
            statement: 주석 대입.

        Returns:
            컬럼이면 True.
        """
        annotation = statement.annotation
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return False
        if not isinstance(annotation, ast.Subscript):
            return False
        head = (
            annotation.value.attr
            if isinstance(annotation.value, ast.Attribute)
            else getattr(annotation.value, "id", "")
        )
        if head != "Mapped":
            return False
        inner = annotation.slice
        text = ast.unparse(inner)
        if any(
            word in text
            for word in ("List[", "list[", "Set[", "set[", "Dict[", "dict[", "WriteOnlyMapped", "DynamicMapped")
        ):
            return False
        names = {child.id for child in ast.walk(inner) if isinstance(child, ast.Name)}
        names |= {
            child.value for child in ast.walk(inner) if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
        return not any(self._is_class_name(symbol.path, name) for name in names)

    def _is_class_name(self, path: str, name: str) -> bool:
        """이름이 프로젝트 클래스를 가리키는지 본다(관계 주석 판정용).

        Args:
            path: 모듈 경로.
            name: 이름.

        Returns:
            프로젝트 클래스면 True.
        """
        if any(symbol.qualname == name for symbol in self._classes.values()):
            return True
        resolved = self.symbols.resolve_name(path, name)
        return isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef)

    def _place_table(
        self, model: SaModel, body: _ClassBody, parents: list[SaModel], mixins: list[ProjectSymbol]
    ) -> None:
        """테이블 소유자와 이름을 정한다.

        Args:
            model: 매핑 클래스.
            body: 자기 본문 재료.
            parents: 매핑된(추상 포함) 부모.
            mixins: 매핑되지 않은 프로젝트 믹스인.
        """
        if model.abstract:
            return
        schema = self._schema(model, body, parents, mixins)
        if body.table is not None:
            model.table_owner, model.table = model.key, self._table_expression(model.path, body.table)
            return
        name = body.tablename if body.tablename is not None else self._inherited_tablename(parents, mixins)
        mapped = [parent for parent in parents if not parent.abstract]
        if name is None and mapped:
            if model.base.autonaming and self._own_primary_key(model, body):
                name = self._auto_name(model.name)
            elif model.base.autonaming is None:
                name = ...
            else:
                model.table_owner = mapped[0].table_owner
                return
        if name is None and model.base.autonaming:
            name = self._auto_name(model.name)
        model.table_owner = model.key
        if not isinstance(name, str) or schema is ...:
            self.unknown_tablenames += 1
            return
        model.table = RelationName(((schema,) if isinstance(schema, str) else ()) + (name,))

    def _auto_name(self, class_name: str) -> object:
        """Flask-SQLAlchemy 자동 이름을 후보 메이저 모두에서 같을 때만 돌려준다.

        Args:
            class_name: 클래스 이름.

        Returns:
            이름, 후보마다 다르면 `...`.
        """
        names = {FSA_NAMING[major](class_name) for major in self.fsa_majors}
        if len(names) == 1:
            return next(iter(names))
        self.naming_disagreements += 1
        return ...

    def _inherited_tablename(self, parents: list[SaModel], mixins: list[ProjectSymbol]) -> object:
        """매핑되지 않은 믹스인·추상 부모의 `__tablename__`을 찾는다(매핑된 부모 이름은 물려받지 않는다).

        Args:
            parents: 매핑된(추상 포함) 부모.
            mixins: 믹스인.

        Returns:
            이름, 없으면 None, 계산 이름이면 `...`.
        """
        for parent in parents:
            if parent.abstract:
                body = self._bodies.get(parent.key)
                if body is not None and body.tablename is not None:
                    return body.tablename
        for mixin in mixins:
            found = self._mixin_value(mixin, lambda body: body.tablename, 0)
            if found is not None:
                return found
        return None

    def _schema(self, model: SaModel, body: _ClassBody, parents: list[SaModel], mixins: list[ProjectSymbol]) -> object:
        """`__table_args__`(자기·믹스인·추상 부모) 또는 기반 `MetaData`의 스키마를 구한다.

        Args:
            model: 매핑 클래스.
            body: 자기 본문 재료.
            parents: 부모.
            mixins: 믹스인.

        Returns:
            스키마, 없으면 None, 읽지 못하면 `...`.
        """
        args = body.table_args
        if args is None:
            for parent in parents:
                parent_body = self._bodies.get(parent.key)
                if parent.abstract and parent_body is not None and parent_body.table_args is not None:
                    args = parent_body.table_args
        if args is None:
            for mixin in mixins:
                found = self._mixin_value(mixin, lambda item: item.table_args, 0)
                args = args or (found if isinstance(found, ast.expr) else None)
        if args is None:
            return model.base.schema
        options = args.elts[-1] if isinstance(args, ast.Tuple) and args.elts else args
        if isinstance(args, ast.Tuple) and not isinstance(options, ast.Dict):
            return model.base.schema
        if not isinstance(options, ast.Dict):
            return ...
        for key, value in zip(options.keys, options.values, strict=False):
            if isinstance(key, ast.Constant) and key.value == "schema":
                found = self.evaluator.string(model.path, value)
                return found if found is not None else ...
        return model.base.schema

    def _mixin_value(self, mixin: ProjectSymbol, pick: Callable[[_ClassBody], object], depth: int) -> object:
        """믹스인 사슬에서 본문 재료 값을 찾는다.

        Args:
            mixin: 믹스인 클래스.
            pick: 본문 재료 → 값 함수.
            depth: 깊이.

        Returns:
            값 또는 None.
        """
        found = pick(self._body(mixin))
        if found is not None or depth > MAX_CLASS_DEPTH:
            return found
        node = mixin.node
        assert isinstance(node, ast.ClassDef)
        for base in node.bases:
            resolved = self.symbols.resolve_expr(mixin.path, base)
            if isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef):
                found = self._mixin_value(resolved, pick, depth + 1)
                if found is not None:
                    return found
        return None

    def _own_primary_key(self, model: SaModel, body: _ClassBody) -> bool:
        """자기 본문에 기본 키 컬럼이 있는지 본다(Flask-SQLAlchemy 단일 테이블 상속 판정).

        Args:
            model: 매핑 클래스.
            body: 본문 재료.

        Returns:
            있으면 True.
        """
        return any(call is not None and _primary_key(call) for _, call, _ in body.columns)

    def _table_object(self, path: str, node: ast.expr | None) -> SaTable | None:
        """`__table__` 값(Core `Table(...)` 호출 또는 그 이름)을 테이블로 푼다.

        Args:
            path: 모듈 경로.
            node: 값 식.

        Returns:
            테이블 또는 None.
        """
        if isinstance(node, ast.Call):
            return self.tables.get(id(node))
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self.table_for_symbol(self.symbols.resolve_expr(path, node))
        return None

    def _table_expression(self, path: str, node: ast.expr) -> RelationName | None:
        """`__table__` 값의 테이블 이름을 구한다.

        Args:
            path: 모듈 경로.
            node: 값 식.

        Returns:
            관계 이름 또는 None.
        """
        table = self._table_object(path, node)
        return table.name if table is not None else None

    def _collect_columns(
        self, model: SaModel, body: _ClassBody, parents: list[SaModel], mixins: list[ProjectSymbol]
    ) -> None:
        """부모(소유자 유지)·믹스인(복사)·자기 컬럼과 관계를 모은다.

        Args:
            model: 매핑 클래스.
            body: 자기 본문 재료.
            parents: 부모.
            mixins: 믹스인.
        """
        owner = model.table_owner or model.key
        for parent in parents:
            for key, column in parent.columns.items():
                model.columns.setdefault(key, column if not parent.abstract else _owned(column, owner))
            for key, relationship in parent.relationships.items():
                model.relationships.setdefault(key, relationship)
        for mixin in mixins:
            for key, call, node in self._mixin_columns(mixin, 0):
                model.columns.setdefault(key, self._column(mixin.path, key, call, node, owner))
        for key, call, node in body.columns:
            model.columns[key] = self._column(model.path, key, call, node, owner)
        table = self._table_object(model.path, body.table)
        for name in sorted(table.columns) if table is not None else ():
            model.columns.setdefault(name, SaColumn(name, name, owner, False, None, model.path))
        for key, call in body.relationships:
            model.relationships[key] = SaRelationship(key, None, self._secondary(model.path, call))

    def _mixin_columns(self, mixin: ProjectSymbol, depth: int) -> list[tuple[str, ast.Call | None, ast.AST]]:
        """믹스인 사슬의 컬럼 선언을 모은다(가까운 선언이 이긴다).

        Args:
            mixin: 믹스인.
            depth: 깊이.

        Returns:
            (속성, 호출, 위치) 목록.
        """
        found = [
            (key, call if isinstance(call, ast.Call) else None, node) for key, call, node in self._body(mixin).columns
        ]
        if depth > MAX_CLASS_DEPTH:
            return found
        node = mixin.node
        assert isinstance(node, ast.ClassDef)
        for base in node.bases:
            resolved = self.symbols.resolve_expr(mixin.path, base)
            if isinstance(resolved, ProjectSymbol) and isinstance(resolved.node, ast.ClassDef):
                known = {key for key, _, _ in found}
                found.extend(item for item in self._mixin_columns(resolved, depth + 1) if item[0] not in known)
        return found

    def _column(self, path: str, key: str, call: ast.expr | None, node: ast.AST, owner: str) -> SaColumn:
        """컬럼 선언 하나를 속성으로 바꾼다.

        Args:
            path: 선언 모듈.
            key: 속성 이름.
            call: `Column`·`mapped_column` 호출(주석만 있으면 None).
            node: 위치.
            owner: 테이블 소유 클래스 키.

        Returns:
            컬럼 속성.
        """
        if not isinstance(call, ast.Call):
            return SaColumn(key, key, owner, False, node, path)
        return SaColumn(key, self.column_name(path, call, key), owner, _primary_key(call), node, path)

    def _secondary(self, path: str, call: ast.Call) -> object:
        """`relationship(secondary=...)`의 연결 테이블 이름을 구한다.

        Args:
            path: 모듈 경로.
            call: 관계 호출.

        Returns:
            관계 이름, 없으면 None, 모르면 `...`.
        """
        for keyword in call.keywords:
            if keyword.arg != "secondary":
                continue
            text = self.evaluator.string(path, keyword.value) if isinstance(keyword.value, ast.Constant) else None
            if text is not None:
                return RelationName(tuple(text.split(".")))
            resolved = (
                self.symbols.resolve_expr(path, keyword.value)
                if isinstance(keyword.value, (ast.Name, ast.Attribute))
                else None
            )
            table = self.table_for_symbol(resolved)
            return table.name if table is not None and table.name is not None else ...
        return None

    def _link_relationships(self, model: SaModel) -> None:
        """관계의 대상 클래스를 푼다(클래스 이름 문자열·클래스·람다).

        Args:
            model: 매핑 클래스.
        """
        body = self._bodies.get(model.key)
        for key, call in body.relationships if body else []:
            target = self._relationship_target(model.path, call)
            model.relationships[key] = SaRelationship(key, target, model.relationships[key].secondary)

    def _relationship_target(self, path: str, call: ast.Call) -> str | None:
        """관계 호출의 대상 클래스 키를 구한다.

        Args:
            path: 모듈 경로.
            call: 관계 호출.

        Returns:
            클래스 키 또는 None.
        """
        argument = (
            call.args[0]
            if call.args
            else next((keyword.value for keyword in call.keywords if keyword.arg == "argument"), None)
        )
        if isinstance(argument, ast.Lambda):
            argument = argument.body
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            name = argument.value.rpartition(".")[2]
            found = [key for key, item in self.models.items() if item.name == name and not item.abstract]
            return found[0] if len(found) == 1 else None
        if isinstance(argument, (ast.Name, ast.Attribute)):
            return self.model_for_symbol(self.symbols.resolve_expr(path, argument))
        return None


def _owned(column: SaColumn, owner: str) -> SaColumn:
    """추상 부모 컬럼을 자식 테이블 소유로 바꾼다.

    Args:
        column: 컬럼.
        owner: 새 소유자.

    Returns:
        컬럼.
    """
    return SaColumn(column.key, column.column, owner, column.primary_key, column.node, column.path)


def _primary_key(call: ast.Call) -> bool:
    """호출에 `primary_key=True`가 있는지 본다.

    Args:
        call: 호출.

    Returns:
        있으면 True.
    """
    return any(
        keyword.arg == "primary_key" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
        for keyword in call.keywords
    )


def _assignment(statement: ast.stmt) -> tuple[str | None, ast.expr | None]:
    """단순 이름 대입이면 (이름, 값)을 돌려준다. 값 없는 주석이면 값은 None이다.

    Args:
        statement: 문장.

    Returns:
        (이름, 값) 또는 (None, None).
    """
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
        return statement.targets[0].id, statement.value
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target.id, statement.value
    return None, None


def _target_node(statement: ast.stmt) -> ast.AST:
    """대입문의 대상 노드(위치용)를 돌려준다.

    Args:
        statement: 대입문.

    Returns:
        대상 노드.
    """
    if isinstance(statement, ast.Assign):
        return statement.targets[0]
    if isinstance(statement, ast.AnnAssign):
        return statement.target
    return statement
