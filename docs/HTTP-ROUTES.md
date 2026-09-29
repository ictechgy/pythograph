# HTTP 라우트 선언 규칙 (`pythograph routes --role server`)

pythograph가 Django(+Django REST framework)·Flask 프로젝트에서 isthmus http `route-decl` 사실을 만드는
규칙과, 각 규칙을 확인한 공식 소스를 적는다. 규칙은 추정하지 않고 아래 버전의 설치 패키지 소스를 직접 읽어
확인했다(PyPI에서 스크래치 가상 환경에 설치, 2026-09-29).

| 패키지 | 확인한 버전 | pythograph가 받는 선언 범위 |
|---|---|---|
| Django | 5.2.17 | 메이저 5 |
| djangorestframework | 3.18.1 | 메이저 3 |
| Flask | 3.1.3 | 메이저 3 |
| Werkzeug | 3.1.9 | Flask 3이 요구하는 3.x |

프로젝트의 잠금·요구 파일(`uv.lock`·`poetry.lock`·`pdm.lock`·`Pipfile.lock`·`requirements*.txt`·
`requirements/*.txt`·`pyproject.toml`·`setup.cfg`·`Pipfile`)이 해당 패키지를 위 메이저로 제한하지 않으면
`route-framework-version-unknown:` 한계를 낸다(다른 버전에서는 규칙이 다를 수 있다는 서버 측 공백 신고).

분석 대상 코드는 표준 라이브러리 `ast`로만 읽는다. import·실행·네트워크 접근을 하지 않는다. 정규식은 표준
라이브러리 정규식 파서(3.11+ `re._parser`, 3.10 `sre_parse`)의 구문 트리만 읽고 실행하지 않는다.

## 심볼 id

`symbol.usr`(와 `qualifiedName`)는 `<프로젝트 상대 POSIX 경로>#<어휘적 점 경로>`다. 점 경로는 바깥 선언부터
함수·클래스 이름을 점으로 잇고 `<locals>`는 넣지 않는다. tsograph(`src/lib/jobs.ts#listJobs`)와 같은 모양이다.

| 핸들러 | id 예시 |
|---|---|
| 모듈 수준 함수 뷰 | `catalog/views.py#item_list` |
| 앱 팩토리 안의 함수 | `blog/__init__.py#create_app.index` |
| Django·DRF 클래스 뷰의 method | `catalog/views.py#ItemEditView.get` |
| DRF viewset action | `orders/views.py#OrderViewSet.list`, `…#CartViewSet.set_price` |
| Flask `MethodView` | `blog/views.py#ItemAPI.post` |
| Flask `View`(`methods` 선언) | `blog/views.py#SettingsView.dispatch_request` |

클래스 핸들러 id는 **URL에 등록한 클래스** 기준이다. 메서드를 상속했어도(프로젝트 기반 클래스, DRF
`ModelViewSet.list` 같은 프레임워크 구현) `<등록 클래스 파일>#<등록 클래스>.<메서드>`다. 호출
그래프(`pythograph graph`, [GRAPH.md](GRAPH.md))는 같은 id의 "상속 멤버" 정점을 만들고 정의한 메서드(또는 프레임워크
구현이 부르는 프로젝트 훅)로 잇는다. tsograph가 재수출 이름을 `<파일>#<export 이름>`으로 두고 그래프에 alias 간선을 두는 것과
같은 방식이다. 프로젝트 밖 클래스를 직접 쓴 뷰(`TemplateView.as_view()`)와 API 루트는 usr가 없고
`missing-route-usrs:`(체인 전용)로 센다.

## Django

| 규칙 | 확인한 소스(Django 5.2.17) |
|---|---|
| 루트 resolver는 `URLResolver(RegexPattern(r"^/"), ROOT_URLCONF)`이고 패턴을 목록 순서대로 시도해 처음 맞는 것을 쓴다. include 안에서 맞는 것이 없으면(Resolver404) 다음 패턴으로 넘어간다 → 깊이 우선 순서가 등록 순서 | `django/urls/resolvers.py` `get_resolver`·`URLResolver.resolve` |
| `path()` 문자열은 `^` + 이스케이프한 리터럴 + `(?P<name>변환기 정규식)`이고 endpoint면 끝에 `\Z`를 붙인다. include 접두사는 끝을 열어 둔다 | `resolvers.py` `_route_to_regex`, `django/urls/conf.py` `_path` |
| 기본 변환기: `int` `[0-9]+`, `str` `[^/]+`(생략 시), `slug` `[-a-zA-Z0-9_]+`, `uuid` 소문자 8-4-4-4-12, `path` `.+` | `django/urls/converters.py` `DEFAULT_CONVERTERS` |
| `register_converter(Class, "name")`로 등록한 변환기는 클래스 `regex`를 쓴다. `to_python`의 `ValueError`는 "맞지 않음" | `converters.py` `register_converter`, `RoutePattern.match` |
| `<…>` 안 공백, 식별자가 아닌 이름, 모르는 변환기는 `ImproperlyConfigured` | `_route_to_regex` |
| `re_path()` endpoint는 정규식 텍스트가 `$`로 끝나면 `fullmatch`, 아니면 `search`. include는 항상 `search` | `resolvers.py` `RegexPattern.match` |
| 요청 경로는 퍼센트 디코드한 `path_info`에 맞춘다(라우트 문자열의 `%`는 글자 그대로) | `django/core/handlers/wsgi.py` `get_path_info` |
| `include(arg, namespace=None)`: 모듈 이름 문자열, 모듈 객체, 패턴 목록, `(목록 또는 모듈, app_name)` 튜플. `path(…, (목록, app_name, namespace))`도 include다 | `conf.py` `include`·`_path` |
| 함수 뷰는 모든 method를 받는다. `require_http_methods`·`require_GET`·`require_POST`·`require_safe`(GET·HEAD)가 좁힌다 | `django/views/decorators/http.py` |
| 클래스 뷰: `http_method_names`(기본 get·post·put·patch·delete·head·options·trace) 중 핸들러가 있는 method만 받고 나머지는 405. `get`이 있고 `head`가 없으면 `head = get`. `options`는 `View.options`가 항상 받는다 | `django/views/generic/base.py` `View.setup`·`dispatch`·`options` |
| generic view 핸들러: `TemplateView`·`ListView`·`DetailView`·날짜 뷰 get, `RedirectView` get·head·post·options·delete·put·patch, `FormView`·`CreateView`·`UpdateView` get·post·put, `DeleteView` get·post·delete. 기반 클래스 `BaseDetailView`·`BaseListView`·`Base…ArchiveView`·`BaseDateDetailView` get, `ProcessFormView`·`BaseFormView`·`BaseCreateView`·`BaseUpdateView` get·post·put, `BaseDeleteView` get·post·delete, `DeletionMixin` post·delete. 핸들러가 없는 믹스인(`SingleObjectTemplateResponseMixin`·`MultipleObjectTemplateResponseMixin`·날짜 믹스인)은 투명 | 설치 패키지에서 `hasattr`로 조사 |
| `django.contrib.auth.views`: `LoginView`·`PasswordChangeView`·`PasswordResetView`·`PasswordResetConfirmView` get·post·put, `PasswordChangeDoneView`·`PasswordResetDoneView`·`PasswordResetCompleteView` get, `LogoutView`는 `http_method_names = ["post", "options"]`라 post만(하위 클래스가 `http_method_names`를 다시 선언하면 그 값). `RedirectURLMixin`·`PasswordContextMixin`은 투명 | `django/contrib/auth/views.py`, 설치 패키지에서 `hasattr`로 조사 |
| `APPEND_SLASH`: 경로가 맞지 않고 끝에 `/`를 붙이면 맞을 때만 `CommonMiddleware`가 301로 넘긴다(DEBUG의 POST 등은 RuntimeError). 매칭 자체는 정확하다 | `django/middleware/common.py` `should_redirect_with_slash`·`process_response` |
| `static(prefix, view=serve)`는 `DEBUG`이고 prefix에 host가 없을 때만 `re_path(r"^<prefix>(?P<path>.*)$", serve)`를 더한다. `serve`는 method를 제한하지 않는다 | `django/conf/urls/static.py`, `django/views/static.py` |
| `django.contrib.staticfiles`는 `DEBUG`의 `runserver`에서 `STATIC_URL` 아래 파일을 제공한다 | `django/contrib/staticfiles/handlers.py` `StaticFilesHandlerMixin` |
| `i18n_patterns`는 언어 코드 접두사를 붙인다(설정·활성 언어에 따라 다름) | `resolvers.py` `LocalePrefixPattern` |
| include의 경로 문자열도 앞부분을 소비하는 패턴이라, 맨 앞 include 문자열이 설정값(`path(settings.BASE_PATH, include(...))`)이면 하위 패턴은 그 알 수 없는 접두사 뒤에 붙는다 | `conf.py` `_path`, `resolvers.py` `URLResolver.resolve` |

### 사실로 바꾸는 방법

- **channel**: 루트 `/` 뒤에 include 조각과 endpoint 조각을 순서대로 잇는다(Django가 조각마다 앞부분을 소비하는
  방식과 같다). `path()`는 Django와 같은 정규식으로 바꾼 뒤, `re_path()`는 그대로 정규식 구문 트리로 토큰열을
  만든다. 세그먼트 전체 파라미터는 `{}`, 일부면 리터럴 골격(`files/{}.json`), 한 세그먼트에 둘 이상이면 dynamic과
  `route-coverage:`다. 리터럴은 디코드된 공간의 문자라서 pchar가 아니면 UTF-8로 인코딩하고 `%`는 `%25`가 된다.
- **catch-all**: `/`와 맞을 수 있는 파라미터(`<path:x>`, `.+`)는 마지막 세그먼트 전체일 때만 `{**}`이고 그 밖은
  dynamic이다. Django `path` 변환기는 한 글자 이상이라 `/files`(접두사만)와 맞지 않으므로 0세그먼트 접두사 decl
  (`catchAllPrefix`)을 내지 않는다. `.*`처럼 빈 값을 받으면 빈 값 변형(`/files/`)을 더한다.
- **정규식**: `^`로 시작하지 않는 `search`, 끝이 열린 endpoint, 전후방 탐색, 역참조, `\b` 같은 앵커, 대소문자 무시
  플래그는 dynamic이다. 선택 리터럴(`x?`), 대안(`a|b`), 유한 문자 집합(`[12]`)은 템플릿 여러 개로 펼치고 16개를
  넘으면 dynamic과 `route-template-expansion-capped:`다. 끝 `/?`는 `trailingSlash: "optional"` 하나로 합친다.
- **paramConstraints**: `X+` 모양에서 X가 받는 ASCII 문자 집합이 숫자면 `int`, `[-A-Za-z0-9_]`면 `slug`,
  `/`를 뺀 전부면 제약 없음(싣지 않음), 알려진 UUID 정규식이면 `uuid`, `{**}`는 `path`(정보용), 그 밖은 `regex`와
  원문 `pattern`이다. ASCII 밖 문자는 요청에서 퍼센트 인코딩되어 소비자가 평가하지 않으므로 판별에서 뺀다.
- **trailingSlash**: 정적 템플릿은 `strict`(정확 매칭, `APPEND_SLASH`는 넘겨주기일 뿐이라 다른 형태는 핸들러에
  닿지 않는다), 끝 `/?`는 `optional`, `{**}`로 끝나면 생략(unknown)이다.
- **method**: 함수 뷰 `ANY` 또는 장식자가 좁힌 동사, 클래스 뷰는 받는 핸들러 동사다. HEAD는 GET이 있으면
  내지 않고(소비자 `head-as-get`), 클래스 뷰의 자동 OPTIONS도 내지 않는다(`options-any`). 명시한 OPTIONS는 낸다.
- **order**: `dispatch: "registration-order"` 문서의 사실마다 `{group: "django:<ROOT_URLCONF>", index: <깊이 우선
  순번>}`이다. 한 패턴에서 나온 사실(method·펼친 템플릿)은 같은 순번이다. 앞 패턴이 뒤 패턴을 가려도 둘 다
  선언으로 낸다(가림 판정은 소비자의 `route-decl-shadowed` 몫이다).
- **location**: endpoint `path()`·`re_path()`의 경로 문자열 인자(줄, UTF-8 바이트 열). DRF 라우터 경로는
  `router.register()`의 prefix 인자, extra action은 `@action` 장식자, API 루트는 라우터 생성 호출이다.

## Django REST framework

| 규칙 | 확인한 소스(DRF 3.18.1) |
|---|---|
| `SimpleRouter(trailing_slash=True, use_regex_path=True)`: 등록마다 목록(`get`→list, `post`→create), `detail=False` action, 상세(`get`→retrieve, `put`→update, `patch`→partial_update, `delete`→destroy), `detail=True` action 순서. viewset에 없는 action은 빼고 남는 동사가 없으면 경로를 만들지 않는다 | `rest_framework/routers.py` `SimpleRouter.routes`·`get_routes`·`get_method_map`·`get_urls` |
| URL 틀: 정규식 모드 `^{prefix}{trailing_slash}$`·`^{prefix}/{lookup}{trailing_slash}$`… prefix는 정규식 조각 그대로, prefix가 비면 앞 `/`를 뗀다. 경로 모드는 `^`·`$`를 뗀 `path()` 문법 | `SimpleRouter.__init__`·`get_urls` |
| lookup: 정규식 모드 `(?P<{lookup_url_kwarg or lookup_field}>{lookup_value_regex, 기본 [^/.]+})`, 경로 모드 `<{lookup_value_converter, 기본 str}:{kwarg}>` | `SimpleRouter.get_lookup_regex` |
| extra action은 `inspect.getmembers` 순서(이름순)이며 `methods` 기본 `["get"]`, `url_path` 기본 함수 이름, `@x.mapping.<method>`로 동사를 더한다 | `rest_framework/viewsets.py` `get_extra_actions`, `rest_framework/decorators.py` `action`·`MethodMapper` |
| viewset `as_view(actions)`는 `get`이 있으면 `head`를 같은 action으로 붙인다 | `viewsets.py` `ViewSetMixin.as_view` |
| `DefaultRouter`는 끝에 API 루트(`path("", APIRootView)`, GET)를 더하고, 모든 패턴 바로 뒤에 형식 접미사 변형을 더한다: 정규식 `\.(?P<format>[a-z0-9]+)/?$`(원본의 끝 `$`·`/`를 뗀 뒤), 경로 모드는 `drf_format_suffix` 변환기(`\.[a-z0-9]+/?`) | `routers.py` `DefaultRouter.get_urls`, `rest_framework/urlpatterns.py` `format_suffix_patterns`·`apply_suffix_patterns` |
| `@api_view(methods)`는 인자가 없으면 `["GET"]`이다 | `rest_framework/decorators.py` `api_view` |
| `APIView`는 `http_method_names` 중 핸들러가 있는 method를 받는다(`allowed_methods`). generic view 핸들러: `ListAPIView` get, `CreateAPIView` post, `RetrieveAPIView` get, `DestroyAPIView` delete, `UpdateAPIView` put·patch, 조합형은 합집합. mixin: `ListModelMixin` list 등 | `rest_framework/views.py`, `generics.py`, `mixins.py`, `viewsets.py`(설치 패키지 조사) |

DRF 사실은 Django 규칙 위에서 같은 방식으로 만든다. `router.urls`는 **접근 시점**의 등록만 담는다(DRF도
`include(router.urls)`를 평가할 때 목록을 만든다). 모르는 기반 클래스를 가진 viewset, 리터럴이 아닌
`trailing_slash`·lookup 속성, 사용자 라우터 하위 클래스는 등록 prefix 아래 `route-coverage:` 스코프로 남긴다.
DefaultRouter의 상세 경로 형식 접미사 변형(`/orders/{}.{}`)은 한 세그먼트에 파라미터가 둘이라 계약대로 dynamic이다.

## Flask(Werkzeug)

| 규칙 | 확인한 소스(Flask 3.1.3, Werkzeug 3.1.9) |
|---|---|
| `add_url_rule(rule, endpoint=None, view_func=None, provide_automatic_options=None, **options)`: `methods`가 없으면 `view_func.methods` 또는 `("GET",)`. `PROVIDE_AUTOMATIC_OPTIONS`면 OPTIONS를 더한다 | `flask/sansio/app.py` `App.add_url_rule` |
| `@route`는 `add_url_rule`을 부르고 `@get`·`@post`·`@put`·`@delete`·`@patch`(2.0+)는 `methods=[동사]`다 | `flask/sansio/scaffold.py` `route`·`_method_route` |
| Werkzeug `Rule`: `GET`이 있고 `HEAD`가 없으면 `HEAD`를 더한다 | `werkzeug/routing/rules.py` `Rule.__init__` |
| 블루프린트 규칙: `url_prefix.rstrip("/") + "/" + rule.lstrip("/")`, 규칙이 비면 접두사 그대로. 등록 옵션 `url_prefix`가 블루프린트 값보다 앞선다. 중첩은 부모 접두사와 같은 방식으로 잇는다 | `flask/sansio/blueprints.py` `BlueprintSetupState.__init__`·`add_url_rule`, `Blueprint.register` |
| `MethodView.methods`는 기반 클래스 `methods`와 정의한 핸들러(get·post·head·options·delete·put·trace·patch) 대문자의 합집합. `View`는 클래스 `methods` 선언, 없으면 None | `flask/views.py` `MethodView.__init_subclass__`, `View.as_view` |
| 규칙 문법 `<변환기(인자):이름>`, 인자는 불리언·숫자·따옴표 문자열 | `rules.py` `_part_re`·`parse_converter_args`·`_pythonize` |
| 기본 변환기: `default`·`string`(`[^/]{minlength,maxlength}`, 기본 minlength 1), `int`(`\d+`, signed면 `-?`), `float`(`\d+\.\d+`), `uuid`, `path`(`[^/].*?`, `/`를 넘음), `any(a, b)`, 사용자 변환기는 `url_map.converters` | `werkzeug/routing/converters.py` |
| `merge_slashes`(기본 True)면 규칙의 연속 `/`를 하나로 합친다 | `rules.py` `Rule.compile` |
| `strict_slashes`(기본 True): 끝 `/` 규칙은 `/` 없는 요청을 308로 넘기고, `/` 없는 규칙은 끝 `/` 요청과 맞지 않는다. False면 양쪽이 규칙에 닿는다 | `werkzeug/routing/matcher.py` `StateMachineMatcher.match` |
| 매칭: 세그먼트마다 정적 전이를 먼저, 동적 전이는 가중치 순으로 시도하고 되돌아간다. method가 맞지 않으면 다른 규칙을 계속 찾고 끝내 없으면 405 | `matcher.py` `StateMachineMatcher.update`·`match` |
| 앱 정적 파일: `static_folder`(기본 `"static"`)가 있으면 `static_url_path`(기본 `/` + 폴더 이름) + `/<path:filename>`을 GET으로 등록한다 | `flask/app.py` `Flask.__init__`, `flask/sansio/scaffold.py` `static_url_path` |

### 사실로 바꾸는 방법

- 문서는 `dispatch: "specificity"`다. isthmus 구체성(리터럴 > 부분 세그먼트 > 제약 있는 `{}` > `{}` > `{**}`)은
  Werkzeug 가중치와 완전히 같지는 않지만, isthmus는 method를 먼저 거르고 경로 후보가 없을 때만 error를 내므로
  차이는 거짓 match 쪽이지 거짓 error가 아니다.
- channel·paramConstraints는 Django와 같은 토큰열 규칙이다. `any(a, b)`는 템플릿 여러 개로 펼친다.
  `string(length=2)` 같은 길이 제약은 `regex`(`[^/]{2}`)다.
- trailingSlash: 규칙 옵션 → `url_map.strict_slashes` 대입 → 기본 True 순서로 정한 값이 True면 `strict`, False면
  `optional`, 리터럴이 아니면 생략이다. `{**}`로 끝나면 생략한다.
- method: 선언한 동사에서 GET이 있으면 HEAD를 빼고, 자동 OPTIONS는 목록에 없으므로 내지 않는다. 명시한
  OPTIONS는 낸다(뷰가 직접 처리한다).
- `Flask(...)`·`Blueprint(...)`는 모듈 수준과 함수(앱 팩토리) 안 대입에서 찾고, 이름은 반복 변수(리터럴 목록을
  도는 `for`) → 함수 지역 → 바깥 함수 → 모듈 → import 순서로 푼다. 앱 팩토리 안의 import(`from app.auth import bp
  as auth_bp`, `from . import main` 뒤 `main.bp`)도 그 함수와 안쪽 함수의 지역 묶음으로 따라가며, 지역에서 묶은 이름은
  같은 이름의 모듈 수준 import를 가린다.
- 앱에 닿는 등록을 찾지 못한 블루프린트의 규칙은 `pathAnchor: "base"`(규칙만)와 `unresolved-route-prefix:`다.

## 한계와 스코프

| 상황 | 결과 |
|---|---|
| 설정 모듈·`ROOT_URLCONF`를 찾지 못함 | 사실 0건 + `route-coverage:` |
| `FORCE_SCRIPT_NAME` 설정 | 모든 사실 `pathAnchor: "base"` + `unresolved-route-prefix:` |
| 조건문(`if`·`try`) 안 등록 | decl 대신 `route-coverage:`, 템플릿을 확정하면 `templates` 스코프 |
| 반복문으로 만든 목록, 모르는 호출·식, 모르는 호출로 바꾼 목록 | 그 자리에 `route-coverage:`, include 접두사가 정적이면 세그먼트 경계 `templatePrefixes` 스코프 |
| 프로젝트 밖 URL 모듈 include | Django·DRF 모듈이면 `framework-provided-routes:`, 그 밖은 `route-coverage:`, 접두사 스코프 |
| `admin.site.urls` | `framework-provided-routes:` + 접두사 스코프(`/admin`) |
| `static()`(DEBUG 전용) | `framework-provided-routes:` + 접두사 스코프, prefix를 모르면 스코프 없음 |
| `django.contrib.staticfiles` | `framework-provided-routes:` + `STATIC_URL` 접두사 스코프(외부 host면 없음, 모르면 스코프 없음) |
| `INSTALLED_APPS`를 읽지 못함 | `framework-provided-routes:`(스코프 없음) |
| Flask 정적 파일 | `framework-provided-routes:` + `templatePrefixes: [static_url_path]`, `methods: ["GET", "HEAD"]` |
| 라우트를 따로 등록하는 Flask 확장(flask_restful·flask_restx·flask_smorest·connexion·flask_appbuilder·flask_admin) import | `route-coverage:`(스코프 없음) |
| method를 확정하지 못한 뷰(모르는 기반 클래스·장식자 인자·뷰 식) | 아는 method 또는 `ANY` + 그 템플릿 `templates` 스코프의 `route-coverage:` |
| 템플릿으로 확정할 수 없는 경로 | dynamic 사실 + `route-coverage:`(펼침 상한은 `route-template-expansion-capped:`) |
| i18n 접두사 | 그 아래 사실 `pathAnchor: "base"` |
| 맨 앞 include 문자열이 리터럴이 아님(앞 조각이 빈 문자열·`re_path`의 `^`·언어 접두사뿐) | 그 조각을 떼고 하위 사실 `pathAnchor: "base"` + `unresolved-route-prefix:`. 앞에 리터럴 경로가 있으면 dynamic |
| 프로젝트 밖 핸들러(usr 없음) | `missing-route-usrs:`(체인 전용) |
| 파싱 실패(문법 오류·실행 중인 파이썬보다 새 문법·UTF-8 아님·4 MiB 초과), 심볼릭 링크, 순회 상한(200,000 항목, 깊이 64) | `route-coverage:` |
| 버전 선언을 확인하지 못함 | `route-framework-version-unknown:` |

스코프는 그 한계가 가릴 수 있는 모든 요청의 상한일 때만 싣는다. 스코프는 문서 조립 전에 계약 검사
(`scope_problem`, 공유 벡터 `scope.validate`로 검증)를 통과해야 하며, 통과하지 못하면 스코프 없이 남긴다. 루트
`/`만이고 method 제한이 없는 스코프는 문서 전체 효과와 같으므로 싣지 않는다.

## 결정 사항

- **Django는 registration-order, Flask는 specificity.** 계약(GRAPH-EXCHANGE "디스패치 모델")의 추정과 같고 위
  소스로 확인했다. isthmus의 현재 버전은 `registration-order` 문서를 거부하므로, 그 전까지 쓸 수 있게
  `--dispatch specificity`(Django 문서를 specificity로 선언하고 `order`를 싣지 않음)를 둔다. 이 근사의 실패 방향은
  가려진 패턴이 match되는 거짓 match뿐이다. isthmus가 method를 먼저 거르고 경로 후보가 없을 때만 error를 내며,
  method 불일치는 모든 후보가 그 method를 받지 않을 때만인데 Django도 그때는 첫 매치에서 405를 낸다.
- **가려진 패턴도 선언이다.** 등록 순서상 닿지 않는 패턴도 소스의 선언이며, 가림 판정은 소비자
  (`route-decl-shadowed`)가 `order`로 한다.
- **조건부 등록은 decl이 아니라 스코프 있는 한계다.** 설정에 따라 없는 경로를 선언으로 내면 거짓 match가 되고,
  빼면 거짓 error가 되므로, "있을 수 있음"을 템플릿 스코프 한계로 나타낸다.
- **HEAD·자동 OPTIONS는 내지 않는다.** tsograph(Next.js)와 같은 결정이다. 소비자의 `head-as-get`·`options-any`가
  처리한다.
- **WSGI 마운트 접두사(`SCRIPT_NAME`)는 모델링하지 않는다.** 배포 설정이라 저장소에서 알 수 없다. 설정 파일의
  `FORCE_SCRIPT_NAME`만 base 앵커로 반영한다.
- **파이썬 3.10 이상.** Django 5.x가 3.10 이상을 요구하고, 분석 대상의 3.10 문법(`match` 문)을 파싱하려면 실행
  인터프리터가 3.10 이상이어야 한다. 3.11+ 전용 기능은 쓰지 않는다. 분석 대상이 실행 인터프리터보다 새 문법을
  쓰면 그 파일은 한계가 되므로 pythograph는 프로젝트와 같거나 새 파이썬으로 실행한다.

## 오라클 검증

`experiments/oracle/`의 하네스는 스크래치 가상 환경(Django 5.2.17·DRF 3.18.1·Flask 3.1.3)에서 fixture 앱을 실제로
import해 비교한다. 제품은 분석 대상을 실행하지 않는다.

- Django: `get_resolver()`를 등록 순서대로 순회해 끝점(패턴 사슬·핸들러·허용 method)을 덤프하고, pythograph의
  정적 사실마다 표본 경로를 만들어 **그 사실이 가리키는 끝점의 패턴 사슬**에 맞는지, 핸들러 id와 method 허용이
  같은지, `trailingSlash` 판정이 맞는지 확인한다(가려진 패턴은 `shadowedBy`에 가린 route를 적고 검증). `order`의
  순서가 순회 순서와 같은지도 본다. 클래스 기반 뷰의 허용 method는 프레임워크 계산(`allowed_methods`·
  `_allowed_methods()`·`actions`)으로, 함수 뷰는 `RequestFactory` 탐침(405 여부)으로 구한다.
- Flask: `app.url_map`의 규칙(자동 HEAD·OPTIONS 제외)과 `url_map.bind(...).match(표본, method)`로 같은 확인을 한다.

| 대상 | 정밀도 | 재현율 | 비고 |
|---|---|---|---|
| `fixtures/django/drf-shop` | 69/69 = 100% | 58/59 | 놓친 1건은 전방 탐색 정규식(의도한 dynamic). DRF 형식 접미사 변형 16건 중 10건 일치(상세 변형 6건은 계약상 dynamic) |
| `fixtures/flask/blog-app` | 28/28 = 100% | 27/27 | |
| HackSoftware/Django-Styleguide-Example `a70ef43`(MIT, 스크래치에 복제, 저장소에 넣지 않음) | 21/21 = 100% | 21/22 | 놓친 1건은 DEBUG 전용 `static()` media 경로(`framework-provided-routes:`로 신고) |

기록(`experiments/oracle/recorded/*.json`)은 커밋하고, `tests/test_fixtures.py`가 오프라인으로 지금 출력의 정적
사실이 기록에서 검증된 사실과 정확히 같은지 확인한다. 다시 기록하려면 스크래치 가상 환경의 파이썬으로
`python experiments/oracle/run_all.py`를 실행한다.

## isthmus 호환

isthmus `main`(`78d3dee`)은 `platform: "python"` 문서를 `Unsupported bridge platform.`(종료 코드 2)으로 거부한다.
`src/exchange/parse.ts`의 네 곳(플랫폼 유니온 타입, `bridgePlatforms`, `httpPlatforms`, `routeKindPlatforms`의
`route-decl`)에 `python`을 더하면 Flask 문서와 `--dispatch specificity` Django 문서가 그대로 받아들여지고 check가
method 불일치·선언 없는 호출 error와 스코프 있는 한계의 `-unverified` 강등을 기대대로 낸다(스크래치 사본으로
확인). 기본 Django 문서(`registration-order`와 `order`)는 isthmus가 registration-order 디스패치를 구현해야 받는다.
