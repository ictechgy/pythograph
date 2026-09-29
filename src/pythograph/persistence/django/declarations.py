"""Django 모델 선언 → relation-use 사실.

모델 클래스(추상 제외)는 클래스 위치에 자기 테이블 관계 사실을, 필드는 선언 위치에 컬럼 사실을 낸다. 외래
키·일대일은 대상 테이블 관계 사실(제약이 가리키는 테이블)을, M2M은 중간 테이블과 두 컬럼, 대상 테이블 사실을
함께 낸다. 부모에서 받은 필드·자동 `id`·다중 테이블 상속 연결 컬럼은 클래스 위치에 낸다. 프록시 모델은 구체
부모 테이블 관계 사실만 낸다.
"""

from __future__ import annotations

import ast

from pythograph.persistence.django.catalog import DjangoCatalog, DjangoModel, ResolvedField
from pythograph.persistence.location import fact_location
from pythograph.persistence.model import PersistenceExtraction
from pythograph.persistence.scope import ModuleScopes
from pythograph.source.symbols import symbol_id


class DjangoDeclarations:
    """모듈 하나에 정의된 Django 모델의 선언 사실을 낸다."""

    def __init__(self, catalog: DjangoCatalog, scopes: ModuleScopes, extraction: PersistenceExtraction) -> None:
        """생성한다.

        Args:
            catalog: 모델 목록.
            scopes: 모듈 범위 도우미.
            extraction: 결과.
        """
        self.catalog = catalog
        self.scopes = scopes
        self.extraction = extraction

    def run(self) -> None:
        """이 모듈에 정의된 구체 모델마다 선언 사실을 낸다."""
        for model in self.catalog.models.values():
            if model.path == self.scopes.path and not model.abstract and model.node is not None:
                self._model(model, model.node)

    def _model(self, model: DjangoModel, node: ast.ClassDef) -> None:
        """모델 하나의 선언 사실을 낸다.

        Args:
            model: 모델.
            node: 클래스 정의.
        """
        symbol = symbol_id(self.scopes.path, self.scopes.index.qualname(node))
        location = fact_location(self.scopes, node)
        relation = self.catalog.relation(model.concrete)
        if relation is None:
            self.extraction.add_dynamic(
                model.name, location, symbol,
                "{count} Django model tables depend on an unresolved app label or database backend",
            )  # fmt: skip
            return
        self.extraction.add_relation(relation, location, symbol)
        if model.proxy:
            return
        for field in model.fields.values():
            if field.owner == model.key:
                self._field(field, node, symbol)

    def _field(self, field: ResolvedField, owner: ast.ClassDef, symbol: str) -> None:
        """필드 하나의 컬럼·관계 사실을 낸다.

        Args:
            field: 필드.
            owner: 모델 클래스 정의.
            symbol: 모델 심볼 id.
        """
        local = field.node is not None and field.path == self.scopes.path and _inside(field.node, owner)
        location = fact_location(self.scopes, field.node if local and field.node is not None else owner)
        relation = self.catalog.relation(field.owner)
        if relation is None:
            return
        if field.kind == "m2m" and field.m2m is not None:
            through = self.catalog.m2m_relation(field.m2m)
            if through is None:
                self.extraction.add_dynamic(
                    field.name, location, symbol, "{count} Django many-to-many tables could not be named statically"
                )
            elif field.m2m.through is None:
                self.extraction.add_relation(through, location, symbol)
                for column in (field.m2m.source_column, field.m2m.target_column):
                    name = self.catalog.column(column)
                    if name is not None:
                        self.extraction.add_column(through, name, location, symbol)
        elif field.column is None and field.kind != "unknown":
            self.extraction.add_dynamic(
                field.name,
                location,
                symbol,
                "{count} Django column names depend on the database backend or are not literal",
            )
        elif field.column is not None:
            column = self.catalog.column(field.column)
            if column is None:
                self.extraction.add_dynamic(
                    field.column, location, symbol,
                    "{count} Django column names depend on the database backend or are not literal",
                )  # fmt: skip
            else:
                self.extraction.add_column(relation, column, location, symbol)
        target = self.catalog.relation(field.target) if field.target is not None else None
        if target is not None:
            self.extraction.add_relation(target, location, symbol)


def _inside(node: ast.AST, owner: ast.ClassDef) -> bool:
    """노드가 클래스 본문 줄 범위 안에 있는지 본다.

    Args:
        node: 노드.
        owner: 클래스.

    Returns:
        안이면 True.
    """
    line = getattr(node, "lineno", 0)
    return owner.lineno <= line <= (owner.end_lineno or owner.lineno)
