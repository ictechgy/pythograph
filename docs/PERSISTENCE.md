# persistence 관계 사용 규칙 (`pythograph schema`)

pythograph가 Django ORM·SQLAlchemy 2.x·Flask-SQLAlchemy 3·SQL 텍스트에서 isthmus persistence `relation-use`
사실을 만드는 규칙과, 각 규칙을 확인한 공식 소스를 적는다. 규칙은 추정하지 않고 아래 버전의 설치 패키지 소스를
직접 읽어 확인했다(PyPI에서 스크래치 가상 환경에 설치, 2026-09-29). 이름 규칙은 합성 명명 벡터로 실제 ORM이
만든 이름과 100% 같은지 확인한다([명명 벡터](#명명-벡터)).

| 패키지 | 확인한 버전 | 쓰는 곳 |
|---|---|---|
| Django | 5.2.17 | 모델 테이블·컬럼·M2M·앱 라벨·식별자 절단, QuerySet 조회식 |
| SQLAlchemy | 2.0.54 | Declarative·Core 테이블·컬럼 이름, 질의 구성 |
| Flask-SQLAlchemy | 3.1.1(3.0.5 정규식 동일), 2.5.1(비교용) | `db.Model` 자동 `__tablename__` |

분석 대상 코드는 표준 라이브러리 `ast`로만 읽는다. import·실행·네트워크 접근을 하지 않는다.

## 명령과 출력

```sh
pythograph schema --project <root> [--include-tests] [--settings <module>] [--generated-at <timestamp>] [--format json]
```

- 문서: bridge-facts v1, `platform: "python"`, `target: "persistence"`(사실이 없으면 `null`), 사실은 모두
  `relation-use`다. 계약은 isthmus `docs/GRAPH-EXCHANGE.md`의 `target: "persistence"` 절이다.
- `channel`: 관계 이름. 코드나 ORM 규칙이 한정하면 `schema.table`, 아니면 비한정 그대로 쓴다(기본 스키마를
  추측하지 않는다 — isthmus가 비한정 사용을 마지막 조각으로 잇는다). 이름 자체에 든 `.`은 `%2E`, `%`는 `%25`다.
- `method`: 컬럼 이름. 컬럼 사실은 관계 사실을 함축하지 않으므로 관계 사실을 따로 낸다.
- `dynamic: true`: 이름을 확정하지 못한 사용. `channel`은 원문 표현(최대 200자)이고 컬럼은 싣지 않는다.
  isthmus는 이것을 직접 세어 `unjoined-dynamic-relations`로 `relation-decl-without-use`를 `-unverified`로 내린다.
- `symbol`: 사실을 담은 가장 안쪽 함수·클래스의 id(`<프로젝트 상대 경로>#<어휘적 점 경로>`, routes와 같은
  규칙)를 `qualifiedName`과 `usr`에 싣는다. 모델 선언 사실은 모델 클래스 id다. 모듈 수준 사실에는 symbol이 없고
  `missing-relation-usrs:`로 센다. 다음 단계의 호출 그래프가 같은 id를 쓰므로 isthmus `trace`가 문자열로 잇는다.
- 종료 코드는 routes와 같다(0 성공·2 입력 오류·64 사용법 오류, 1 예약). 사실 0건도 성공이며 완전성의 증거가
  아니다.

읽지 않는 파일: 테스트 소스(`--include-tests`가 없을 때, `test-sources-excluded:`로 셈), 마이그레이션(Django
`migrations/`, Alembic·Flask-Migrate `alembic/versions/`·`migrations/versions/`, `skipped-migration-sources:`로 셈).
마이그레이션은 과거 스키마를 기술하므로 지금 카탈로그와 조인하면 거짓 `relation-use-without-decl`이 된다.

## Django

### 이름 규칙

| 규칙 | 확인한 소스(Django 5.2.17) |
|---|---|
| 기본 테이블은 `"%s_%s" % (app_label, model_name)`을 `truncate_name(…, connection.ops.max_name_length())`로 자른 것이다. `model_name`은 클래스 이름 소문자. `Meta.db_table`은 자르지 않는다 | `django/db/models/options.py` `Options.contribute_to_class` |
| `truncate_name`: 이름 부분이 길이를 넘으면 앞 `length - 4`자 + 전체 이름 md5 16진 앞 4자. `USER"."TABLE` 꼴이면 이름 부분만 자른다 | `django/db/backends/utils.py` `truncate_name`·`names_digest`·`split_identifier` |
| `max_name_length`: PostgreSQL 63, MySQL 64, Oracle 30, SQLite 없음(기본 `None`) | `django/db/backends/*/operations.py` |
| `quote_name`: SQLite·PostgreSQL은 `"…"`(이미 감쌌으면 그대로), MySQL은 `` `…` ``, Oracle은 따옴표로 시작·끝나지 않으면 30자로 한 번 더 자르고 대문자로 감싼다 | 같은 파일 `quote_name` |
| 앱 라벨: `Meta.app_label`, 없으면 모듈을 담는 설치 앱(이름이 같거나 `이름.`으로 시작하는 가장 긴 앱)의 라벨 | `django/db/models/base.py` `ModelBase.__new__`, `django/apps/registry.py` `get_containing_app_config` |
| `INSTALLED_APPS` 원소: 모듈이면 `apps` 하위 모듈에서 `default = False`가 아닌 `AppConfig` 하위 클래스(import한 것 포함) 하나, 아니면 `default = True`인 것 하나, 없으면 기본 `AppConfig`. 클래스 경로면 그 클래스. 앱 이름은 클래스 `name`, 라벨은 `label` 또는 이름의 마지막 조각 | `django/apps/config.py` `AppConfig.create`·`__init__` |
| `Meta`가 없으면 클래스 속성 `Meta`(추상 부모 것만 남는다)를 쓰고, 그때 `abstract`는 물려받지 않는다. `class Meta(Base.Meta)`는 파이썬 상속 | `ModelBase.__new__` |
| 추상 모델 필드는 자식 테이블에 복사, 프록시는 구체 부모 테이블, 다중 테이블 상속 자식은 `<부모>_ptr`(명시 `parent_link`가 없을 때) 일대일 필드와 자기 테이블 | `ModelBase.__new__`, `Options.setup_proxy` |
| 기본 키: 선언한 `primary_key=True`, 없으면 첫 부모 연결 필드, 없으면 `id` 자동 필드 | `Options._prepare`, `Options.add_field` |
| 컬럼: `db_column` 또는 `attname`. 외래 키·일대일 `attname`은 `<이름>_id`. `Field(verbose_name, name, …)`의 `name`은 필드 이름을 바꾼다 | `django/db/models/fields/__init__.py` `Field.get_attname_column`, `fields/related.py` `ForeignKey.get_attname` |
| M2M 중간 테이블: `through` 모델 테이블, `db_table`, 또는 `truncate_name("%s_%s" % (strip_quotes(모델 db_table), 필드 이름), …)`. 자동 중간 모델 컬럼은 `<모델>_id`·`<대상>_id`, 자기 참조면 `from_`·`to_` 접두사 | `fields/related.py` `ManyToManyField._get_m2m_db_table`, `create_many_to_many_intermediary_model` |
| 역관계 질의 이름: `related_query_name` → `related_name` → `model_name`. 접근자: `related_name` → `<model_name>_set`(일대일은 `model_name`). `related_name`이 없으면 `Meta.default_related_name`, `%(class)s`·`%(model_name)s`·`%(app_label)s` 치환, `+`로 끝나면 역관계 없음, 대칭 자기 참조 M2M도 없음 | `fields/related.py` `RelatedField.related_query_name`·`contribute_to_class`, `fields/reverse_related.py` `get_accessor_name` |
| django.contrib 모델(`auth.User`·`Group`·`Permission`, `contenttypes.ContentType`, `sessions.Session`, `sites.Site`, `admin.LogEntry`)과 추상 모델(`AbstractUser`·`AbstractBaseUser`·`PermissionsMixin`)의 필드는 표로 둔다. `settings.AUTH_USER_MODEL`·`get_user_model()`은 `AUTH_USER_MODEL`로 푼다 | 설치 패키지 `_meta`(명명 벡터로 확인) |

**백엔드 선택.** 설정 모듈(routes와 같은 방식으로 찾는다)의 `DATABASES` 모든 항목의 `ENGINE`이 내장 백엔드나
GeoDjango 백엔드면 그 백엔드들이 후보다. 하나라도 모르거나 `DATABASES`를 읽지 못하면(조건부 대입, 부분 변경,
`dj_database_url` 같은 호출) 내장 백엔드 네 개가 모두 후보다. 후보마다 실제 카탈로그 이름을 계산해 모두 같으면
정적 사실, 다르면 dynamic이다(kartograph JPA 명명과 같은 방식). PostgreSQL 서버는 63바이트보다 긴 식별자를
조용히 자르고 MySQL은 64자보다 긴 식별자를 거부하는데 Django는 명시 이름을 자르지 않으므로, 그런 이름은 그
백엔드에서 확정할 수 없다. Oracle의 대문자 변환은 isthmus가 소문자로 접어 조인하므로 반영하지 않는다.

**설정 읽기.** `INSTALLED_APPS`는 대입·`+=`·`append`·`extend`·`insert`를 순서대로 따라가고, 조건문 안의 원소도
"설치될 수 있는 앱"으로 넣는다. 평가하지 못한 원소가 있으면 목록이 불완전하다고 보고, 그때는 알려진 앱의
`models` 모듈(`앱.models`, `앱.models.x`)만 그 앱에 귀속한다. 앱 라벨을 확정하지 못한 기본 이름 테이블은
dynamic이고 `django-app-label-unresolved:`로 센다. 명시 `db_table`은 라벨 없이도 정적이다.

### 사용 규칙

값의 출처를 구문으로 증명할 때만 모델에 귀속한다(형 검사기 없음):

- 모델 클래스: 모델 클래스 이름(import 포함), `get_user_model()`, 리터럴 인자의 `apps.get_model(...)`.
- QuerySet: 매니저(`objects`·선언한 매니저·`as_manager()`·`from_queryset()()`·`_default_manager`), QuerySet 메서드
  사슬, 인스턴스의 관계 매니저(정방향 M2M, 역방향 FK·M2M), 그런 값을 대입한 지역 이름(`qs = qs.filter(...)`
  재대입은 종류를 바꾸지 않는다. 서로 다른 값을 대입한 이름은 모른다).
- 인스턴스: 생성자, `get`·`first`·`last`·`earliest`·`latest`·`create`, `get_object_or_404`, QuerySet 반복 변수,
  첨자, 모델 메서드의 `self`, 모델 주석 매개변수, 정방향 FK·일대일 접근. 비동기 이름(`aget` 등)은 동기와 같다.

| 코드 | 사실 |
|---|---|
| `Model.objects`(매니저 접근), 인스턴스 `save`·`delete`·`refresh_from_db`, 생성자 | 모델 테이블(다중 테이블 상속이면 조상 테이블도) 관계 사실 |
| `filter`·`exclude`·`get`·`get_or_create`·`update_or_create` 키워드, `Q(...)` | 조회식을 `Query.names_to_path`처럼 푼다: 조각마다 필드(이름·`attname`·`pk`)나 역관계, 관계면 대상 모델로 넘어가 조인 테이블 관계 사실과 조인 컬럼 사실. 컬럼 필드에 닿거나 관계 뒤에 조회 이름(`in`·`isnull`·`year` 등)이 오면 끝 |
| `create`·`update` 키워드, `defaults`·`create_defaults` 사전 키, `update_fields`·`bulk_update` 필드 목록, `in_bulk(field_name=)` | 컬럼 사실(조인 없음) |
| `values`·`values_list`·`order_by`(`-` 제거, `?` 무시)·`only`·`defer`·`distinct`·`select_related`·`prefetch_related`·`latest`·`earliest`·`dates`·`datetimes` 문자열 | 필드 경로 |
| `F`·`Count`·`Sum` 등 `django.db.models` 식의 문자열 인자(`Extract`·`Trunc` 등은 첫 인자만, `Value`·`RawSQL`·`OuterRef`·`Subquery`는 제외), `When(then=)`·`Case(default=)`·`Window(order_by=, partition_by=)` | 필드 경로 |
| `annotate`·`alias`·`values` 키워드 이름 | 뒤따르는 조회식의 주석 이름(사실 없음) |
| 인스턴스 관계 매니저(`article.tags`, `article.comment_set`), 정방향 관계(`comment.article`) | 대상(과 M2M 중간) 테이블과 외래 키 컬럼 사실 |
| `raw(sql)`, `RawSQL(sql)`, `extra(tables=[...])` | SQL 텍스트, 테이블 관계 사실. `extra`의 `where`·`select` 조각은 읽지 않고 `skipped-sql-fragments:` |

풀지 못한 매니저 수신자(`model.objects`, django를 import한 모듈만)·조회식·`**` 인자는 dynamic 사실이다.

## SQLAlchemy·Flask-SQLAlchemy

### 이름 규칙

| 규칙 | 확인한 소스 |
|---|---|
| Declarative 기반: `DeclarativeBase`·`DeclarativeBaseNoMeta` 하위 클래스, `declarative_base()`, `registry().generate_base()`, Flask-SQLAlchemy `db.Model` | SQLAlchemy `orm/decl_api.py`, Flask-SQLAlchemy `extension.py` `_make_declarative_base` |
| 테이블 이름: 자기 `__table__`(Core 테이블), 자기 `__tablename__`, 매핑되지 않은 믹스인·추상(`__abstract__`) 클래스의 `__tablename__`. 매핑된 부모의 이름은 물려받지 않고, 이름이 없으면 단일 테이블 상속으로 부모 테이블을 쓴다. `@declared_attr` 이름은 dynamic | SQLAlchemy 2.0.54 `orm/decl_base.py` `_ClassScanMapperConfig._scan_attributes`·`_setup_table` |
| 컬럼: `Column`·`mapped_column`의 첫 위치 문자열 또는 `name=`, 없으면 속성 이름. 값 없는 `x: Mapped[T]`도 컬럼(T가 매핑 클래스·컬렉션이면 관계). 믹스인 컬럼은 매핑 클래스마다 복사, 단일 테이블 상속 자식 컬럼은 부모 테이블, 조인 상속 부모 컬럼은 부모 테이블에 남는다 | `orm/decl_base.py`, `orm/_orm_constructors.py` `mapped_column` |
| 스키마: `__table_args__`(사전 또는 끝이 사전인 튜플)의 `schema`, 없으면 기반 `MetaData(schema=)`. Core `Table`은 `schema=`, 없으면 MetaData 스키마(Flask-SQLAlchemy `db.Table`은 `SQLAlchemy(metadata=)`) | `sql/schema.py` `Table`·`MetaData`(스크래치에서 실측) |
| Flask-SQLAlchemy 자동 이름: `camel_to_snake_case(클래스 이름)` = `re.sub(r"((?<=[a-z0-9])[A-Z]\|(?!^)[A-Z](?=[a-z]))", r"_\1", name).lower().lstrip("_")`. 이름이 어디에도 없을 때, 또는 매핑된 부모에만 있을 때 붙인다. 매핑된 부모가 있으면 자기 기본 키 컬럼이 있을 때만 조인 테이블이고 없으면 단일 테이블 상속(부모 테이블) | Flask-SQLAlchemy 3.1.1 `model.py` `camel_to_snake_case`·`should_set_tablename`·`NameMixin.__table_cls__`(3.0.5 동일) |
| `disable_autonaming=True`거나 `model_class`가 이미 만든 declarative 클래스면 자동 이름이 없다 | `extension.py` `_make_declarative_base` |
| Flask-SQLAlchemy 2.x는 `([A-Z]+)(?=[a-z0-9])` 규칙이다(`User2FA` → 3.x `user2_fa`, 2.x `user2FA`). 버전을 한 메이저로 선언하지 않으면 두 규칙이 같은 이름만 정적이고 나머지는 dynamic과 `flask-sqlalchemy-naming-unverified:` | Flask-SQLAlchemy 2.5.1 `model.py` `camelcase_re`·`camel_to_snake_case` |

### 사용 규칙

| 코드 | 사실 |
|---|---|
| `select`·`insert`·`update`·`delete`·`aliased`·`exists` 인자, `.query`·`.join`·`.outerjoin`·`.select_from`·`.with_entities`·`.add_columns` 인자, `.get(M, …)`·`db.get_or_404(M, …)` 첫 인자가 매핑 클래스·그 컬럼 속성·Core 테이블 | 관계 사실(조인 상속이면 조상 테이블도) |
| `Model.column` | 컬럼 사실 |
| `Model.relationship`(조인·로더 옵션 등) | 대상과 `secondary` 테이블 관계 사실 |
| Flask-SQLAlchemy `Model.query`, `Model.__table__`, Core `table.select()` 등 | 관계 사실 |
| `table.c.name` | 컬럼 사실 |
| `filter_by(**kw)` | 마지막 조인 대상(없으면 첫 엔터티)의 컬럼 사실 |
| 생성자 `Model(key=...)` | 관계와 컬럼(관계 키는 대상 테이블) 사실 |
| `ForeignKey("schema.table.column")`, `ForeignKeyConstraint(…, ["t.c"])`, `relationship(secondary="t")` | 선언이 가리키는 관계·컬럼 사실 |
| `text(sql)`, `exec_driver_sql(sql)` | SQL 텍스트 |

`.query(...)`·`.join(...)` 같은 메서드는 수신자를 증명하지 않는다. 인자가 매핑 클래스·테이블일 때만 사실이 된다.
SQLAlchemy 함수(`select` 등) 인자의 풀지 못한 이름(`select(model)`)은 dynamic 사실이다. SQLModel·Peewee 등
해석하지 않는 ORM은 `unsupported-db-packages:`, 몽고·레디스 등은 `non-relational-stores:`로 센다.

## SQL 텍스트

관계 추출기는 가족 공유 알고리즘(tsograph `sql-relations.ts` ← dartograph·kartograph·cartograph)을 한 규칙씩 옮겼고
공유 벡터를 그대로 통과한다(`tests/test_sql_relations.py`).

- 명시 sink(`raw()`, `RawSQL`, `text()`, `exec_driver_sql`, DB 패키지 — `sqlite3`·`psycopg`·`psycopg2`·`pymysql`·
  `MySQLdb`·`mysql`·`cx_Oracle`·`oracledb`·`asyncpg`·`aiosqlite`·`aiomysql`·`pyodbc`·`django`·`sqlalchemy`·
  `flask_sqlalchemy` — 를 import한 모듈의 `execute`·`executemany`·`executescript`)는 소문자 SQL도 읽는다.
- 인자는 리터럴, 모듈 상수, 같은 함수의 유일한 대입, f-string, `+`, `%`, `.format()`으로 만든 문자열까지 읽고, 알
  수 없는 조각은 `{}` 플레이스홀더다. 관계 자리의 플레이스홀더는 dynamic 사실 하나다.
- DB-API 파라미터 표기(`%s`, `%(name)s`)는 값 자리라 `?`로 바꾼다(그대로 두면 `FROM %s`의 `s`가 관계로 읽힌다).
  문자열 보간 `"… %s" % x`의 `%s`는 SQL 조각이라 `{}`로 바꾼다. 파이썬 전용 전처리다.
- 명시 ORM sink의 읽지 못한 인자는 dynamic 사실, 일반 `execute`의 읽지 못한 이름 인자는
  `unresolved-sql-arguments:`로 센다(`select(...)` 같은 호출·그 대입은 SQL 텍스트가 아니라 세지 않는다).
- 그 밖의 문자열 리터럴은 SQL 동사와 관계 키워드가 대문자일 때만 읽는다(가족 strict 규칙). docstring은 읽지 않는다.
  소문자라 읽지 않은 SQL 모양 리터럴은 `skipped-sql-literals:`로 센다.

## 한계 접두사

모두 호출 측 한계다. isthmus는 이 문구로 심각도를 바꾸지 않고, dynamic 사실은 직접 센다.

| 접두사 | 뜻 |
|---|---|
| `dynamic-relation-names:` | 이유별 dynamic 사실 수(매니저 수신자, 조회식, 백엔드·라벨에 따라 다른 이름, SQL 플레이스홀더 등) |
| `django-app-label-unresolved:` | 앱 라벨을 확정하지 못한 모델 수 |
| `django-database-backend-unknown:` | `DATABASES` ENGINE을 읽지 못해 모든 내장 백엔드가 같은 이름만 정적 |
| `flask-sqlalchemy-naming-unverified:` | Flask-SQLAlchemy 2·3 규칙이 다른 자동 이름 수 |
| `persistence-framework-version-unknown:` | Django 5·SQLAlchemy 2로 선언하지 않음(확인한 버전 밖) |
| `unmodeled-orm-mappings:` | 모르는 외부 기반 클래스를 상속해 필드 목록이 불완전한 Django 모델 |
| `unresolved-django-lookups:` | 모델에 귀속하지 못한 `OuterRef` |
| `skipped-sql-fragments:` | 읽지 않은 `extra()` SQL 조각 |
| `unresolved-sql-arguments:` | 읽지 못한 일반 `execute` 인자 |
| `skipped-sql-literals:` | 읽지 않은 소문자 SQL 모양 리터럴 |
| `invalid-relation-names:` | 교환 형식이 싣지 못해 dynamic으로 내린 이름 |
| `missing-relation-usrs:` | symbol이 없는(모듈 수준) 사실 수 |
| `unsupported-db-packages:`, `non-relational-stores:` | 해석하지 않는 ORM·저장소를 import한 파일 수 |
| `skipped-migration-sources:`, `test-sources-excluded:` | 읽지 않은 마이그레이션·테스트 파일 수 |
| `unreadable-sources:`, `skipped-symlinks:`, `scan-truncated:` | 파싱 실패·심볼릭 링크·순회 상한 |

## 명명 벡터

`fixtures/persistence-naming/`의 합성 모델(Django 앱 네 개와 django.contrib, SQLAlchemy 2.x, Flask-SQLAlchemy 3)을
`experiments/persistence/run_naming.py`가 스크래치 가상 환경(Django 5.2.17·SQLAlchemy 2.0.54·Flask-SQLAlchemy
3.1.1과 psycopg·oracledb·pymysql)에서 실제로 import해 이름을 기록한다(`vectors.json`). Django는 설정의 `ENGINE`만
바꿔 백엔드 네 개(sqlite3·postgresql·mysql·oracle)마다 `_meta.db_table`·`field.column`·자동 중간 모델을
`connection.ops.quote_name()`으로 인용한 실제 이름을, SQLAlchemy는 매퍼의 `local_table`과 컬럼 속성의
(테이블, 컬럼)을 덤프한다. 데이터베이스에는 연결하지 않는다. `tests/test_persistence_naming.py`가 오프라인으로
비교한다.

| 벡터 | 일치 |
|---|---|
| Django 34개 모델(자동 중간 모델·contrib 포함) × 4 백엔드: 테이블·컬럼 | 136/136 = 100% |
| SQLAlchemy 9개 매핑 클래스·9개 테이블 | 100% |
| Flask-SQLAlchemy 10개 매핑 클래스·10개 테이블 | 100% |

벡터가 잡아낸 규칙: MySQL은 `'"archive"."documents"'`를 따옴표를 담은 한 식별자로 인용한다(다른 백엔드는
`archive.documents`). 기록하려면 `python experiments/persistence/run_naming.py`를 스크래치 가상 환경의 파이썬으로
실행한다.

## isthmus·schemagraph 종단 검증

`experiments/persistence/build_schema.py`가 두 사용 fixture(`fixtures/persistence/django-shop`,
`fixtures/persistence/flask-blog`)의 모델로 실제 ORM(Django `schema_editor().create_model()`, Flask-SQLAlchemy
`db.create_all()`)이 만든 SQLite DDL을 `experiments/persistence/e2e/*.sql`로 기록한다. `run_e2e.py`는 그 DDL을 임시
DB에 적용하고 schemagraph(`scan --emit-document`·`facts`)와 `pythograph schema`, `isthmus check --pairs`를 잇는다.
`tests/test_persistence_fixtures.py`는 정적 사용이 모두 그 DDL에 있는지 오프라인으로 확인한다.

| fixture | 결과(isthmus `578e852`의 스크래치 사본 + `python` 플랫폼, schemagraph `703a21f`) |
|---|---|
| `django-shop` | error 0, 매치 41(관계 9·컬럼 32), 경고 6 — 코드가 쓰지 않는 django.contrib 테이블의 `relation-decl-without-use-unverified`(dynamic 사용 2건으로 강등) |
| `flask-blog` | error 0, 경고 0, 매치 20(관계 5·컬럼 15) |

isthmus `main`은 `platform: "python"`을 `Unsupported bridge platform.`(종료 코드 2)으로 거부한다.
`src/exchange/parse.ts`의 플랫폼 유니온 타입과 `bridgePlatforms`에 `python`을 더하면 persistence 문서가 그대로
조인된다(persistence 도메인의 kind·플랫폼 규칙은 비sql 플랫폼을 모두 호출 측으로 본다).

## 결정 사항

- **모델 선언도 사용이다.** tsograph(Prisma 모델)·kartograph(JPA 엔터티)와 같이 모델 클래스와 필드 선언이
  테이블·컬럼 사실이다. 스키마 쪽 드리프트(모델은 있는데 테이블이 없음)가 `relation-use-without-decl`로 보인다.
- **인스턴스 속성 읽기는 사실이 아니다.** `book.title`은 이미 읽은 행의 값이다. 관계 접근(`book.author`,
  `book.tags`)만 질의가 되므로 사실이다.
- **외래 키 선언은 대상 테이블 사용이다.** 제약이 대상 테이블을 이름으로 가리킨다.
- **이름을 모르면 추측하지 않는다.** 백엔드·앱 라벨·Flask-SQLAlchemy 버전에 따라 이름이 갈리면 dynamic이다.
  dynamic은 isthmus에서 `relation-decl-without-use`를 판정 불가로 내리는 호출 측 공백이다.
- **`.query()`·`.join()` 수신자는 증명하지 않는다.** 인자가 매핑 클래스일 때만 사실이라 거짓 사실이 없고, 수신자
  증명이 필요한 Django 매니저·관계 매니저와 다르다.

## 다음 단계

- `pythograph graph`·`reach`·`impact`: 같은 심볼 id로 호출 그래프를 만들어 isthmus `trace`가 route → 핸들러 →
  `relation-use` → schemagraph 순으로 잇게 한다.
- 인스턴스 출처 확장(클래스 속성 `self.model`, 반환 형 주석), SQLAlchemy 인스턴스 관계 로딩, Alembic 모델 비교는
  아직 없다.
