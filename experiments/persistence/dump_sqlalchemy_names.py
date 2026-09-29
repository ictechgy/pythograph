"""SQLAlchemy·Flask-SQLAlchemy가 실제로 만드는 테이블·컬럼 이름을 덤프한다(명명 벡터 오라클).

사용법(scratch venv의 python으로): python dump_sqlalchemy_names.py <fixture> <모듈> <출력.json>
모듈을 import하고 모든 registry의 매퍼와 MetaData 테이블을 읽는다. 매핑 클래스마다 `__table__`(단일 테이블
상속이면 부모 테이블)과 컬럼 속성의 (테이블, 컬럼)을 기록한다. 데이터베이스에 연결하지 않는다.
"""

import gc
import importlib
import json
import sys
from pathlib import Path


def table_name(table):
    """테이블의 한정 이름 조각을 만든다.

    :param table: SQLAlchemy Table
    :returns: 조각 목록
    """
    return ([table.schema] if table.schema else []) + [table.name]


def mappers():
    """살아 있는 모든 registry의 매퍼를 모은다.

    :returns: 매퍼 목록
    """
    from sqlalchemy.orm import registry

    found = []
    for item in gc.get_objects():
        if isinstance(item, registry):
            found.extend(item.mappers)
    return found


def dump(fixture, module_name):
    """매핑 클래스와 Core 테이블의 이름을 모은다.

    :param fixture: fixture 경로
    :param module_name: import할 모듈
    :returns: 덤프 사전
    """
    sys.path.insert(0, str(fixture))
    module = importlib.import_module(module_name)
    classes = {}
    metadatas = set()
    for mapper in mappers():
        cls = mapper.class_
        if not cls.__module__.startswith(module_name.split(".")[0]):
            continue
        metadatas.add(id(mapper.local_table.metadata))
        columns = {}
        for attribute in mapper.column_attrs:
            column = attribute.columns[0]
            columns[attribute.key] = table_name(column.table) + [column.name]
        classes[f"{cls.__module__}.{cls.__qualname__}"] = {"table": table_name(mapper.local_table), "columns": columns}
    tables = {}
    for mapper in mappers():
        for table in mapper.local_table.metadata.tables.values():
            tables[".".join(table_name(table))] = sorted(column.name for column in table.columns)
    del module
    return {"classes": classes, "tables": tables}


def main(argv):
    """덤프를 파일로 쓴다.

    :param argv: fixture, 모듈, 출력 경로
    """
    fixture, module_name, output = argv
    value = dump(Path(fixture).resolve(), module_name)
    Path(output).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
