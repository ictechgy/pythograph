"""ORM 이름 규칙: Django 기본 테이블 이름·식별자 절단·따옴표 규칙, Flask-SQLAlchemy snake_case.

모든 규칙은 설치한 패키지 소스로 확인했다(Django 5.2.17, SQLAlchemy 2.0.54, Flask-SQLAlchemy 3.1.1 —
`docs/PERSISTENCE.md`). 합성 명명 벡터(`fixtures/persistence-naming/vectors.json`)가 실제 ORM이 만든 이름과
100% 같은지 오라클로 확인한다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: Flask-SQLAlchemy 3.x `camel_to_snake_case`의 정규식이다(3.0.5·3.1.1 `flask_sqlalchemy/model.py`).
_CAMEL_BOUNDARY = re.compile(r"((?<=[a-z0-9])[A-Z]|(?!^)[A-Z](?=[a-z]))")

#: Flask-SQLAlchemy 2.x `camelcase_re`다(2.5.1 `flask_sqlalchemy/model.py`).
_CAMEL_RUN_V2 = re.compile(r"([A-Z]+)(?=[a-z0-9])")


def camel_to_snake_case(name: str) -> str:
    """Flask-SQLAlchemy 3의 자동 `__tablename__` 규칙이다.

    `flask_sqlalchemy/model.py` `camel_to_snake_case`와 같은 정규식으로 경계에 `_`를 넣고 소문자로 바꾼 뒤
    앞 `_`를 벗긴다(`HTTPRequest` → `http_request`, `User2FA` → `user2_fa`).

    Args:
        name: 클래스 이름.

    Returns:
        테이블 이름.
    """
    return _CAMEL_BOUNDARY.sub(r"_\1", name).lower().lstrip("_")


def camel_to_snake_case_v2(name: str) -> str:
    """Flask-SQLAlchemy 2.x의 자동 `__tablename__` 규칙이다(2.5.1 소스).

    대문자 연속 뒤에 소문자·숫자가 오면 연속이 두 글자 이상일 때 마지막 대문자 앞에도 `_`를 넣는다
    (`HTTPRequest` → `http_request`). 맞지 않은 대문자는 소문자로 바꾸지 않는다(`User2FA` → `user2FA`).

    Args:
        name: 클래스 이름.

    Returns:
        테이블 이름.
    """

    def join(match: re.Match[str]) -> str:
        """대문자 연속 하나를 바꾼다.

        Args:
            match: 대문자 연속.

        Returns:
            바꾼 조각.
        """
        word = match.group()
        return f"_{word[:-1]}_{word[-1]}".lower() if len(word) > 1 else "_" + word.lower()

    return _CAMEL_RUN_V2.sub(join, name).lstrip("_")


#: Flask-SQLAlchemy 메이저 → 자동 이름 함수다.
FSA_NAMING = {2: camel_to_snake_case_v2, 3: camel_to_snake_case}


def django_names_digest(name: str, length: int) -> str:
    """Django `names_digest`(md5 16진 앞부분)다. 보안 용도가 아니라 이름 재현용이다.

    Args:
        name: 이름.
        length: 자를 길이.

    Returns:
        16진 다이제스트 앞 `length`자.
    """
    return hashlib.md5(name.encode(), usedforsecurity=False).hexdigest()[:length]


def django_split_identifier(identifier: str) -> tuple[str, str]:
    """Django `split_identifier`: `USER"."TABLE` 꼴을 (네임스페이스, 이름)으로 나눈다.

    Args:
        identifier: 식별자.

    Returns:
        (네임스페이스, 이름). 네임스페이스가 없으면 빈 문자열.
    """
    parts = identifier.split('"."')
    if len(parts) != 2:
        return "", identifier.strip('"')
    return parts[0].strip('"'), parts[1].strip('"')


def django_truncate_name(identifier: str, length: int | None, hash_len: int = 4) -> str:
    """Django `truncate_name`(`django/db/backends/utils.py`)을 그대로 옮겼다.

    이름 부분이 `length`보다 길면 앞 `length - hash_len`자에 전체 이름의 md5 앞 `hash_len`자를 붙인다.
    네임스페이스가 있으면 이름 부분만 자른다.

    Args:
        identifier: 식별자.
        length: 최대 길이(None이면 자르지 않는다).
        hash_len: 해시 길이.

    Returns:
        자른 식별자.
    """
    namespace, name = django_split_identifier(identifier)
    if length is None or len(name) <= length:
        return identifier
    digest = django_names_digest(name, hash_len)
    prefix = f'{namespace}"."' if namespace else ""
    return f"{prefix}{name[: length - hash_len]}{digest}"


def django_strip_quotes(name: str) -> str:
    """Django `strip_quotes`: 양끝이 모두 `"`면 벗긴다.

    Args:
        name: 이름.

    Returns:
        벗긴 이름.
    """
    return name[1:-1] if len(name) >= 2 and name.startswith('"') and name.endswith('"') else name


@dataclass(frozen=True)
class DjangoBackend:
    """Django 데이터베이스 백엔드의 이름 규칙.

    Attributes:
        key: `sqlite`·`postgresql`·`mysql`·`oracle`.
        max_name_length: `connection.ops.max_name_length()`(sqlite는 None).
    """

    key: str
    max_name_length: int | None


#: 확인한 백엔드다(`django/db/backends/*/operations.py` `max_name_length`).
SQLITE = DjangoBackend("sqlite", None)
POSTGRESQL = DjangoBackend("postgresql", 63)
MYSQL = DjangoBackend("mysql", 64)
ORACLE = DjangoBackend("oracle", 30)

#: 백엔드를 모를 때 비교할 후보다.
ALL_BACKENDS = (SQLITE, POSTGRESQL, MYSQL, ORACLE)


def django_generated_name(name: str, backend: DjangoBackend) -> str:
    """Django가 만든 기본 이름(기본 테이블·M2M 테이블)에 `Options`·`_get_m2m_db_table`의 절단을 적용한다.

    Args:
        name: 자르기 전 이름.
        backend: 백엔드.

    Returns:
        `truncate_name(name, max_name_length)`.
    """
    return django_truncate_name(name, backend.max_name_length)


def django_quote_name(identifier: str, backend: DjangoBackend) -> str:
    """백엔드 `DatabaseOperations.quote_name`을 옮겼다(대소문자 변환 제외).

    - sqlite·PostgreSQL: 양끝이 `"`면 그대로, 아니면 `"…"`로 감싼다.
    - MySQL: 양끝이 `` ` ``면 그대로, 아니면 `` `…` ``로 감싼다.
    - Oracle: `"`로 시작하지도 끝나지도 않으면 30자로 자르고(`truncate_name`) `"…"`로 감싼다. Oracle은 대문자로
      바꾸지만 isthmus는 소문자로 접어 조인하므로 여기서는 바꾸지 않는다.

    Args:
        identifier: Django가 SQL에 넣는 이름.
        backend: 백엔드.

    Returns:
        인용한 SQL 식별자.
    """
    if backend is ORACLE:
        if not identifier.startswith('"') and not identifier.endswith('"'):
            return f'"{django_truncate_name(identifier, ORACLE.max_name_length)}"'
        return identifier
    mark = "`" if backend is MYSQL else '"'
    if len(identifier) >= 2 and identifier.startswith(mark) and identifier.endswith(mark):
        return identifier
    return f"{mark}{identifier}{mark}"


def django_final_segments(identifier: str, backend: DjangoBackend) -> tuple[str, ...] | None:
    """백엔드가 실제 카탈로그에 만드는 이름 조각을 구한다.

    `quote_name` 결과를 인용 부호 기준으로 나눈다: `'"s"."t"'`는 sqlite·PostgreSQL·Oracle에서 두 식별자(`s.t`)지만
    MySQL에서는 따옴표를 담은 한 식별자다. PostgreSQL 서버는 63바이트보다 긴 식별자를 조용히 자르고 MySQL은
    64자보다 긴 식별자를 거부한다. Django는 명시 이름을 자르지 않으므로 이때 카탈로그 이름을 확정할 수 없어
    None이다.

    Args:
        identifier: Django가 SQL에 넣는 이름.
        backend: 백엔드.

    Returns:
        이름 조각, 확정할 수 없으면 None.
    """
    quoted = django_quote_name(identifier, backend)
    mark = quoted[0]
    inner = quoted[1:-1] if len(quoted) >= 2 and quoted[-1] == mark else quoted
    segments = tuple(inner.split(f"{mark}.{mark}"))
    if backend is POSTGRESQL and any(len(segment.encode()) > 63 for segment in segments):
        return None
    if backend is MYSQL and any(len(segment) > 64 for segment in segments):
        return None
    return segments
