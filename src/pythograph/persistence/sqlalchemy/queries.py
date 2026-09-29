"""SQLAlchemy·Flask-SQLAlchemy 매핑 선언과 사용 → relation-use 사실.

선언: 매핑 클래스(클래스 위치의 관계 사실, 컬럼 선언 위치의 컬럼 사실), Core `Table(...)`, `ForeignKey("t.c")`,
`relationship(secondary=...)`.

사용(값의 출처를 구문으로 증명할 때만):

- 엔터티 자리: `select`·`insert`·`update`·`delete`·`aliased`·`exists`의 인자, `.query(...)`·`.join(...)`·
  `.outerjoin(...)`·`.select_from(...)`·`.with_entities(...)`·`.add_columns(...)`의 인자, `.get(M, …)`·
  `.get_or_404(M, …)`의 첫 인자 — 매핑 클래스·그 컬럼 속성·Core 테이블이면 관계 사실.
- `Model.column` 속성은 컬럼 사실, `Model.relationship`은 대상(과 연결) 테이블 사실, Flask-SQLAlchemy
  `Model.query`와 `Model.__table__`은 관계 사실, Core `table.c.name`은 컬럼 사실이다.
- `filter_by(**kw)`는 마지막 조인 대상(없으면 첫 엔터티)의 컬럼 사실, 생성자 `Model(key=...)`는 컬럼 사실이다.
- `text()`·`exec_driver_sql()`의 인자는 SQL로 읽는다.
"""

from __future__ import annotations

import ast

from pythograph.persistence.location import fact_location
from pythograph.persistence.model import PersistenceExtraction, RelationName
from pythograph.persistence.scope import ModuleScopes
from pythograph.persistence.sqlalchemy.catalog import SaModel, SaTable, SqlAlchemyCatalog
from pythograph.persistence.sqltext import SqlText, expression_text
from pythograph.routes.model import Location

#: 모든 인자가 엔터티 자리인 SQLAlchemy 함수다.
_ENTITY_FUNCTIONS = frozenset({"select", "insert", "update", "delete", "aliased", "exists", "with_polymorphic"})

#: 모든 인자가 엔터티 자리인 메서드다(수신자는 증명하지 않는다 — 인자가 매핑 클래스일 때만 사실이 된다).
_ENTITY_METHODS = frozenset({"query", "join", "outerjoin", "select_from", "with_entities", "add_columns"})

#: 첫 인자만 엔터티 자리인 메서드다.
_FIRST_ENTITY_METHODS = frozenset({"get", "get_or_404"})

#: 인자가 SQL 텍스트인 SQLAlchemy 함수·메서드다.
_SQL_FUNCTIONS = frozenset({"text"})

#: 테이블 객체의 DML 메서드다.
_TABLE_METHODS = frozenset({"select", "insert", "update", "delete"})

#: 지역 이름 출처를 따라가는 최대 깊이다.
MAX_DEPTH = 16


class SqlAlchemyQueries:
    """모듈 하나의 SQLAlchemy 선언과 사용을 사실로 바꾼다."""

    def __init__(
        self,
        catalog: SqlAlchemyCatalog,
        scopes: ModuleScopes,
        extraction: PersistenceExtraction,
        sql: SqlText,
    ) -> None:
        """분석기를 만든다.

        Args:
            catalog: 매핑 목록.
            scopes: 모듈 범위 도우미.
            extraction: 결과.
            sql: SQL 텍스트 수집기.
        """
        self.catalog = catalog
        self.symbols = catalog.symbols
        self.scopes = scopes
        self.extraction = extraction
        self.sql = sql

    def run(self) -> None:
        """선언과 사용 사실을 낸다."""
        for model in self.catalog.models.values():
            if model.path == self.scopes.path and not model.abstract:
                self._definition(model)
        for table in self.catalog.tables.values():
            if table.path == self.scopes.path:
                self._table_definition(table)
        for node in ast.walk(self.scopes.index.module.tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                self._attribute(node)
            elif isinstance(node, ast.Call):
                self._call(node)

    # ------------------------------------------------------------------ 선언

    def _definition(self, model: SaModel) -> None:
        """매핑 클래스 선언의 관계·컬럼 사실을 낸다.

        Args:
            model: 매핑 클래스.
        """
        symbol = self.scopes.symbol_for(model.node.body[0]) if model.node.body else None
        class_location = self._location(model.node)
        relation = self.catalog.relation(model.key)
        if relation is None:
            self.extraction.add_dynamic(
                model.name, class_location, symbol, "{count} SQLAlchemy mapped classes have computed table names"
            )
            return
        self.extraction.add_relation(relation, class_location, symbol)
        for column in model.columns.values():
            owner = self.catalog.relation(column.owner)
            if owner is None or column.column is None:
                continue
            here = column.path == model.path and column.node is not None and self._inside(column.node, model.node)
            location = self._location(column.node) if here and column.node is not None else class_location
            self.extraction.add_column(owner, column.column, location, symbol)

    @staticmethod
    def _inside(node: ast.AST, owner: ast.ClassDef) -> bool:
        """노드가 클래스 본문 안(줄 범위)에 있는지 본다.

        Args:
            node: 노드.
            owner: 클래스.

        Returns:
            안이면 True.
        """
        line = getattr(node, "lineno", 0)
        return owner.lineno <= line <= (owner.end_lineno or owner.lineno)

    def _table_definition(self, table: SaTable) -> None:
        """Core `Table(...)` 선언의 관계·컬럼 사실을 낸다.

        Args:
            table: 테이블.
        """
        symbol = self.scopes.symbol_for(table.node)
        location = self._location(table.node.args[0])
        if table.name is None:
            self.extraction.add_dynamic(
                expression_text(table.node.args[0]), location, symbol, "{count} SQLAlchemy Table names are not literal"
            )
            return
        self.extraction.add_relation(table.name, location, symbol)
        for argument in table.node.args[1:]:
            if isinstance(argument, ast.Call) and self.catalog.sqlalchemy_name(self.scopes.path, argument.func) in (
                "Column",
                "column",
            ):
                column = self.catalog.column_name(self.scopes.path, argument, None)
                if column is not None:
                    target = argument.args[0] if argument.args else argument
                    self.extraction.add_column(table.name, column, self._location(target), symbol)

    # ------------------------------------------------------------------ 사용

    def _attribute(self, node: ast.Attribute) -> None:
        """매핑 클래스·Core 테이블의 속성 접근을 사실로 낸다.

        Args:
            node: 속성 접근.
        """
        model = self.model_of(node.value)
        if model is not None:
            self._model_attribute(model, node)
            return
        if node.attr in ("c", "columns"):
            return
        if isinstance(node.value, ast.Attribute) and node.value.attr in ("c", "columns"):
            table = self._table_of(node.value.value)
            if table is not None:
                self.extraction.add_column(table, node.attr, self._location(node), self.scopes.symbol_for(node))

    def _model_attribute(self, key: str, node: ast.Attribute) -> None:
        """`Model.<속성>` 하나를 사실로 낸다.

        Args:
            key: 클래스 키.
            node: 속성 접근.
        """
        model = self.catalog.models[key]
        location, symbol = self._location(node), self.scopes.symbol_for(node)
        column = model.columns.get(node.attr)
        if column is not None and column.column is not None:
            owner = self.catalog.relation(column.owner)
            if owner is not None:
                self.extraction.add_column(owner, column.column, location, symbol)
            return
        relationship = model.relationships.get(node.attr)
        if relationship is not None:
            self._relationship(relationship.target, relationship.secondary, node)
        elif (node.attr == "query" and model.base.fsa) or node.attr == "__table__":
            self._emit_model(key, node)

    def _relationship(self, target: str | None, secondary: object, node: ast.AST) -> None:
        """관계 속성 사용을 대상·연결 테이블 사실로 낸다.

        Args:
            target: 대상 클래스 키.
            secondary: 연결 테이블 이름.
            node: 위치 노드.
        """
        location, symbol = self._location(node), self.scopes.symbol_for(node)
        if isinstance(secondary, RelationName):
            self.extraction.add_relation(secondary, location, symbol)
        elif secondary is not None:
            self.extraction.add_dynamic(
                expression_text(node), location, symbol, "{count} relationship secondary tables are not literal"
            )
        if target is not None:
            self._emit_model(target, node)

    def _call(self, node: ast.Call) -> None:
        """엔터티 자리·`filter_by`·생성자·SQL 텍스트·외래 키 호출을 사실로 낸다.

        Args:
            node: 호출.
        """
        name = self.catalog.sqlalchemy_name(self.scopes.path, node.func)
        if name in _ENTITY_FUNCTIONS:
            for argument in node.args:
                self._entity(argument, unresolved_dynamic=name != "exists")
        elif name in _SQL_FUNCTIONS and node.args:
            self.sql.sink(node.args[0], explicit=True)
        elif name == "ForeignKey" and node.args:
            self._foreign_key(node.args[0])
        elif name == "ForeignKeyConstraint" and len(node.args) >= 2:
            elements = node.args[1].elts if isinstance(node.args[1], (ast.List, ast.Tuple)) else []
            for element in elements:
                self._foreign_key(element)
        elif name == "relationship":
            self._secondary_literal(node)
        else:
            # Flask-SQLAlchemy `db.get_or_404(...)`처럼 SQLAlchemy 이름이 아닌 메서드도 여기서 읽는다.
            self._method_or_constructor(node)

    def _method_or_constructor(self, node: ast.Call) -> None:
        """엔터티 메서드·`filter_by`·`exec_driver_sql`·테이블 DML 메서드·매핑 클래스 생성자를 처리한다.

        Args:
            node: 호출.
        """
        function = node.func
        if isinstance(function, ast.Attribute):
            if function.attr in _ENTITY_METHODS:
                for argument in node.args:
                    self._entity(argument, unresolved_dynamic=False)
            elif function.attr in _FIRST_ENTITY_METHODS and node.args:
                self._entity(node.args[0], unresolved_dynamic=False)
            elif function.attr == "filter_by":
                self._filter_by(function.value, node)
            elif function.attr == "exec_driver_sql" and node.args:
                self.sql.sink(node.args[0], explicit=True)
            elif function.attr in _TABLE_METHODS:
                table = self._table_of(function.value)
                if table is not None:
                    self.extraction.add_relation(table, self._location(function), self.scopes.symbol_for(node))
            return
        model = self.model_of(function)
        if model is not None:
            self._emit_model(model, function)
            self._keyword_columns(model, node)

    def _entity(self, argument: ast.expr, unresolved_dynamic: bool) -> None:
        """엔터티 자리 인자 하나를 관계 사실로 낸다.

        Args:
            argument: 인자.
            unresolved_dynamic: 풀지 못한 이름을 dynamic 사실로 남길지(`select` 등 SQLAlchemy 함수만).
        """
        model = self.model_of(argument)
        if model is not None:
            self._emit_model(model, argument)
            return
        if isinstance(argument, ast.Attribute):
            owner = self.model_of(argument.value)
            if owner is not None:
                column = self.catalog.models[owner].columns.get(argument.attr)
                if column is not None:
                    self._emit_model(column.owner, argument.value)
                return
        table = self._table_of(argument)
        if table is not None:
            self.extraction.add_relation(table, self._location(argument), self.scopes.symbol_for(argument))
            return
        if (
            isinstance(argument, ast.Call)
            and self.catalog.sqlalchemy_name(self.scopes.path, argument.func) == "aliased"
        ):
            return
        if unresolved_dynamic and self._unresolved_reference(argument):
            self.extraction.add_dynamic(
                expression_text(argument), self._location(argument), self.scopes.symbol_for(argument),
                "{count} SQLAlchemy statement entities could not be resolved to a mapped class or table",
            )  # fmt: skip

    def _unresolved_reference(self, argument: ast.expr) -> bool:
        """인자가 아무것도 가리키지 못하는 이름·속성(모델일 수 있는 값)인지 본다.

        Args:
            argument: 인자.

        Returns:
            그러면 True.
        """
        if not isinstance(argument, (ast.Name, ast.Attribute)):
            return False
        if isinstance(argument, ast.Attribute) and self.model_of(argument.value) is not None:
            return False
        if self.catalog.sqlalchemy_name(self.scopes.path, argument) is not None:
            return False
        root: ast.expr = argument
        while isinstance(root, ast.Attribute):
            root = root.value
        if not isinstance(root, ast.Name):
            return False
        function = self.scopes.enclosing_function(argument)
        if function is not None and self.scopes.binds_locally(function, root.id):
            binding = self.scopes.local(function, root.id)
            return binding is None or binding.kind == "param" or binding.value is None
        resolved = self.symbols.resolve_expr(self.scopes.path, argument)
        return resolved is None or getattr(resolved, "dotted", "").split(".")[0] not in (
            "sqlalchemy",
            "flask_sqlalchemy",
        )

    def _filter_by(self, receiver: ast.expr, node: ast.Call) -> None:
        """`filter_by(**kw)`의 키워드를 엔터티의 컬럼 사실로 낸다.

        Args:
            receiver: 수신 식.
            node: 호출.
        """
        model = self._query_entity(receiver, 0)
        if model is None:
            return
        self._keyword_columns(model, node)

    def _query_entity(self, receiver: ast.expr, depth: int) -> str | None:
        """질의 사슬의 `filter_by` 대상 엔터티(마지막 조인 대상, 없으면 첫 엔터티)를 찾는다.

        Args:
            receiver: 수신 식.
            depth: 깊이.

        Returns:
            클래스 키 또는 None.
        """
        current = receiver
        while depth <= MAX_DEPTH:
            depth += 1
            if isinstance(current, ast.Attribute) and current.attr == "query":
                return self.model_of(current.value)
            if not isinstance(current, ast.Call):
                return self._bound_entity(current, depth)
            name = self.catalog.sqlalchemy_name(self.scopes.path, current.func)
            if name == "select" and current.args:
                return self.model_of(current.args[0])
            if not isinstance(current.func, ast.Attribute):
                return None
            if current.func.attr in ("join", "outerjoin", "query") and current.args:
                found = self.model_of(current.args[0])
                if found is not None or current.func.attr == "query":
                    return found
            current = current.func.value
        return None

    def _bound_entity(self, node: ast.expr, depth: int) -> str | None:
        """지역 이름에 대입한 질의의 엔터티를 찾는다.

        Args:
            node: 식.
            depth: 깊이.

        Returns:
            클래스 키 또는 None.
        """
        if not isinstance(node, ast.Name):
            return None
        function = self.scopes.enclosing_function(node)
        binding = self.scopes.local(function, node.id) if function is not None else None
        if binding is None or binding.kind != "assign" or binding.value is None:
            return None
        return self._query_entity(binding.value, depth + 1)

    def _keyword_columns(self, key: str, node: ast.Call) -> None:
        """키워드 인자 이름(속성 이름)을 컬럼·관계 사실로 낸다.

        Args:
            key: 클래스 키.
            node: 호출.
        """
        model = self.catalog.models[key]
        for keyword in node.keywords:
            if keyword.arg is None:
                continue
            column = model.columns.get(keyword.arg)
            relationship = model.relationships.get(keyword.arg)
            if column is not None and column.column is not None:
                owner = self.catalog.relation(column.owner)
                if owner is not None:
                    self.extraction.add_column(
                        owner, column.column, self._location(keyword), self.scopes.symbol_for(node)
                    )
            elif relationship is not None:
                self._relationship(relationship.target, relationship.secondary, keyword)

    def _foreign_key(self, argument: ast.expr) -> None:
        """`ForeignKey("schema.table.column")` 문자열을 관계·컬럼 사실로 낸다.

        Args:
            argument: 첫 인자.
        """
        if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
            return
        parts = argument.value.split(".")
        if len(parts) < 2 or not all(parts):
            return
        relation = RelationName(tuple(parts[:-1]))
        location, symbol = self._location(argument), self.scopes.symbol_for(argument)
        self.extraction.add_relation(relation, location, symbol)
        self.extraction.add_column(relation, parts[-1], location, symbol)

    def _secondary_literal(self, node: ast.Call) -> None:
        """`relationship(secondary="table")` 선언을 관계 사실로 낸다.

        Args:
            node: 관계 호출.
        """
        for keyword in node.keywords:
            value = keyword.value
            if keyword.arg == "secondary" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                relation = RelationName(tuple(value.value.split(".")))
                self.extraction.add_relation(relation, self._location(value), self.scopes.symbol_for(node))

    # ------------------------------------------------------------------ 분류

    def model_of(self, node: ast.expr, depth: int = 0) -> str | None:
        """식이 매핑 클래스(또는 그 `aliased`)면 키를 돌려준다.

        Args:
            node: 식.
            depth: 깊이.

        Returns:
            클래스 키 또는 None.
        """
        if depth > MAX_DEPTH:
            return None
        if isinstance(node, ast.Call):
            if self.catalog.sqlalchemy_name(self.scopes.path, node.func) == "aliased" and node.args:
                return self.model_of(node.args[0], depth + 1)
            return None
        if isinstance(node, ast.Name):
            function = self.scopes.enclosing_function(node)
            if function is not None and self.scopes.binds_locally(function, node.id):
                binding = self.scopes.local(function, node.id)
                if binding is None or binding.kind != "assign" or binding.value is None:
                    return None
                return self.model_of(binding.value, depth + 1)
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self.catalog.model_for_symbol(self.symbols.resolve_expr(self.scopes.path, node))
        return None

    def _table_of(self, node: ast.expr, depth: int = 0) -> RelationName | None:
        """식이 Core 테이블(모듈 이름, 지역 대입, `Model.__table__`)이면 이름을 돌려준다.

        Args:
            node: 식.
            depth: 깊이.

        Returns:
            관계 이름 또는 None.
        """
        if depth > MAX_DEPTH:
            return None
        if isinstance(node, ast.Attribute) and node.attr == "__table__":
            model = self.model_of(node.value)
            return self.catalog.relation(model) if model is not None else None
        if isinstance(node, ast.Call) and id(node) in self.catalog.tables:
            return self.catalog.tables[id(node)].name
        if isinstance(node, ast.Name):
            function = self.scopes.enclosing_function(node)
            if function is not None and self.scopes.binds_locally(function, node.id):
                binding = self.scopes.local(function, node.id)
                if binding is None or binding.kind != "assign" or binding.value is None:
                    return None
                return self._table_of(binding.value, depth + 1)
        if isinstance(node, (ast.Name, ast.Attribute)):
            table = self.catalog.table_for_symbol(self.symbols.resolve_expr(self.scopes.path, node))
            return table.name if table is not None else None
        return None

    def _emit_model(self, key: str, node: ast.AST) -> None:
        """매핑 클래스의 테이블(조인 상속이면 조상 테이블도) 관계 사실을 낸다.

        Args:
            key: 클래스 키.
            node: 위치 노드.
        """
        location, symbol = self._location(node), self.scopes.symbol_for(node)
        relation = self.catalog.relation(key)
        if relation is None:
            self.extraction.add_dynamic(
                self.catalog.models[key].name, location, symbol,
                "{count} SQLAlchemy mapped classes have computed table names",
            )  # fmt: skip
            return
        self.extraction.add_relation(relation, location, symbol)
        for ancestor in self.catalog.ancestors(key):
            parent = self.catalog.relation(ancestor)
            if parent is not None:
                self.extraction.add_relation(parent, location, symbol)

    def _location(self, node: ast.AST) -> Location:
        """노드 위치를 만든다. 속성 접근은 속성 이름을 가리킨다.

        Args:
            node: 노드.

        Returns:
            위치.
        """
        return fact_location(self.scopes, node)
