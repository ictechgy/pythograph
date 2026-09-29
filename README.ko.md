# pythograph

[English](README.md)

Python 서비스(Django, Django REST framework, Flask)의 정적 사실을
[isthmus](https://github.com/ictechgy/isthmus) bridge-facts 교환 형식으로 낸다.

pythograph는 정적 분석 CLI 가족(TypeScript/JavaScript의 tsograph, Swift의 cartograph, Kotlin의 kartograph,
Dart의 dartograph, Go의 gartograph, Rust의 rustograph, SQL의 schemagraph)의 Python 생산자다. 각 도구는 자기
언어에서 본 것만 보고하고, 조인은 isthmus가 한다.

분석 대상 프로젝트는 표준 라이브러리 `ast`로만 파싱한다. import하거나 실행하지 않으며, pythograph는 런타임
의존성이 없고 네트워크를 쓰지 않는다.

## 상태

| 영역 | 상태 |
|---|---|
| `pythograph routes --role server`: Django URLconf, Django REST framework 라우터·뷰, Flask/Werkzeug 규칙 → `route-decl` 사실 | 구현됨 |
| persistence `relation-use`(Django 모델 `app_label`·`db_table`, SQLAlchemy, Flask-SQLAlchemy) | 계획 |
| `pythograph graph`·`reach`·`impact`: Python 호출 그래프 → isthmus `language-traversal` v1 | 계획 |
| 클라이언트 route-call(requests, httpx) | 계획 |

isthmus의 `http` target은 아직 isthmus `docs/GRAPH-EXCHANGE.md`의 초안이고, isthmus는 아직
`platform: "python"`을 받지 않는다([isthmus 호환](#isthmus-호환) 참고).

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
`orders/views.py#OrderViewSet.list`. 클래스 핸들러는 메서드를 상속했어도 URL에 등록한 클래스 이름을 쓰고, 계획한
호출 그래프가 같은 id의 상속 멤버 정점을 만든다. 프로젝트 밖에서 정의한 뷰는 usr가 없고 `missing-route-usrs:`로
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

- **Django는 `registration-order`, Flask는 `specificity`**다(소스로 확인). 현재 isthmus는 `registration-order`를
  거부하므로 `--dispatch specificity`로 Django를 specificity로 선언하고 `order`를 뺄 수 있다. 이 근사는 가려진
  패턴이 match되는 거짓 match만 만들 수 있고 거짓 error는 만들지 않는다. isthmus는 method를 먼저 거르고 method
  불일치를 어떤 후보도 그 method를 받지 않을 때만 내는데, 그때는 Django도 405를 낸다.
- **가려진 패턴도 선언이다.** 가림 판정은 소비자가 `order`로 한다.
- **조건부 등록은 선언이 아니라 스코프 있는 한계다.**

## 검증

`experiments/oracle/`의 하네스가 스크래치 가상 환경에서 합성 fixture를 import해 pythograph 사실을 Django resolver
순회·DRF 라우터·Flask `url_map`과 비교한다(`tests/test_fixtures.py`가 기록을 오프라인으로 다시 확인한다):

| 대상 | 정밀도 | 재현율 |
|---|---|---|
| `fixtures/django/drf-shop` | 69/69 | 58/59(의도한 dynamic 전방 탐색 패턴 1건) |
| `fixtures/flask/blog-app` | 28/28 | 27/27 |
| HackSoftware/Django-Styleguide-Example `a70ef43`(MIT, 스크래치 복제) | 21/21 | 21/22(DEBUG 전용 `static()` 경로) |

isthmus 공유 적합성 벡터(`conformance/`, `conformance.lock`으로 고정)의 해당 생산자 사례 60건
(`template.grammar`·`template.normalize`·`scope.validate`·`scope.applies`)을 100% 통과한다.

## isthmus 호환

isthmus `main`(`78d3dee`)은 `platform: "python"` 문서를 거부한다. `src/exchange/parse.ts`의 플랫폼 유니온 타입,
`bridgePlatforms`, `httpPlatforms`, `routeKindPlatforms`의 `route-decl` 항목에 `python`을 더하면 Flask 문서와
`--dispatch specificity`로 만든 Django 문서를 받는다. 기본 Django 문서는 isthmus가 `registration-order`도 구현해야
받는다.

## 개발

```sh
uv sync
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run pytest --cov          # 라인·분기 커버리지 게이트 90%
uv run python scripts/verify_cli_contract.py
```

## 라이선스

MIT
