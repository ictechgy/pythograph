# Changelog

이 프로젝트의 주요 변경 사항을 기록한다.

## [Unreleased]

### Fixed

- `graph`·`reach`·`impact`가 `revision`용 git을 PATH의 절대 경로 항목에서만 찾아 그 절대 경로로 실행한다. 빈·상대
  PATH 항목, 프로젝트 안, 현재 디렉터리에 바로 있는 git(Windows에서 현재 디렉터리의 `git.exe` 포함)은 실행하지 않고,
  찾지 못하면 `revision`을 생략한다. 프로젝트·현재 디렉터리는 파일 신원(장치·inode)으로 비교해 대소문자·유니코드
  정규화만 다른 PATH 철자(macOS 기본 APFS)도 막는다. 분석 대상 저장소에 심은 git이 실행될 수 있던 문제다.

- 구문 트리 중첩이 128단을 넘는 모듈(긴 연산자·속성·호출·첨자 사슬, 긴 `elif`)은 분석하지 않고 파싱 실패와 같은
  한계(graph `unparsed-files:`, routes `route-coverage:`·`route-call-coverage:`, schema `unreadable-sources:`)로 남긴다.
  파일 하나 때문에 모든 명령이 내부 오류(RecursionError, 종료 2)와 빈 출력으로 끝나던 문제다.
- Python 3.10에서는 한 논리 줄의 사슬 토큰(이항 연산자·`.`·`(`·`[`)이 5만 개를 넘는 모듈을 파싱 전에 같은 파싱
  실패로 거른다. f-string 치환 필드 안의 사슬(`f"{a+a+…}"`, 그런 문자열 주석 포함)도 그 줄에 더해 센다. `ast.parse`가
  세그폴트해 종료 코드 139와 빈 출력으로 끝나던 문제다. 얕은 한 줄 목록이라도 사슬 토큰이 5만 개를 넘으면 함께
  걸러진다.
- 문자열 주석 파싱을 한 함수로 모아, NUL이 든 주석(3.10)·아주 긴 주석에서 schema가, 깊은 `"X | None | …"` 주석에서
  graph·`routes --role client`가 내부 오류로 끝나던 문제를 고친다. 그런 주석은 모르는 주석으로 본다.
- 50단보다 깊게 중첩된 `re_path`·변환기 정규식은 변환하지 않고 dynamic 사실과 `route-coverage:` 한계로 낸다. 깊게
  중첩된 정규식 하나로 `routes --role server` 전체가 내부 오류(종료 2)로 끝나던 문제다.

- 이름(디렉터리 성분 포함)에 교환 형식이 금지하는 문자(NEL을 포함한 C0·C1 제어 문자, U+2028/U+2029)가 든 파이썬
  파일은 분석하지 않고 한계(graph `scan-incomplete:`, routes `route-coverage:`·`route-call-coverage:`, schema
  `unreadable-sources:`)로 센다. 그런 이름이 `symbol.usr`·`location.path`에 실려 isthmus가 문서 전체를 거부하던 문제다.
- 짝 없는 서러게이트가 든 dynamic 라우트 원문과 정규식 `pattern`, 제어 문자가 든 `pattern`을 싣지 않는다. 출력
  쓰기가 UnicodeEncodeError로 종료 2와 빈 출력이 되거나 isthmus가 `paramConstraints`로 문서를 거부하던 문제다. 그래도
  UTF-8로 쓸 수 없는 문서는 원인(짝 없는 서러게이트)을 밝히고 종료 2로 끝나며, 래퍼 선언 텍스트의 서러게이트는 선언
  오류(64)다.
- `routes --role server`가 교환 형식의 method 집합(GET·HEAD·POST·PUT·PATCH·DELETE·OPTIONS·TRACE) 밖 동사(Flask
  `methods`, Django `http_method_names`, viewset 매핑, `require_http_methods`·`api_view`·`@action`의 `PURGE`·`PROPFIND`
  등)를 route-decl로 내지 않고 그 등록의 `route-coverage:` 한계로 남긴다. 템플릿을 증명한 등록이면 그 템플릿 스코프를
  싣고, `ANY`로 바꾸지 않는다. 동사 하나 때문에 isthmus가 서버 문서 전체를 입력 오류(종료 2)로 거부하던 문제다.
- `routes --role client`가 래퍼 선언의 `service`가 `--service`와 다른 사실을 내면 문서 수준 `service`를 싣지 않는다
  (사실마다의 service는 그대로다). 문서 값과 사실 값이 갈려 isthmus가 문서 전체를 입력 오류(종료 2)로 거부하던 문제다.
- Flask 규칙의 템플릿 펼침이 16개를 넘으면(`any()` 곱, 큰 `any()`, 빈 값 변형) Django와 같이
  `route-template-expansion-capped:`로 센다(전에는 `route-coverage:`).

- `routes --role client`가 `globals()`(어느 범위든)·모듈 수준 `vars()`·`locals()`를 부르는 모듈의 이름과, 수신자를
  모르는 같은 이름의 속성 대입(`sys.modules[__name__].X = …`)이 있는 이름을 URL 상수로 치환하지 않는다(graph와 같은
  판정). 런타임에 바뀌는 base URL이 정적 `root`·`authority` 사실로 나가던 문제다.
- 디렉터리 순회가 소스를 가릴 수 있는 이름의 심볼릭 링크(끊어진 것을 포함한 `*.py` 이름, 확장자가 없고 순회가
  건너뛰지 않는 이름)만 한계로 센다. `CLAUDE.md -> AGENTS.md`·`README.md`·`.venv` 같은 링크 하나가 스코프 없는 한계가
  되어 isthmus가 그 문서의 `route-call-without-decl`·`route-method-mismatch` 진단을 모두 완화하고 graph `bound` 닫힌
  세계 증명을 막던 문제다. 링크 대상에는 접근하지 않아, 분석 머신에 어떤 경로가 있는지가 한계 수로 드러나지 않는다.

- `routes --role client --wrappers`가 선언 파일을 일반 파일이나 파이프일 때만 열고 최대 4 MiB + 1바이트만 읽는다.
  파일 전체를 읽은 뒤 자르던 탓에 큰 파일·`/dev/zero` 링크가 상한과 무관하게 메모리를 쓰고, 쓰는 쪽이 없는 FIFO는
  명령을 멈추던 문제다. 디렉터리·장치는 내용을 읽지 않고 2로 끝난다(마지막 링크는 지금처럼 따라간다). 파이프(셸
  프로세스 치환 `<(…)`)는 지금처럼 읽고, 쓰는 쪽이 없는 FIFO는 기다리지 않고 빈 입력으로 읽는다.
  `reach`·`impact --roots-from`의 파일도 같은 규칙이다(일반 파일·파이프가 아니면 64).
- `requirements*.txt`·`requirements/*.txt`를 찾는 디렉터리 열거도 순회 항목 상한(200,000개)을 지킨다. 넘친
  디렉터리는 일부만 고르지 않고 요구 파일을 읽지 않는다.

- 같은 상수를 여러 번 참조하는 f-string·`+`·목록 사슬(`B = f"{A}{A}…"`)을 한 번만 펼치고, 이렇게 만든 값이 64 KiB
  (클라이언트 URL은 조각 4,096개)를 넘으면 모르는 값으로 본다. 수백 바이트 모듈 하나로 schema·routes가 수 초~수 분과
  수백 MiB를 쓰던 문제다. 상한을 넘은 SQL 텍스트는 `skipped-sql-literals:` 한계로 센다.
- 8 KiB(템플릿 상한의 4배)보다 긴 라우트 원문(슬래시를 합친 Flask 규칙, include 접두사까지 이은 Django 경로)과,
  그보다 짧아도 리터럴 글자(Flask 변환기 밖 글자, Django `path()` 변환기 자리 밖 글자와 `re_path` 묶음 밖 리터럴)만으로
  템플릿 상한(2,048자)을 넘는 경로는 변환하지 않고 너무 긴 템플릿과 같은 dynamic 사실로 낸다. 나머지 변환(Flask
  규칙·변환기 인자, Django 변환기 자리·정규식 토큰열)은 원문 길이에 선형으로 만든다. 수십 KB 경로 하나로, 또는 긴
  상수를 경로마다 재사용한(`path(BIG + "eN/")`) 수 KB 모듈 하나로 `routes --role server`가 수십 초~수 시간을 쓰던
  문제다.
- 경로 파라미터의 종류(`int`·`slug` 등)를 정할 때 문자 집합마다 ASCII 판정을 한 번만 한다. 같은 변환기를 수백 번
  쓰는 경로(`<int:p0>/<int:p1>/…`)에서 파라미터마다 128자를 다시 맞춰 봐서 8 KiB 경로 하나에 약 28 ms(이제 약
  10 ms)가 들던 문제다. 출력은 같다.
- 버전 선언(`requirements*.txt`·잠금 파일·`pyproject.toml`·`Pipfile`·`setup.cfg`)과 graph 라이브러리 판정의
  `pyproject.toml` 표 머리를 읽는 정규식을 입력 길이에 선형으로 바꾼다. 긴 공백·빈 줄·닫히지 않은 괄호가 든 선언
  파일 하나로 schema·routes·graph·reach·impact가 수 시간 멈출 수 있던 문제다. PEP 508·TOML에 맞는 선언의 결과는 같다.
- 대문자 SQL 리터럴과 SQL 인자의 관계 스캔·DB-API 파라미터 치환을 리터럴 길이에 선형으로 만든다. 수십 KB 리터럴
  하나로 schema가 수 초~수십 초(4 MiB면 수 시간)를 쓰던 문제다. 출력은 같다.
- 같은 부모를 여러 경로로 물려받는 다이아몬드 상속 격자를 (클래스, 깊이)마다 한 번만 편다(뷰 클래스 분석, SQLAlchemy
  매핑 해석). 1 KB 남짓한 모듈 하나로 graph·schema·routes가 깊이 20에서 수십 초를 쓰고 상속 깊이 상한(32)에 닿으면
  끝나지 않던 문제다. 출력은 같다. 파이썬으로 만들 수 없는 촘촘한 정적 상속 순환 때문에 같은 클래스를 1,024번 넘게
  다시 펴야 하면 남은 기반을 모르는 기반으로 본다.
- `routes --role server`가 서로(또는 자기)를 등록한 Flask 블루프린트의 순환을 닫는 등록을 따라가지 않고
  `route-coverage:` 한계로 센다. 순환 하나로 내부 오류(RecursionError, 종료 2)와 빈 출력으로 끝나던 문제다. 같은
  블루프린트를 여러 경로로 등록한 격자는 계약 동사 사실이 100,000개를 넘는 순간 멈춰 같은 사실 상한 오류를 빨리
  내고, 하위 등록이 200,000개를 넘으면 `route-coverage:` 한계와 함께 멈춘다(규칙 없는 격자가 끝나지 않던 문제).

- 모듈 이름 해석 결과를 한 실행 안에서 기억한다. `manage.py`가 여러 개(모두 모듈 루트)인 저장소에서 같은 외부
  이름을 찾을 때마다 루트 수 × 후보 2개의 성분별 lstat을 되풀이하던 문제다(합성 6서비스 monorepo에서 schema
  17.1 → 1.6 s, graph 10.2 → 1.6 s). 출력은 같다.
- graph가 클래스 수준 대입을 클래스마다 한 번만 모은다. 메서드만 있는 클래스를 MRO 조회마다 다시 훑던 문제다. 출력은
  같다.
- schema의 Django 모델·SQLAlchemy 매핑 선언 단계가 모듈마다 자기 모듈의 모델·테이블만 본다. 모듈마다 전체 모델을
  훑어 모듈 수 × 모델 수가 들던 문제다(합성 앱 800개에서 6.8 → 4.9 s). 출력은 같다.
- Flask 앱 팩토리 안 `add_url_rule(view_func=지역 함수)`가 등록마다 모듈 구문 트리 전체를 걷지 않는다(등록 4,000개
  9.5 → 0.35 s). 출력은 같다.

### Changed

- CI와 릴리스 빌드가 hatchling과 그 의존성의 버전·해시를 고정한 `build-constraints.txt`를 `--require-hashes`로
  쓰고, setup-uv의 uv를 0.12.17로 고정한다. 태그 시점의 PyPI 최신 빌드 백엔드가 게시물에 들어가지 않게 하기
  위해서다. 다시 만드는 절차는 `RELEASING.md` "빌드 도구 고정"에 있다.

## [0.2.2] - 2026-10-13 (UTC)

PyPI에 발행했다.

### Fixed

- 같은 검색 루트의 `name.py`와 `name/__init__.py`가 공존하면 Python import와 같이 일반 패키지를 먼저
  선택한다. 가려진 모듈로 향하던 거짓 직접 호출 간선과 실제 패키지 대상의 누락을 바로잡는다.

- 모듈·패키지 이름 해석과 소스·의존성 선언 읽기에서 마지막 파일뿐 아니라 모든 프로젝트 상대 경로 성분의 링크를
  거부한다. Django settings·URLconf 문자열이 링크 부모를 통해 프로젝트 밖 파일의 사실을 가져오던 경계를 바로잡는다.
- 내부를 가리키는 링크와 Windows 디렉터리 junction도 제외하며, 같은 링크는 중복 없이 한계에 센다.
- 지원하는 운영체제에서는 디렉터리 핸들 상대 `O_NOFOLLOW`로 순회·읽기를 고정하고, 읽기 전에 파일 신원을 확인한다.
  링크 교체 경쟁 회귀 테스트와 대체 경로의 원자성 한계를 문서화한다. 분석 대상 코드는 여전히 import·실행하지 않는다.

- 파일 전체를 읽은 뒤 자르던 경로를 제한된 스트림 읽기로 바꿔 4 MiB 상한을 실제 메모리 사용에 적용한다.
- 디렉터리 전체를 정렬하기 전에 순회 항목 상한을 적용한다. 초과 디렉터리는 임의 부분 수확 대신 통째로 제외하고 한계를 남긴다.
- 디렉터리·항목 읽기 실패를 모든 생산자의 한계에 세고, 누락 모듈이 있는데도 `bound` 근거를 확정하던 경로를 막는다.

## [0.2.1] - 2026-10-01

### Fixed

- SQL FROM/JOIN의 테이블 값 함수를 관계로 추측하지 않고 미해석 피연산자로 센다.

## [0.2.0] - 2026-10-01

0.1.0 이후 #7–#12를 담는다. 클라이언트 route-call(`routes --role client`, #10)로 파이썬 호출부를 서버 route에 잇고, `bound` 근거
등급과 대형 그래프 스냅샷 상한 분리(#9)로 호출 그래프를 넓혔다. isthmus 공유 벡터를 두 번 다시 벤더링했고(#8, #12), 문서를
정리했다(#7, #11). python route-decl·route-call 문서를 받는 isthmus 발행 버전은 isthmus-cli 0.10.0이다.

### Added (클라이언트 route-call, #10)

- `pythograph routes --role client`: requests(최상위 함수·`Session`)·httpx(최상위 함수·`Client`·`AsyncClient`의 `base_url`)·
  aiohttp(`ClientSession`의 `base_url`·`aiohttp.request`)·`urllib.request.urlopen`(`Request(method=)`·`data`) 호출과
  `--wrappers`(isthmus `http-wrappers` v1, `"language": "python"`)로 선언한 래퍼 호출을 isthmus http `route-call` 사실로 낸다
  (`platform: "python"`, `roles: ["client"]`). `symbol.usr`는 감싸는 선언의 pythograph id라 `graph`·`reach`·`impact` 정점과 같다.
  규칙과 확인한 소스는 `docs/HTTP-CLIENTS.md`.
- base 결합은 isthmus 확정 이름을 따른다: `httpx-base-url`(끝 `/` 보장, 경로 앞 `/` 모두 제거, 점 세그먼트 제거),
  `aiohttp-base-url`(RFC 3986, 경로 있는 base·상대 경로는 3.11 이상, base 세션의 절대 URL은 3.12 이상을 선언 파일로 증명할 때만
  주장 — `versions.declared_minimum`). `urllib.parse.urljoin`은 벡터가 없어 절대 URL과 `/` 경로만 주장한다.
- URL 조립: f-string·`+`·`%` 서식·`str.format`, 한 번만 묶인 모듈 상수(다른 모듈 포함, `global`·모듈 속성 대입이 있으면 아님),
  모든 대입이 같은 리터럴인 클래스 속성·`__init__` 필드, 증명한 query 꼬리 지역 변수, 고엔트로피·웹훅 마스킹. dynamic 사실은
  `channel: null`과 마스킹한 `channelPrefix`를 싣는다.
- 한계: `route-call-coverage:`(모델링하지 않은 요청 API, 타입 모르는 수신자의 URL 리터럴 호출, 스캔 공백),
  `ambiguous-base-join:`, `http-wrapper-undeclared:`, `http-wrapper-unresolved:`.
- 모의 서버 오라클(`experiments/client_oracle/`, `fixtures/client/shop-client`): 스크래치 가상 환경에서 실제 requests 2.34.2·
  httpx 0.28.1·aiohttp 3.14.3·urllib 요청을 127.0.0.1 `http.server`에 기록해 35개 시나리오 일치 31·dynamic 4·불일치 0.
  `tests/test_client_oracle.py`가 기록과 golden(`tests/golden/client-shop-client.json`)을 오프라인으로 다시 확인한다.
- 파이썬 클라이언트 × Django 서버 종단 trace(`experiments/client_e2e/`, `fixtures/e2e/py-client`): isthmus main `3a45450` 빌드로
  route 네 개 모두 호출부(exact) → 핸들러 → relation-use, 호출부 → 화면 함수가 이어진다(`tests/test_client_e2e.py`).
- isthmus 공유 적합성 벡터를 `3a45450`(#133)으로 다시 벤더링했다. 실행하는 생산자 사례는 `producer`·`producer:pythograph`
  135건(url-compose 57건 포함)이며 모두 통과한다. `scope.dynamic-*`은 `dynamicScope`를 아직 내지 않아 건너뛴다.

### Added (bound 근거 등급·대형 그래프 스냅샷, #9)

- `bound` 근거 등급(tsograph와 같은 계약): 타입 모르는 수신자·인스턴스 속성 수신자·재정의 후보가 있는 주석 수신자의 메서드
  호출은 수신자로 들어오는 관찰된 값이 모두 프로젝트 클래스 인스턴스일 때만 구현마다 `bound` 간선을 둔다. 전체 프로그램 값
  흐름(`graph/flow.py`): 생성자, 재대입한 지역 이름, 모듈 수준 인스턴스(조건부 대입·`global`·몽키패치 쓰기 합침), `__init__`
  매개변수로 받은 속성(DI), 호출 지점이 모두 프로젝트 안인 함수 매개변수, 팩토리 반환 값. 열린 자리(`graph/exposure.py`):
  라이브러리 공개 이름(`setup.py`·`setup.cfg`·`pyproject.toml` `[project]`/`[tool.poetry]`, 설치하지 않는 앱 표시는 제외), 값으로
  새는 함수·클래스(뷰·시그널 등록), 장식한 함수·클래스(Celery·시그널·래퍼), 프레임워크 기반 클래스(관리 명령·뷰), 문자열로 적은
  이름, 호출 지점 없는 함수, 메서드 매개변수, `self`·`cls`, `*args`·`**kwargs` 펼치기, 계산된 `getattr`·모듈 이름공간
  노출(`globals()`·`sys.modules`·동적 import), 계산된 이름의 쓰기(`setattr`·`__dict__`·`vars()`·`__class__`) 대상, 속성 훅·서술자·
  메서드 가림, 동적 생성·`exec`·동적 하위 클래스, 물려받은 메타클래스·`__new__` 쓰기, 조건부 재정의, 불완전한 스캔, 테스트 소스(별도 프로그램 — 테스트가 아닌 모듈이 import하면 함께 훑는다).
  `bound`·`candidates` 모드의 미해석 수에서 이은 호출을 빼고, `statistics.boundDispatch`와 `bound-dispatch:`·`bound-assumptions:`
  한계를 싣는다. 기본 모드는 `direct`를 유지한다(공개 앱 네 개에서 이은 호출 0건, `DOGFOOD.md`).
- 건전성 탐침(`tests/test_graph_bound_probes.py`): 흐름을 숨기는 기법을 섞은 합성 프로그램을 테스트 안에서 실행해 런타임
  수신자가 `bound`·direct 대상에 모두 있는지 본다(로컬 3,000개 위반 0건). 순회 오라클에 `bound` 모드·모드 사이 등급 포개짐·
  실제 그래프 비교를 더했다.
- `graph` 스냅샷 출력 상한을 isthmus 입력 상한(16 Mi 문자)에서 떼어 256 Mi 문자로 올렸다(스냅샷은 isthmus 입력이 아니다,
  직렬화 봉우리 약 2바이트/문자). netbox 규모 스냅샷(21 Mi 문자)이 이제 끝까지 나온다. `reach`·`impact`는 16 Mi 그대로다.

### Fixed (direct 등급, #9)

- 모듈 전역·클래스 본문 속성을 다시 쓰는 곳(두 번째 모듈 수준 대입, `global` 대입, `globals()`, 같은 이름의 속성 쓰기·리터럴
  `setattr`, 클래스 본문 중복 대입)이 있거나 값이 서술자면 정확한 수신자로 보지 않는다. 전에는 마지막 무조건 대입 값을 정확한
  값으로 보아 몽키패치된 전역·클래스 속성의 호출을 빠뜨렸다(건전성 탐침이 찾았다). 이름 기준이라 보수적이다(babybuddy에서
  클래스 속성 간선 7개가 빠졌고 핸들러 도달은 같다). 새 해석기 이유는 `local-value`·`dynamic-attribute`로 묶는다.

### Changed

- isthmus 공유 적합성 벡터를 `76b6141`로 다시 벤더링했다(#8). `url-compose.json`에 Spring base 결합 케이스 13개
  (`base-join/spring-*`, `producer:kartograph`)가 더해져 sha256만 바뀌었고, pythograph는 `compose.*`를 건너뛰므로 실행 사례(78건)는 같다.
- isthmus 공유 적합성 벡터를 `2954375`(#134, aiohttp `versionRange` 보강)로 다시 벤더링했다(#12). pythograph 동작은 그대로다.
- README 설치 절을 PyPI(`uv tool install pythograph`·`pipx install pythograph`) 기준으로 바꿨다(#7).
- AGENTS.md·DOGFOOD.md 로드맵 문구를 동기화했다(#11): 구현된 `bound` 근거 등급을 장기 범위에서 빼고, netbox `graph` 출력 상한
  초과가 #9 재측정에서 해결됐음을 연결했다.
- README(en/ko)의 isthmus 호환 절: python route-decl·route-call을 받는 발행 버전으로 개발 commit(`3a45450`) 대신 isthmus-cli
  0.10.0(npm)을 적었다.

## [0.1.0] - 2026-09-30

### Changed

- Phase 6 종료 조건 기록(`experiments/e2e/recorded/`)의 Android 문서를 kartograph `4c09d91`(#122 Retrofit route-call usr·상속
  인터페이스 호출 간선, #123 Retrofit baseUrl 결합)로 다시 기록하고, route 선택에 `POST /api/checkout/`을 더해 trace를 다시
  만들었다. Android `OrdersService.getOrder` → `OrderRepository.load`(1) → `OrderViewModel.refresh`(2)와 `OrdersService.checkout`
  → `CheckoutRepository.submit`(1) → `CheckoutViewModel.pay`(2)가 체인에 붙고, `unattributed-calls-omitted` gap(route 3개·
  relation 2개)이 사라졌다. **Android 기록은 kartograph `4c09d91` 이상에 의존한다** — 그 전 판으로 다시 기록하면
  `tests/test_e2e_trace.py`가 실패한다. iOS 기록(cartograph 0.22.0)과 서버 문서는 바뀌지 않았다.
- `experiments/e2e/record_clients.py --client ios|android`: 한 클라이언트만 다시 기록한다(생략하면 둘 다).

### Added

- 릴리스 준비(게시는 아직 하지 않음): `.github/workflows/release.yml` 초안(`v*` 태그 push → ubuntu·macOS × Python 3.10·3.13 검증 →
  태그·버전 대조와 sdist·wheel 빌드, 깨끗한 가상 환경의 wheel 설치·`--version`·CLI 계약 확인 → 환경 `pypi`에서 PyPI Trusted
  Publishing 게시, 액션은 커밋 SHA 고정)과 관리자가 직접 할 절차를 적은 `RELEASING.md`.
- `DOGFOOD.md`: 공개 앱 네 개(Django-Styleguide-Example, babybuddy, microblog, netbox)의 route 오라클 정밀도·재현율,
  schemagraph·isthmus 조인율, 핸들러 → relation-use 도달률, 미해석 호출 분포, 실행 시간·메모리와 고친 문제.

- `pythograph graph`·`reach`·`impact`: 표준 라이브러리 `ast`로 만든 파이썬 호출 그래프(`pythograph-graph` v1 스냅샷)와
  isthmus `language-traversal` v1 순회 문서(`docs/GRAPH.md`). 정점은 모듈·함수·메서드·클래스·중첩 정의와 routes가 등록
  클래스 기준으로 내는 상속 멤버 핸들러(`<클래스>.<멤버>`)이고, id는 routes·schema `symbol.usr`와 같다. 간선은 import(절대·상대·
  별칭·`__init__` 재수출·`*`)·모듈 속성·생성자(`__init__`)·C3 MRO로 푼 `self`·`super()`·주석 수신자·property·장식자·콜백·참조·
  클래스 속성·상속이며 근거 등급 `direct`와 하위 클래스 재정의 `candidate`를 싣는다(`bound`는 아직 없다 — `--dispatch bound`는
  direct 그래프다). 대상을 모르는 호출은 추측하지 않고 이유별로 세어 `unresolvedCalls`로 낸다(타입 모르는 수신자는 프로젝트가
  그 이름을 정의·대입하지 않을 때만 외부로 확정).
- 프레임워크 디스패치: Django 5.2.17·DRF 3.18.1·Flask 3.1.3 설치본 소스를 ast로 읽어 만든 훅 표
  (`experiments/graph/dump_framework_hooks.py` → `graph/framework_table.py`)로 `as_view()` 디스패치 경로와 프레임워크 구현
  (`ModelViewSet.retrieve` → `get_queryset`, `ModelSerializer.save` → `create`)이 부르는 프로젝트 훅을 잇는다. 프레임워크가 클래스
  속성으로 만드는 객체는 `framework-callback` 미해석으로 센다.
- 순회: `--dispatch direct|bound|candidates`, `--max-depth`, `--max-reached`, `--roots-from <file|->`(JSON 배열·bridge-facts),
  `--revision`, `--generated-at`. 다중 root 단일 패스(root별 오라클과 무작위 그래프 비교 테스트), root별 하한 `evidence`,
  `rootsTruncated`, 정점이 아닌 root는 문서를 쓰고 64(root-not-found), 제어 문자 id는 64. `revision`은 작업 트리가 깨끗할 때의
  git HEAD(또는 `--revision`), `graphRevision`은 그래프 내용 SHA-256.
- `exchange/order.py`: registration-order `order` 검증기. isthmus `f9dcd1d`의 `http-dispatch` 벡터를 벤더링하고
  `dispatch.validate` 18건과 routes 출력 golden에 적용한다(생산자 사례 78건 100%).
- Phase 6 종료 조건 fixture(`fixtures/e2e/shop-api`, `experiments/e2e/`): 합성 Django+DRF 서버, Django DDL의 schemagraph 카탈로그,
  합성 iOS(cartograph)·Android(kartograph) 클라이언트를 isthmus `trace`(workspace)로 잇고 세 질문(API → DB·DB 의존자, API →
  클라이언트 호출부 → 영향 심볼, 테이블 → API → 클라이언트)의 기대 경로 일치를 기록한다. `tests/test_e2e_trace.py`가 오프라인으로
  다시 확인한다.

- 저장소 골격: `pyproject.toml`(hatchling, Python 3.10 이상, 런타임 의존성 없음, 콘솔 스크립트 `pythograph`),
  `uv tool install`·`pipx install` 배포, dev 도구(uv·pytest·pytest-cov·ruff·mypy), GitHub Actions CI(ubuntu·macOS,
  Python 3.10·3.13, lint·format·typecheck·테스트·커버리지 90%·CLI 계약·wheel 설치 계약).
- CLI: `pythograph --version`, `help`, 종료 코드 계약 0/2/64(1은 예약, 예기치 못한 내부 오류도 2),
  키 정렬 결정적 JSON, `--generated-at`으로 바이트 단위 재현.
- `pythograph routes --role server`: Django URLconf(설정 모듈·`ROOT_URLCONF`·`include`·변환기·`re_path` 정규식 구문
  트리 변환·함수/클래스 뷰 method), Django REST framework(`SimpleRouter`·`DefaultRouter`·`@action`·형식 접미사·
  `api_view`·generic view·viewset), Flask/Werkzeug(앱 팩토리·블루프린트 중첩·`add_url_rule`·`MethodView`·변환기·
  `strict_slashes`·`merge_slashes`)를 isthmus http `route-decl`(platform `python`)로 낸다. Django는
  `dispatch: "registration-order"`와 `order`, Flask는 `specificity`다. 규칙은 Django 5.2.17·DRF 3.18.1·Flask 3.1.3·
  Werkzeug 3.1.9 소스로 확인했다(`docs/HTTP-ROUTES.md`).
- 심볼 id 규칙 `<프로젝트 상대 경로>#<어휘적 점 경로>`(클래스 핸들러는 등록 클래스 기준). 다음 단계 호출 그래프가
  같은 id를 쓴다.
- `--dispatch specificity`: registration-order를 아직 받지 않는 isthmus용 근사(거짓 match만 가능, 거짓 error 없음).
- isthmus 공유 적합성 벡터를 `78d3dee`에서 벤더링하고 `conformance.lock`으로 고정했다. 해당 생산자 사례 60건을
  100% 통과한다.
- `pythograph schema`: Django 모델(추상·프록시·다중 테이블 상속, `INSTALLED_APPS`·`AppConfig` 앱 라벨, 백엔드별
  `truncate_name` 절단, M2M 중간 테이블, django.contrib 모델)과 QuerySet 사용(조회식 조인·역관계·관계 매니저·
  `values`·`F`·`Q`·집계·`raw()`·`RawSQL`·`extra(tables=)`), SQLAlchemy 2.x·Flask-SQLAlchemy 3 매핑(Declarative·
  믹스인·상속·스키마·Core `Table`·자동 snake_case 이름, 버전 미상이면 2·3 규칙이 같은 이름만)과 질의
  (`select`·`session.query`·`Model.query`·`filter_by`·`text()`), SQL 텍스트(가족 공유 추출기 포트와 공유 벡터)를
  isthmus persistence `relation-use`(platform `python`)로 낸다. 확정하지 못한 이름은 dynamic 사실과 한계다. 테스트·
  마이그레이션은 기본으로 읽지 않는다. 규칙은 Django 5.2.17·SQLAlchemy 2.0.54·Flask-SQLAlchemy 3.1.1(2.5.1 비교)
  소스로 확인했다(`docs/PERSISTENCE.md`).
- persistence 명명 벡터(`fixtures/persistence-naming/`)와 오라클(`experiments/persistence/`): 실제 ORM(Django 백엔드 4개,
  SQLAlchemy, Flask-SQLAlchemy) 이름과 100% 일치, 기록을 오프라인 테스트로 다시 확인한다. 사용 fixture 두 개의 ORM DDL로
  schemagraph·isthmus 종단 조인에서 error 0을 확인했다.
- 합성 fixture(`fixtures/django/drf-shop`, `fixtures/flask/blog-app`)와 오라클 하네스(`experiments/oracle/`):
  resolver 순회·DRF 라우터·Flask `url_map` 대비 정밀도 100%, 기록을 오프라인 테스트로 다시 확인한다.

### Fixed

- 이름 해석: 프로젝트 모듈의 `from x import *`를 패키지 `__init__`을 거쳐 거듭 따라가고 리터럴 `__all__`·밑줄 규칙을 지킨다.
  외부·`__all__` 미확정 모듈의 `*`는 가림(그래프 `star-import`)이다. routes·schema는 별 import를 전혀 따라가지 않았고
  그래프는 한 단계만 따라갔다.
- schema: `django.forms` 필드(`forms.CharField` 등)를 선언한 ModelForm·Form을 모델로 오인해 없는 테이블을 내던 문제,
  `from app import models` 뒤 `models.Book.objects`처럼 모듈 속성으로 닿은 모델을 풀지 않던 문제(바깥 함수까지 지역에서 다시
  묶으면 풀지 않는다), 조건부 `INSTALLED_APPS.remove(...)`가 앱 목록 전체를 불완전하게 만들던 문제(무조건 `remove`는 적용).
- routes(Django): 맨 앞 include 문자열이 설정값(`path(settings.BASE_PATH, include(...))`)이면 하위 경로가 모두 dynamic이던
  문제 → 그 조각을 떼고 `pathAnchor: "base"`와 `unresolved-route-prefix:`. `django.contrib.auth.views` 클래스(`LogoutView`는
  `http_method_names`로 post만)와 `Base…View`·`ProcessFormView`·`DeletionMixin`·템플릿·날짜 믹스인을 알려진 클래스 표에
  더했다(Django 5.2.17 설치본 조사). 호출 그래프 프레임워크 표도 같은 클래스로 다시 만들어 상속 핸들러 route usr가 그래프
  정점이 된다. 설정 모듈을 찾지 못한 한계 문구에 `--settings` 안내를 더했다.
- routes(Flask): 앱 팩토리 안의 import(`from app.auth import bp as auth_bp`, `from . import main`)를 따라가 `register_blueprint`
  대상과 `url_prefix`를 푼다. import 뒤 대입·반복 변수·with 대상으로 다시 묶은 이름은 풀지 않는다.
- `graph`: 16 Mi 문자 출력 상한을 넘을 때 "isthmus rejects" 대신 상한과 `reach`/`impact` 사용을 안내한다.

### Changed

- 배포 메타데이터: Python 3.10~3.13·Django·Flask·`Typing :: Typed` 분류자, Repository·Changelog URL. README의 문서 링크를
  PyPI에서도 열리도록 절대 URL로 바꾸고, 설치 절에 PyPI 릴리스 전까지의 GitHub·wheel 설치(`uv tool install`·`pipx install`)를 적었다.
- isthmus 공유 적합성 벡터를 `f9dcd1d`로 다시 벤더링했다(`http-dispatch.json` 추가).
- README·`docs/HTTP-ROUTES.md`·`docs/PERSISTENCE.md`: isthmus `main`(`f9dcd1d`)이 `platform: "python"`(http
  `registration-order`, persistence, python 순회 분석)을 받는다는 호환 정보와 호출 그래프 구현을 반영했다.
