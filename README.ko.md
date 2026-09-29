# pythograph

[English](README.md)

Python 서비스(Django, Django REST framework, Flask, SQLAlchemy)의 정적 사실을
[isthmus](https://github.com/ictechgy/isthmus) bridge-facts 교환 형식으로 낸다.

pythograph는 정적 분석 CLI 가족(TypeScript/JavaScript의 tsograph, Swift의 cartograph, Kotlin의 kartograph,
Dart의 dartograph, Go의 gartograph, Rust의 rustograph, SQL의 schemagraph)의 Python 생산자다. 각 도구는 자기
언어에서 본 것만 보고하고, 조인은 isthmus가 한다.

분석 대상 프로젝트는 표준 라이브러리 `ast`로만 파싱한다. import하거나 실행하지 않으며, pythograph는 런타임
의존성이 없고 네트워크를 쓰지 않는다(`graph`·`reach`·`impact`는 `revision`을 읽으려고 프로젝트 루트의 git만 실행한다).

## 상태

| 영역 | 상태 |
|---|---|
| `pythograph routes --role server`: Django URLconf, Django REST framework 라우터·뷰, Flask/Werkzeug 규칙 → `route-decl` 사실 | 구현됨 |
| `pythograph schema`: Django 모델·QuerySet, SQLAlchemy 2.x·Flask-SQLAlchemy 3 매핑·질의, SQL 텍스트 → persistence `relation-use` 사실 | 구현됨 |
| `pythograph graph`·`reach`·`impact`: Python 호출 그래프 → isthmus `language-traversal` v1(근거 등급 `direct`·`candidate`, `unresolvedCalls`, Django·DRF·Flask 디스패치) | 구현됨 |
| 클라이언트 route-call(requests, httpx) | 계획 |

isthmus `main`(`f9dcd1d`)은 `platform: "python"`의 http(`registration-order` 포함)·persistence 문서와 python
`language-traversal` 분석을 받는다([isthmus 호환](#isthmus-호환) 참고).

## 요구 사항과 설치

- Python 3.10 이상(Django 5.x가 3.10 이상을 요구하고, pythograph는 실행 중인 인터프리터로 분석 대상을
  파싱하므로 프로젝트와 같거나 새 파이썬으로 실행한다).

```sh
uv tool install git+https://github.com/ictechgy/pythograph
# 또는
pipx install git+https://github.com/ictechgy/pythograph
```

## `pythograph routes --role server`

```sh
pythograph routes --role server --project <root> [--service <name>] [--include-tests]
                  [--framework auto|django|flask] [--settings <module>]
                  [--dispatch specificity] [--generated-at <timestamp>] [--format json]
```

bridge-facts v1 문서를 표준 출력에 쓴다: `platform: "python"`, `target: "http"`, `roles: ["server"]`,
`dispatch`, `sourceSets`, (라우트, HTTP method)마다 `route-decl` 사실 하나.

- `--project`(필수): 프로젝트 루트. `project`는 그 POSIX realpath이고 모든 `location.path`는 그 기준 상대 경로다.
- `--framework`: 기본 `auto`는 Django(`manage.py`·`wsgi.py`·`asgi.py`의 `DJANGO_SETTINGS_MODULE` 기본값 또는
  `--settings`)와 Flask(`Flask(...)`·`Blueprint(...)` 객체)를 찾는다. 둘 다 있으면 하나를 고를 때까지 사용법 오류다.
- `--settings`: 진입 파일이 설정 모듈을 알려 주지 않을 때의 Django 설정 모듈.
- `--include-tests`: 테스트 소스(`test_*.py`, `*_test.py`, `tests.py`, `conftest.py`, `tests/`·`test/` 아래)의
  라우트도 `testSource: true`와 `sourceSets.tests: "included"`로 낸다.
- `--dispatch specificity`: Django 문서를 `specificity`로 선언하고 `order`를 싣지 않는다([결정 사항](#결정-사항)).
- `--generated-at`: 바이트 단위로 같은 출력을 위한 고정 `generatedAt`.
- 종료 코드: `0` 성공(사실 0건도 성공이며 완전성의 증거가 아니다), `2` 읽을 수 없는 프로젝트·사실 100,000건
  초과·출력 16 Mi 문자 초과·내부 오류(문구에 원인과 해결 방향을 담고 소스 원문·절대 경로는 싣지 않는다),
  `64` 사용법 오류. `1`은 예약이다.

### 모델링하는 것

모든 규칙은 설치한 패키지 소스(Django 5.2.17, djangorestframework 3.18.1, Flask 3.1.3, Werkzeug 3.1.9)로
확인했다. 소스 파일까지 적은 전체 표는 [docs/HTTP-ROUTES.md](docs/HTTP-ROUTES.md)에 있다.

- **Django**: 설정 모듈의 `ROOT_URLCONF`(프로젝트 설정 모듈의 star import 포함), 목록·`+`·`+=`·`.append`·
  `.extend`·`.insert`로 만든 `urlpatterns`, `path()`·`re_path()`·`include()`(모듈 문자열·모듈 객체·목록·
  `(patterns, app_name)` 튜플·중첩), 기본·등록 경로 변환기, 구문 트리로 바꿀 수 있는 `re_path` 정규식, 함수 뷰
  (`ANY`, `require_http_methods`·`require_GET`·`require_POST`·`require_safe`·`api_view`로 좁힘), 클래스 뷰
  (`as_view()`, 클래스 사슬의 핸들러, `http_method_names`), 등록 순서(처음 맞는 패턴이 이긴다).
- **Django REST framework**: `SimpleRouter`·`DefaultRouter`(`trailing_slash`, `use_regex_path`, lookup 설정,
  `detail`·`methods`·`url_path`·`.mapping`이 있는 `@action`), `DefaultRouter`의 API 루트와 형식 접미사 변형,
  `format_suffix_patterns`, `APIView`, generic view, viewset.
- **Flask**: 모듈 수준·앱 팩토리의 `Flask(...)`·`Blueprint(...)`, `@route`, `@get`·`@post`·`@put`·`@delete`·
  `@patch`, `add_url_rule`, `register_blueprint`(중첩, `url_prefix`), `MethodView`·`View`, Werkzeug 변환기(`string`·
  `int`·`float`·`uuid`·`path`·`any`·사용자), `strict_slashes`, `merge_slashes`.

### 사실을 만드는 방법

- **channel**: 정규 경로 템플릿. 세그먼트 전체 파라미터는 `{}`, 일부면 리터럴 골격(`/files/{}.json`), `/`와 맞을
  수 있는 끝 파라미터는 `{**}`이고, 증명할 수 없는 경로(한 세그먼트 두 파라미터, 중간 catch-all, 전후방 탐색,
  고정되지 않은 정규식)는 `dynamic`과 `route-coverage:`다. 리터럴은 디코드된 경로 공간이라 pchar가 아닌 문자는
  UTF-8로 퍼센트 인코딩하고 `%`는 `%25`가 된다.
- **method**: 대문자 동사 또는 `ANY`. GET 옆의 HEAD와 자동 OPTIONS는 내지 않는다(isthmus `head-as-get`·
  `options-any`가 맞춘다). 명시한 OPTIONS는 낸다.
- **paramConstraints**: `int`·`slug`·`uuid`·`path`(`{**}`)·`regex`(원문 포함).
- **trailingSlash**: Django와 strict Flask 규칙은 `strict`, 정규식의 `/?`와 Flask `strict_slashes=False`는
  `optional`, `{**}` 뒤는 생략.
- **order**(Django): `{group: "django:<ROOT_URLCONF>", index: <깊이 우선 순번>}`.
- **location**: 경로 문자열 인자(Django `path()`·`re_path()`, Flask 장식자·`add_url_rule`), DRF 경로는
  `router.register()`의 prefix, extra action은 `@action` 장식자. 1부터 시작하는 줄과 UTF-8 바이트 열.

### 심볼 id

`symbol.usr`는 `<프로젝트 상대 POSIX 경로>#<어휘적 점 경로>`다. 바깥 선언부터 잇고 `<locals>`는 넣지 않는다:
`catalog/views.py#item_list`, `blog/__init__.py#create_app.index`, `catalog/views.py#ItemEditView.get`,
`orders/views.py#OrderViewSet.list`. 클래스 핸들러는 메서드를 상속했어도 URL에 등록한 클래스 이름을 쓰고, 호출
그래프(`pythograph graph`)가 같은 id의 상속 멤버 정점을 만든다. 프로젝트 밖에서 정의한 뷰는 usr가 없고 `missing-route-usrs:`로
센다.

### 한계

값을 증명하지 못하면 추측하지 않고 `dynamic`, `pathAnchor: "base"`, 계약 접두사의 limitation으로 내며, 상한을
증명할 때만 `limitationScopes`를 싣는다. 예: 조건부 등록(`if settings.DEBUG:`)은 그 템플릿 스코프의
`route-coverage:`, 풀지 못한 include와 제3자 URL 모듈은 include 접두사 스코프, Django admin·`static()`·
`django.contrib.staticfiles`·Flask 정적 파일은 접두사 스코프의 `framework-provided-routes:`(Flask 정적 파일은
`GET`·`HEAD`도), `FORCE_SCRIPT_NAME`·`i18n_patterns`·등록이 보이지 않는 블루프린트는 `base` 앵커와
`unresolved-route-prefix:`, Django 5·DRF 3·Flask 3으로 버전을 제한하지 않은 프로젝트는
`route-framework-version-unknown:`이다.

### 결정 사항

- **Django는 `registration-order`, Flask는 `specificity`**다(소스로 확인). isthmus `f9dcd1d` 이전 판은 `registration-order`를
  거부하므로 `--dispatch specificity`로 Django를 specificity로 선언하고 `order`를 뺄 수 있다. 이 근사는 가려진
  패턴이 match되는 거짓 match만 만들 수 있고 거짓 error는 만들지 않는다. isthmus는 method를 먼저 거르고 method
  불일치를 어떤 후보도 그 method를 받지 않을 때만 내는데, 그때는 Django도 405를 낸다.
- **가려진 패턴도 선언이다.** 가림 판정은 소비자가 `order`로 한다.
- **조건부 등록은 선언이 아니라 스코프 있는 한계다.**

## `pythograph schema`

```sh
pythograph schema --project <root> [--include-tests] [--settings <module>] [--generated-at <timestamp>] [--format json]
```

bridge-facts v1 문서(`platform: "python"`, `target: "persistence"`, 사실이 없으면 `null`)와 관찰한 관계·컬럼 참조마다
`relation-use` 사실 하나를 표준 출력에 쓴다. isthmus가 `docs/GRAPH-EXCHANGE.md`의 persistence 규칙으로
`platform: "sql"` 문서(schemagraph `facts --document <catalog>`)와 조인한다. 출처를 붙인 전체 규칙은
[docs/PERSISTENCE.md](docs/PERSISTENCE.md)에 있다.

- **Django**: 모델 클래스(추상·프록시·다중 테이블 상속·`Meta` 상속) → 테이블(`<app_label>_<모델>`을 백엔드
  `max_name_length`로 `truncate_name`한 것, 또는 `Meta.db_table`), 필드 → 컬럼(`db_column`, 외래 키 `<이름>_id`,
  M2M 중간 테이블과 컬럼), `INSTALLED_APPS`·`AppConfig`의 앱 라벨, `DATABASES` 백엔드, django.contrib 모델. 사용:
  매니저·QuerySet 사슬, 조회식(`author__profile__city`, 역관계, `attname`, `pk`), `values`·`order_by`·`F`·`Q`·집계,
  `create`·`update` 키워드, 증명한 인스턴스의 관계 매니저·정방향 관계, `raw()`, `RawSQL`, `extra(tables=)`, 커서 SQL.
- **SQLAlchemy 2.x·Flask-SQLAlchemy 3**: Declarative 클래스(`DeclarativeBase`, `declarative_base()`, snake_case 자동
  이름의 `db.Model`), 믹스인, 단일·조인 테이블 상속, `__table_args__`·`MetaData` 스키마, Core `Table`,
  `ForeignKey("t.c")`, `relationship(secondary=)`. 사용: 문장 엔터티(`select`·`insert`·`update`·`delete`·
  `session.query`·`session.get`·`join`), `Model.column`, `Model.relationship`, `Model.query`, `filter_by`, 생성자,
  `table.c.name`, `text()`.
- **SQL 텍스트**: 가족 공유 어휘 추출기(tsograph·dartograph·cartograph·kartograph와 같은 벡터)로 명시 SQL 인자를,
  그 밖에는 대문자 SQL 리터럴을 읽는다(docstring 제외).
- **channel**은 코드·매핑이 쓴 관계 이름(한정했을 때만 `schema.table`, 기본 스키마는 추측하지 않음), **method**는
  컬럼, **symbol.usr**는 감싸는 함수·메서드·모델 클래스이며 `routes`와 같은 id다. 백엔드·앱 라벨·Flask-SQLAlchemy
  버전에 따라 갈리는 이름, 풀지 못한 모델·조회식, 실행 중에 만든 SQL은 추측하지 않고 `dynamic` 사실과
  `dynamic-relation-names:` 한계로 낸다.
- 테스트 소스(`--include-tests`가 없을 때)와 마이그레이션(Django `migrations/`, Alembic `versions/`)은 읽지 않는다.
  마이그레이션은 과거 스키마를 기술한다.

## `pythograph graph`·`reach`·`impact`

```sh
pythograph graph  --project <root> [--include-tests] [--revision <id>] [--generated-at <timestamp>]
pythograph reach  --project <root> [--dispatch direct|bound|candidates] [--max-depth <n>] [--max-reached <n>]
                  [--roots-from <file|->] [--include-tests] [--revision <id>] [--generated-at <timestamp>] [--] <id>...
pythograph impact (reach와 같은 옵션)
```

프로젝트의 파이썬 호출 그래프를 표준 라이브러리 `ast`로 만든다. `graph`는 pythograph 자체 스냅샷(`pythograph-graph` v1),
`reach`는 root가 기대는 심볼(`dependencies`), `impact`는 root에 기대는 심볼(`dependents`)을 isthmus
[`language-traversal` v1](https://github.com/ictechgy/isthmus/blob/main/docs/LANGUAGE-TRAVERSAL.md)로 낸다. id는 `routes`·
`schema`의 `symbol.usr`와 같은 문자열이다. 전체 규칙은 [docs/GRAPH.md](docs/GRAPH.md)에 있다.

- **정점**: 모듈(`<경로>#<module>`), 함수·메서드·클래스·중첩 정의, 상속 멤버(`<등록 클래스>.<멤버>`, 뷰 핸들러와 정확한
  수신자의 물려받은 멤버).
- **간선**: `call`, `new`(프로젝트 `__init__` 또는 클래스), `callback`, `reference`, `decorator`, `attribute`(클래스 본문
  속성), `inherit`, `dispatch`·`framework`(프레임워크 디스패치). import(절대·상대·별칭·`__init__` 재수출·`*`), 모듈 속성, 생성자,
  프로젝트 클래스의 C3 MRO로 푼 `self`·`super()`, 주석·반환 주석 수신자, 모듈 수준 인스턴스, property를 따라간다.
- **근거 등급**: 정적으로 푼 간선은 `direct`, `self`·`cls`·주석 수신자의 프로젝트 하위 클래스 재정의는 `candidate`다.
  `bound`는 아직 만들지 않는다 — `--dispatch bound`는 `direct` 그래프와 같다.
- **추측 없음**: 대상을 모르는 호출은 이유별(`parameter`, `untyped-receiver`, `dynamic-attribute`, `getattr`,
  `dynamic-callee`, `unresolved-import`, `framework-callback` 등)로 세어 정점마다 `unresolvedCalls`로 싣는다. 타입 모르는
  수신자의 호출은 프로젝트가 그 이름을 정의·대입하지 않을 때만 외부로 확정한다.
- **프레임워크 디스패치**: Django 5.2.17·DRF 3.18.1·Flask 3.1.3 설치본 소스를 ast로 읽어 만든 표로 `as_view()` 디스패치 경로
  (`dispatch`·`initial`·권한·`__init__`)와 프레임워크 구현(`ModelViewSet.retrieve` → `get_object` → `get_queryset`,
  `ModelSerializer.save` → `create`)이 부르는 프로젝트 훅을 잇는다. 프레임워크가 클래스 속성으로 만드는 객체
  (`serializer_class`·`permission_classes`)는 `framework-callback` 미해석으로 센다.
- **순회 문서**: `dispatch` 선언, root별 하한 `evidence`, `unresolvedCalls`, 다중 root 단일 패스(root별 오라클과 무작위 비교),
  `--max-depth`·`--max-reached` 잘림, `rootsTruncated`. 정점이 아닌 root는 `symbol` 없이 싣고 문서를 쓴 뒤 64로 끝난다.
  `revision`은 `--revision` 또는 작업 트리가 깨끗할 때의 git HEAD, `graphRevision`은 그래프 내용 SHA-256이다.

## 검증

`experiments/oracle/`의 하네스가 스크래치 가상 환경에서 합성 fixture를 import해 pythograph 사실을 Django resolver
순회·DRF 라우터·Flask `url_map`과 비교한다(`tests/test_fixtures.py`가 기록을 오프라인으로 다시 확인한다):

| 대상 | 정밀도 | 재현율 |
|---|---|---|
| `fixtures/django/drf-shop` | 69/69 | 58/59(의도한 dynamic 전방 탐색 패턴 1건) |
| `fixtures/flask/blog-app` | 28/28 | 27/27 |
| HackSoftware/Django-Styleguide-Example `a70ef43`(MIT, 스크래치 복제) | 21/21 | 21/22(DEBUG 전용 `static()` 경로) |

isthmus 공유 적합성 벡터(`conformance/`, isthmus `f9dcd1d`에서 벤더링해 `conformance.lock`으로 고정)의 해당 생산자
사례 78건(`template.grammar`·`template.normalize`·`scope.validate`·`scope.applies`·`dispatch.validate`)을 100% 통과하고,
`dispatch.validate` 검증기는 routes 출력 golden에도 적용한다.

**Phase 6 종료 조건(Django 백엔드 × iOS/Android 체인).** `experiments/e2e/`가 합성 Django+DRF 서버(`fixtures/e2e/shop-api`),
Django DDL의 schemagraph 카탈로그, 합성 iOS(cartograph)·Android(kartograph) 클라이언트의 route-call·역방향 순회를 isthmus
`trace`(workspace)로 잇고, 세 질문 — (a) API → DB 테이블 + DB 의존자, (b) API → 클라이언트 호출부 → 영향 심볼, (c) 테이블 → API →
클라이언트 — 의 기대 경로가 일치한다. 기록한 입력·출력을 `tests/test_e2e_trace.py`가 오프라인으로 다시 확인한다(표는
[docs/GRAPH.md](docs/GRAPH.md#phase-6-종료-조건-django-백엔드--iosandroid-체인)).

persistence 명명 벡터(`fixtures/persistence-naming/vectors.json`)는 스크래치 환경에서 합성 모델을 실제 ORM으로 import해
기록한다(`experiments/persistence/run_naming.py`): Django 5.2.17 `_meta` 이름을 백엔드별 `connection.ops`(sqlite3·
postgresql·mysql·oracle)로 인용한 것과 SQLAlchemy 2.0.54·Flask-SQLAlchemy 3.1.1 매퍼다. pythograph는 100% 일치한다
(Django 모델×백엔드 136/136, SQLAlchemy 9/9·Flask-SQLAlchemy 10/10 클래스, 모든 테이블·컬럼). 두 persistence
fixture의 `pythograph schema` 출력을 ORM이 만든 DDL의 schemagraph 카탈로그와 조인하면(`experiments/persistence/run_e2e.py`)
isthmus error가 없다(매치 41·20).

## isthmus 호환

isthmus `main`(`f9dcd1d`, #128)은 `platform: "python"`을 받는다: http `route-decl`(Django의 `registration-order`와 `order`,
가림 진단 포함), persistence `relation-use`, trace의 python `forward`·`reverse` 분석(`language-traversal` v1). 옛 isthmus용
`--dispatch specificity`는 그대로 남아 있다.

## 개발

```sh
uv sync
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run pytest --cov          # 라인·분기 커버리지 게이트 90%
uv run python scripts/verify_cli_contract.py
```

프레임워크 훅 표는 스크래치 가상 환경(Django 5.2.17·DRF 3.18.1·Flask 3.1.3 설치)의 site-packages로
`python experiments/graph/dump_framework_hooks.py --site-packages <경로>`가 다시 만든다(`--check`는 비교만).

## 라이선스

MIT
