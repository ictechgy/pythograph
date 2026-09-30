# HTTP 클라이언트 호출 규칙 (`pythograph routes --role client`)

pythograph가 파이썬 클라이언트 코드에서 isthmus http `route-call` 사실을 만드는 규칙과, 각 규칙을 확인한 공식 소스·
실행 기록을 적는다. 계약 정본은 isthmus [GRAPH-EXCHANGE의 http 절](https://github.com/ictechgy/isthmus/blob/main/docs/GRAPH-EXCHANGE.md#개발-중-http-경계-v1-확장)과
[HTTP-WRAPPERS](https://github.com/ictechgy/isthmus/blob/main/docs/HTTP-WRAPPERS.md)(특히
[Go, Rust, Python 클라이언트](https://github.com/ictechgy/isthmus/blob/main/docs/HTTP-WRAPPERS.md#go-rust-python-클라이언트) 절)이고,
결합 규칙은 url-compose 벡터가 고정한다. isthmus `3a45450`(#133)부터 `platform: "python"`의 `route-call`을 받는다.

분석 대상 코드는 표준 라이브러리 `ast`로만 읽는다. import·실행·네트워크 접근을 하지 않는다.

```sh
pythograph routes --role client --project <root> [--wrappers <file>] [--service <name>]
                  [--include-tests] [--generated-at <timestamp>] [--format json]
```

- 문서: bridge-facts v1, `platform: "python"`, `target: "http"`, `roles: ["client"]`, `sourceSets.tests`(`excluded` 기본,
  `--include-tests`면 `included`와 사실의 `testSource: true`), 선택 `service`(문서와 모든 사실). 사실이 0건이어도 target은
  `http`다(스캔했으나 호출 없음). `dispatch`는 싣지 않는다(호출 측 문서).
- `symbol.usr`(=`qualifiedName`): 호출을 감싸는 선언(함수·메서드·클래스 본문·모듈)의 pythograph id다
  (`shopclient/api.py#OrdersApi.fetch`, 모듈 수준이면 `…#<module>`). `pythograph graph`·`reach`·`impact`의 정점과 같은 문자열이라
  isthmus `trace`가 호출부에서 역방향 순회로 잇는다. 람다·컴프리헨션 안의 호출은 감싼 정의다.
- `location`: 호출식이 시작하는 줄과 1부터 센 UTF-8 바이트 열(여러 줄 호출도 시작 줄, `wrapper.location`).
- dynamic 사실의 `channel`은 null이다. 원문 식은 userinfo·query·토큰을 담을 수 있어 싣지 않고, 증명한 접두사만 마스킹한
  `channelPrefix`로 싣는다(계약의 보안 절). dynamic 사실의 `pathAnchor`는 접두사의 앵커, 접두사가 없으면 `base`다.
- 종료 코드: 0 성공, 2 읽을 수 없는 프로젝트·래퍼 파일·출력 상한 초과, 64 사용법 오류(래퍼 선언 스키마 위반 포함). 1은
  예약이다.

## 라이브러리와 확인한 소스

2026-09-30 PyPI에서 스크래치 가상 환경(Python 3.12.13)에 설치한 소스를 읽고, 아래 [모의 서버 오라클](#모의-서버-오라클)로 실제
요청을 기록해 확인했다. 라이브러리 이름은 이름 해석으로만 판정한다(`import requests as r`, `from httpx import get`) — 프로젝트가
같은 이름의 함수를 정의하면 라이브러리 호출이 아니다.

| 라이브러리(확인 버전) | 인식하는 요청 API | base 결합 | 점 세그먼트 | 근거 |
|---|---|---|---|---|
| requests 2.34.2(urllib3 2.8.0) | 최상위 `get`·`head`·`post`·`put`·`patch`·`delete`·`options`·`request`(`requests.api.*` 포함), `Session()`·`session()` 객체의 같은 메서드 | 없음(`Session`에 base URL이 없다) | 지운다(urllib3 `parse_url`) | `requests/sessions.py`, `requests/models.py` `prepare_method`(`upper()`)·`prepare_url` |
| httpx 0.28.1 | 최상위 동사 함수·`request`·`stream`, `Client`·`AsyncClient`(`base_url=`) 객체의 동사 메서드·`request`·`stream` | `httpx-base-url` | 지운다 | `httpx/_client.py` `_enforce_trailing_slash`·`_merge_url`, `_urlparse.py` `normalize_path`, `_models.py` `Request`(`upper()`) |
| aiohttp 3.14.3(yarl 1.25.1) | `ClientSession`(`base_url=`, 첫 위치 인자) 객체의 동사 메서드·`request`, 최상위 `aiohttp.request` | `aiohttp-base-url`(버전 제약) | 지운다(yarl) | `aiohttp/client.py` `__init__`·`_build_url`·`_request`(`upper()`), 3.8.0·3.8.6·3.9.5·3.10.0·3.10.11·3.11.0·3.11.18·3.12.0·3.12.15·3.13.0·3.13.3 sdist 대조 |
| urllib(CPython 3.12) | `urllib.request.urlopen(url 또는 Request(...), data)` | 없음 | 지우지 않는다(요청 줄 그대로) | `urllib/request.py` `Request.get_method`·`OpenerDirector.open` |

- 동사: 동사 함수·메서드는 그 동사다. `request(동사, url)`·`stream(동사, url)`의 동사가 문자열(증명한 상수 포함)이면 requests·
  httpx·aiohttp는 대문자로 바꾼 값이 계약 동사일 때만 동사다(`"post"` → POST). urllib는 바꾸지 않고 보낸다 — `method="put"`은
  서버에 `put`으로 가서 동사가 아니다(methodDynamic). 그 밖은 `methodDynamic: true`.
- urllib 동사: `Request(method=리터럴)`이면 그 값, 없으면 보낼 데이터(`urlopen`의 `data`가 None이 아니면 그것, 아니면
  `Request`의 `data`)가 없거나 None 리터럴이면 GET, None이 아닌 리터럴이면 POST, 모르면 methodDynamic. `urlopen` 인자가
  매개변수처럼 무엇인지 모르는 값이면 Request일 수 있어 methodDynamic이다.
- URL 인자: 동사 함수·메서드는 첫 위치 인자 또는 `url=`, `request`·`stream`은 두 번째 위치 인자 또는 `url=`. 그 자리 앞에
  `*args`가 있거나 인자가 없으면(`**kwargs`) dynamic이다.

### 클라이언트 객체 추적

수신자 식이 클라이언트 객체임을 다음으로만 증명한다(나머지 수신자의 `get`·`post` 호출은 사실로 내지 않는다).

- 생성자 호출 자체(`httpx.Client(base_url=…).get(…)`), 한 범위에서 한 번만 묶인 지역 이름(대입·`with … as`·`async with … as` —
  세 라이브러리 모두 `__enter__`·`__aenter__`가 자기 자신을 돌려준다), 모듈 수준 변수(다른 모듈의 import 포함).
- 인스턴스 필드·클래스 속성(`self.client`): 수신자 클래스의 프로젝트 MRO와(하위 클래스일 수 있는 수신자면) 프로젝트 하위 클래스의
  클래스 본문 대입과 메서드 안 `self.X = …` 대입이 **모두** 같은 라이브러리의 클라이언트여야 한다. base가 대입마다 다르면 base를
  모른다. 수신자 밖에서 그 이름을 대입하면(타입 모르는 객체의 `x.client = …`, 관련 클래스 객체의 대입, 모듈 함수의 `self`
  매개변수나 메서드 안 중첩 함수의 `self.x = …`) 증명하지 않는다.
- 타입 주석이 클라이언트 클래스인 매개변수·클래스 본문 필드(`session: requests.Session`, `http: httpx.Client | None`,
  `Optional[...]`, 문자열 주석): 주입된 클라이언트라 base를 모른다.
- 클라이언트 클래스를 상속한 프로젝트 클래스(`class Api(httpx.Client)`): 수신자 MRO의 첫 외부 기반이 클라이언트 클래스면 그
  라이브러리이고 base는 모른다(하위 클래스 `__init__`이 정할 수 있다).
- httpx `Client.base_url`은 setter가 있으므로, 프로젝트 어디서든 `self`·`cls`가 아닌 수신자의 `base_url` 속성을 대입하면 httpx
  리터럴 base를 모두 모르는 base로 내린다(보수적).

## 조립 규칙

`compose.*` 규칙(query 꼬리, suffix, 세그먼트 보간, 정규화, strip, mask)은 계약과 같다. 파이썬 쪽 입력은 다음과 같다.

- **보간**: f-string `{expr}`(형식 지정자·`!r`·`!a`가 붙으면 값 자리), `+` 연결, `%` 서식(`%s`·`%d`·`%(name)s`, `%%`는 리터럴),
  `str.format`의 `{}`·`{0}`·`{name}`(`{{`·`}}`는 리터럴, 속성·첨자 접근 필드는 식 전체를 값으로 본다). 값 자리는 세그먼트 전체를
  채울 때만 `{}`이고 부분 세그먼트·한 세그먼트에 둘 이상이면 dynamic과 `channelPrefix`다.
- **상수 치환**: 모듈 최상위에서 한 번만 묶이고 다시 묶이지 않는 이름만 치환한다(계약 `compose` 5). 조건문·반복문·`try`·`with`
  안 대입, 누적 대입, 같은 이름의 import·정의·`del`, 함수·클래스 정의 시점에 평가되는 장식자·기본값·주석·기반 클래스 안의
  바다코끼리 대입, 그 모듈 어느 범위의 `global` 선언, 프로젝트 어디서든 모듈 속성 대입
  (`constants.API_ROOT = …`, 리터럴 `setattr`)이 있으면 상수가 아니다. 쓰는 모듈에서도 그 이름이 한 번만 묶여야 한다(import 뒤
  다시 대입하면 아니다). 대문자 이름 관례는 증명으로 쓰지 않는다. 상수의 값은 문자열 리터럴이거나 증명한 상수들의 f-string·`+`
  조립이다.
- **클래스 속성·필드**: `self.BASE_URL`·`cls.BASE_URL`·`ApiClient.BASE_URL`·`self.base`는 위 클라이언트 객체 추적과 같은
  범위의 모든 대입(클래스 본문, 메서드 안 `self.X = …`)이 같은 리터럴(또는 증명한 상수)일 때만 그 값이다. 누적 대입·풀기·반복
  변수·`with … as self.x` 대입이 하나라도 있으면 값을 모른다.
- **지역 이름**: 한 범위에서 한 번만 대입된 이름(바깥 함수 포함, 클래스 범위는 건너뜀)은 초기식을 따라간다. 매개변수는 값이다.
  끝 지역 변수의 초기식이 query 꼬리임을 증명하면(`"?" + urlencode(q)`, `f"?page={n}" if n else ""`) query 꼬리로 뗀다.
- **전체 URL**: scheme은 http·https만 받는다. host까지 리터럴이면 host 뒤 경로가 root이고 소문자 `authority`(userinfo 제거,
  포트 보존)를 싣는다. host 자리에 값이 있으면(`f"https://{host}/v1"`) host 뒤 경로가 base 앵커다. `//`로 시작하는 리터럴과
  다른 scheme은 dynamic이다.
- **앞 값(base 식)**: URL이 값으로 시작하고(`base_url + "/status"`, `f"{self.base}/x"`) 그 값이 상수가 아니면, 뒤 경로가 `/`로
  시작할 때 base 앵커 꼬리다(점 세그먼트가 있으면 모르는 base로 올라갈 수 있어 dynamic). `/` 없이 이어지면 dynamic과
  `ambiguous-base-join:`이다.
- base 없는 클라이언트(requests, httpx·aiohttp 최상위 함수, base 없는 `Client()`·`ClientSession()`, urllib)의 상대 URL은 라이브러리가
  요청을 거부하므로 dynamic이다.

### base 결합과 결합 방식 이름

결합 방식 이름은 url-compose 벡터의 `join`과 같다(HTTP-WRAPPERS 확정 이름).

| 결합 방식 | 적용 | 경로 `/x` | 경로 `x` |
|---|---|---|---|
| `httpx-base-url` | `httpx.Client`·`AsyncClient(base_url=)` | base 경로(끝 `/` 보장) + `x`, 점 세그먼트 제거, 리터럴 base면 root | 같음 |
| `aiohttp-base-url` | `aiohttp.ClientSession(base_url=)` | RFC 3986(base 경로를 바꾼다), root | base 경로의 마지막 `/`까지 + `x`(3.11+) |
| `rfc3986` | (규칙 벡터 실행용, pythograph는 aiohttp 결합 안에서 쓴다) | root | base 디렉터리 뒤 |

- **httpx**(0.28.1): `base_url` 설정이 경로 끝 `/`를 보장하고(`_enforce_trailing_slash`) `_merge_url`이 상대 URL 경로의 앞 `/`를
  **모두** 떼어 붙인 뒤 다시 파싱하며 점 세그먼트를 지운다. 그래서 `…/api`·`…/api/` 어느 쪽이든 `users`·`/users`는
  `/api/users`이고 `..`는 base 경로 밖으로 나간다. 가운데 `//`는 줄이지 않는다. `//`로 시작하는 경로는 host로 파싱되어(요청은
  base 경로로 간다 — 오라클 확인) 주장하지 않는다(dynamic + `ambiguous-base-join:`). base를 모르면 앞 `/`를 뗀 경로가 base 앵커
  꼬리이고(`./`는 지운다) `..`·빈 경로는 dynamic + `ambiguous-base-join:`이다. 절대 URL(scheme과 host)은 base를 쓰지 않는다.
- **aiohttp**: `_build_url`은 상대 URL을 yarl `URL.join`(RFC 3986)으로 합친다. `/`로 시작하는 경로는 base 경로를 버린다
  (`…/api/` + `/popular` → `/popular` — 오라클 확인). 버전 제약은 sdist 대조로 확인했다.
  - 3.8.0~3.10.x: base는 경로 없는 origin이어야 하고(assert) 요청 경로가 `/`로 시작해야 한다(assert).
  - 3.11.0부터 경로 있는 base(끝 `/` 필수, 아니면 `ValueError`)와 `/` 없는 상대 경로를 받는다.
  - 3.12.0부터 base 세션의 절대 URL 요청이 base를 건너뛴다(3.11은 assert로 실패).
  - pythograph는 선언 파일(`uv.lock`·`poetry.lock`·`pdm.lock`·`Pipfile.lock`·`requirements*.txt`·`pyproject.toml`·`setup.cfg`·
    `Pipfile`)의 aiohttp 선언이 **모두** 하한을 가질 때 그중 가장 낮은 하한을 증명한 버전으로 쓴다(`==3.14.3`, `>=3.11,<4`,
    `~=3.12.1`, poetry `^3.11`). 3.11 이상을 증명하지 못하면 경로 있는 base와 `/` 없는 상대 경로를, 3.12 이상을 증명하지 못하면
    base 세션의 절대 URL 요청을 주장하지 않는다(dynamic, 앞의 둘은 `ambiguous-base-join:`). 경로 있는 base가 `/`로 끝나지 않으면
    세션 생성이 실패하므로 그 세션의 모든 요청이 dynamic이다. `//`로 시작하는 경로는 yarl이 절대 URL로 읽어 주장하지 않는다.
- **`urllib.parse.urljoin`**: 벡터가 없는 결합이라 결과를 주장하지 않는다(계약 "그 밖"). 두 번째 인자가 절대 URL이면 그대로
  요청 URL(`compose.strip`), `/`로 시작하는 리터럴이면 base 앵커 꼬리(점 세그먼트는 urljoin이 지운다), 그 밖은 dynamic +
  `ambiguous-base-join:`이다. 모의 서버 오라클은 CPython 3.12 `urljoin`이 RFC 3986처럼 동작함을 확인했다(`…/app/` + `status` →
  `/app/status`, + `/root` → `/root`) — 벡터가 생기면 root로 올릴 수 있다.

## 래퍼 선언 (`--wrappers`, `http-wrappers` v1)

`"language": "python"` 항목만 적용한다. 다른 언어 항목도 스키마는 검사한다(모르는 필드·잘못된 값은 64 — 조용히 무시하면 낡은
선언이 호출 0건을 내어 "호출 없음"으로 읽힌다).

- `owner`(pythograph id와 같은 모양): 메서드·생성자는 소유 클래스 id(`shopclient/api.py#Gateway`), 모듈 수준 함수는 모듈 파일
  경로(`shopclient/api.py`). 래퍼 id는 메서드·생성자면 `owner + "." + name`, 모듈 함수면 `owner + "#" + name`이다.
- `kind: "constructor"`의 `name`은 `__init__`이고 호출은 `Endpoint(...)`다(데이터 클래스처럼 `__init__`을 정의하지 않아도 된다.
  프로젝트 기반 클래스의 `__init__`을 물려받은 하위 클래스 호출도 그 선언이다).
- 호출 판정은 그래프와 같은 이름·수신자 해석이다(`send(...)`, `Gateway().call(...)`, 프로젝트 클래스 주석이 달린 매개변수의
  메서드처럼 수신자를 증명한 호출만 — 주석 없는 인스턴스 필드 수신자 `self.api.call(...)`은 잇지 못한다).
- 인자: `label`은 키워드 인자 이름, `index`는 호출에 쓴 위치 인자 자리(0부터, 메서드 수신자·생성자 `self`는 세지 않는다).
  파이썬은 위치 인자가 키워드 인자보다 앞이라, 그 자리가 키워드면 쓰지 않는다는 계약 규칙과 같다. 그 자리 앞에 `*args`가 있거나,
  찾지 못했는데 `**kwargs`가 있으면 어느 값인지 몰라 동사는 methodDynamic, 경로는 dynamic이다.
- `methodEnum`: 동사 인자가 이름·속성(`HttpMethod.GET`, `GET`)이면 마지막 이름을 case로 찾는다. 없으면 그 값이 증명한 상수
  문자열일 때 리터럴로 본다. 문자열 리터럴은 계약 동사와 정확히 같을 때만 동사다(`wrapper.method`).
- 경로: 전체 URL 리터럴은 host 뒤 경로가 root, `/`로 시작하는 경로는 선언의 `pathAnchor`, 앞 값 뒤 `/` 경로는 base다. `/` 없이
  시작하는 상대 경로는 래퍼 내부 결합을 몰라 dynamic + `ambiguous-base-join:`이다.
- `service`를 적으면 그 래퍼 호출 사실에 싣는다. 선언된 래퍼 본문(과 그 안의 중첩 정의)의 dynamic 호출은 래퍼 호출 사실이
  대신하므로 내지 않는다.
- 선언의 대상이 스캔한 소스에 없거나 호출이 0건이면 `http-wrapper-unresolved:`다. 선언되지 않은 함수가 자기 매개변수를 URL
  전체·앞머리로 쓰거나 `/`로 끝나지 않는 리터럴 뒤에 그대로 붙이면(`BASE + path`) `http-wrapper-undeclared:`로 센다(세그먼트
  하나를 채우는 매개변수는 싱크가 아니다).

## limitation

| 접두사 | 측 | 뜻 |
|---|---|---|
| `route-call-coverage:` | 호출 측 | 모델링하지 않은 요청 API(urllib3·`http.client`·`build_opener`·`requests.Request`/`PreparedRequest`·`httpx.Request`·tornado·pycurl·treq·grequests·requests-futures, 클라이언트의 `send`·`build_request`·`prepare_request`·`ws_connect`) 호출 수, `urlopen`에 곧바로 넘기지 않은 `urllib.request.Request` 수, 클라이언트 타입을 증명하지 못한 수신자에 URL 리터럴(`/`·`http://`·`https://`로 시작)을 넘긴 `get`류 호출 수, 스캔 공백(순회 상한·심볼릭 링크·UTF-8이 아닌 파일 이름·파싱 실패) |
| `ambiguous-base-join:` | 호출 측 | 결합 결과를 증명하지 못한 호출 수(모르는 base 뒤 `/` 없는 경로·`..`·빈 경로, `//` 경로, urljoin 상대 경로, 증명하지 못한 aiohttp 버전) |
| `http-wrapper-undeclared:` | 호출 측 | 매개변수를 URL로 흘려보내는 선언되지 않은 함수 수 |
| `http-wrapper-unresolved:` | 호출 측 | 대상이 없거나 호출이 0건인 파이썬 래퍼 선언(`wrappers[n]`) |

호출 측 한계의 요청 상한을 증명할 수 없어 `limitationScopes`는 내지 않는다(스코프 없는 한계는 문서 전체 효과).

## 알려진 한계

- 수신자를 증명하지 못한 클라이언트(생성자 매개변수로 받아 주석 없이 필드에 넣은 클라이언트, 팩토리 함수 반환값, 컨테이너 원소)는
  사실로 내지 않고 URL 리터럴 호출만 `route-call-coverage:`로 센다.
- 계산된 이름의 `setattr`·`__dict__` 쓰기로 바꾼 클래스 속성·모듈 상수는 따라가지 않는다.
- `__init__.py` 없는 이름공간 패키지 안의 모듈 import는 이름 해석기가 외부 모듈로 본다(서버·그래프와 같은 해석기).
- requests-toolbelt `BaseUrlSession`, 사용자 base 헬퍼, `requests.Session.request`를 감싼 하위 클래스의 재정의 동작은 모델링하지
  않는다(래퍼 선언으로 알려 준다).
- `--route-call-hosts`(계약의 선택 옵션)와 `baseRef`는 아직 싣지 않는다.

## 모의 서버 오라클

`fixtures/client/shop-client`는 각 라이브러리·규칙을 부르는 합성 클라이언트다(함수 하나 = 요청 하나).
`experiments/client_oracle/run_oracle.py`를 fixture `requirements.txt`를 설치한 스크래치 가상 환경에서 실행하면 127.0.0.1 임시
포트에 `http.server`를 띄우고 `socket.getaddrinfo`·`socket.socket.connect`를 바꿔 `*.example.test:80` 연결을 그 포트로 보낸 뒤(외부
네트워크 요청 없음) 시나리오마다 fixture 함수를 불러 (동사, 경로, Host)를 기록하고, pythograph 사실과 비교한다(root는 전체 경로,
base는 세그먼트 경계 꼬리, `{}`는 비어 있지 않은 세그먼트, authority는 Host). 서버는 끝나면 닫는다. 기본 CI는 네트워크를 쓰지
않는다 — `tests/test_client_oracle.py`가 커밋된 기록(`experiments/client_oracle/recorded.json`)을 지금 사실과 비교한다.

2026-09-30 기록(Python 3.12.13, requests 2.34.2, httpx 0.28.1, aiohttp 3.14.3, urllib3 2.8.0, yarl 1.25.1): **35개 시나리오, 일치
31 · dynamic 4 · 불일치 0**.

| 시나리오 | 규칙 | 기록 요청 | route-call 사실 | 결과 |
|---|---|---|---|---|
| `list_products` | f-string + 모듈 상수 | `GET /v1/products` | `GET /v1/products` root | 일치 |
| `product_detail` | 세그먼트 보간, `params` | `GET /v1/products/42?expand=reviews` | `GET /v1/products/{}` root | 일치 |
| `search_products` | query 꼬리 리터럴 | `GET /v1/search?q=lamp` | `GET /v1/search` root | 일치 |
| `create_order` | `request("post", …)` 대문자화 | `POST /orders` | `POST /orders` root | 일치 |
| `health` | 점 세그먼트(urllib3가 지움) | `HEAD /v1/health` | `HEAD /v1/health` root | 일치 |
| `page` | 증명한 query 꼬리 지역 변수 | `GET /v1/pages?page=3` | `GET /v1/pages` root | 일치 |
| `token_status` | 고엔트로피 마스킹 | `GET /v1/tokens/a1b2c3d4e5f6a7b8c9d0/t1` | `GET /v1/tokens/{}/{}` root | 일치 |
| `warehouses` | httpx 최상위 | `GET /warehouses` | `GET /warehouses` root | 일치 |
| `export_rows` | `httpx.stream` | `GET /export` | `GET /export` root | 일치 |
| `CatalogClient.categories` | 클래스 속성 base | `GET /v1/categories` | `GET /v1/categories` root | 일치 |
| `CatalogClient.remove_category` | 클래스 속성 + `+` | `DELETE /v1/categories/lamps` | `DELETE /v1/categories/{}` root | 일치 |
| `ReviewClient.reviews` | `__init__` 필드 base | `GET /v2/products/7/reviews` | `GET /v2/products/{}/reviews` root | 일치 |
| `update_profile` | `with requests.Session()` | `PUT /v1/profile` | `PUT /v1/profile` root | 일치 |
| `stock` | **httpx** base 끝 `/` 없음 + `/stock` | `GET /api/stock` | `GET /api/stock` root | 일치 |
| `patch_item` | httpx base 끝 `/` + 상대 경로 | `PATCH /api/items/sku-9` | `PATCH /api/items/{}` root | 일치 |
| `ShippingClient.quote` | httpx 필드 클라이언트, `/quotes`가 base 경로 뒤 | `POST /v3/quotes` | `POST /v3/quotes` root | 일치 |
| `ShippingClient.label` | httpx `request("GET", …)` | `GET /v3/labels/s-1` | `GET /v3/labels/{}` root | 일치 |
| `rates` | httpx `AsyncClient` | `GET /v3/rates` | `GET /v3/rates` root | 일치 |
| `recommendations` | **aiohttp** 상대 경로 | `GET /api/recommendations` | `GET /api/recommendations` root | 일치 |
| `popular` | aiohttp `/popular`는 base 경로를 바꾼다 | `GET /popular` | `GET /popular` root | 일치 |
| `send_feedback` | base 없는 세션 전체 URL, `request("post", …)` | `POST /api/items/5/feedback` | `POST /api/items/{}/feedback` root | 일치 |
| `legacy_status` | `urljoin` 상대 경로(주장하지 않음) | `GET /app/status` | dynamic | dynamic |
| `legacy_root` | `urljoin` 절대 경로 → base 꼬리 | `GET /root` | `GET /root` base | 일치 |
| `legacy_submit` | `Request(method="PUT")` | `PUT /app/forms/submit` | `PUT /app/forms/submit` root | 일치 |
| `legacy_ping` | `urlopen(data=…)` → POST | `POST /app/ping` | `POST /app/ping` root | 일치 |
| `gateway_order_create` | 함수 래퍼, enum 동사 | `POST /gw/orders` | `POST /orders` base | 일치 |
| `gateway_order` | 함수 래퍼, 리터럴 동사 | `GET /gw/orders/11` | `GET /orders/{}` base | 일치 |
| `cart_delete` | 생성자 래퍼(데이터 클래스), 키워드 동사 | `DELETE /gw/carts/4` | `DELETE /carts/{}` base | 일치 |
| `cart_list` | 생성자 래퍼 기본 동사 | `GET /gw/carts` | `GET /carts` base | 일치 |
| `gateway_items` | 메서드 래퍼 기본 동사 | `GET /gw/items` | `GET /items` base | 일치 |
| `gateway_item_remove` | 메서드 래퍼 enum 키워드 동사 | `DELETE /gw/items/8` | `DELETE /items/{}` base | 일치 |
| `download` | 부분 세그먼트 보간 | `GET /v1/files/report.json` | dynamic, prefix `/v1/files/` | dynamic |
| `proxy_get` | 매개변수 URL(래퍼 싱크) | `GET /v1/proxied` | dynamic | dynamic |
| `service_status` | 모르는 base + `/status` | `GET /v9/status` | `GET /status` base | 일치 |
| `glued` | 모르는 base + `items` | `GET /v1/items` | dynamic(`ambiguous-base-join:`) | dynamic |

탐색 단계에서 같은 하네스로 확인한 경계 동작(fixture에는 없음): httpx `//x`는 host `x`로 파싱되어 base 경로(`/api/`)로 간다,
aiohttp `//x`는 assert로 실패한다, aiohttp base `…/api`(끝 `/` 없음)는 세션 생성이 `ValueError`다, requests는 가운데 `//`를
보존하고 unreserved 퍼센트 인코딩을 풀며 hex를 대문자로 쓴다, urllib는 점 세그먼트와 소문자 동사를 그대로 보낸다.

## 공유 적합성 벡터

`conformance/`에 isthmus `3a45450`의 네 벡터를 벤더링하고 `conformance.lock`에 커밋과 sha256을 적었다. `tests/test_conformance.py`가
`appliesTo`에 `producer` 또는 `producer:pythograph`가 있는 사례를 모두 실행한다: 135건(url-compose 57건 — `compose.interpolation`·
`query-tail`·`suffix`·`normalize`·`strip`·`mask`·`base-join`(`rfc3986`·`slash-join`·`dio-concat`·`httpx-base-url`·
`aiohttp-base-url`)·`wrapper.method`, `wrapper.location`은 실제 스캐너로). `versionRange`가 있는 aiohttp 사례는 그 하한을 증명한
프로젝트로 실행하고, 없는 사례는 확인한 최신(3.14)으로 실행한다. `scope.dynamic-*`(`dynamicScope`)은 pythograph가 아직 내지 않아
건너뛴다.

벡터 관찰: `base-join/aiohttp-unknown-base-relative`(`users` → base `/users`)에는 `versionRange`가 없지만 HTTP-WRAPPERS 규칙상 `/` 없는
상대 경로는 aiohttp 3.11 이상에서만 요청된다(3.8~3.10은 assert). pythograph는 버전을 증명하지 못하면 이 경우를 dynamic으로 낸다 —
벡터에 `"versionRange": ">=3.11"`을 더하는 것이 규칙과 맞다.

## 종단 확인: 파이썬 클라이언트 × Django 서버

`experiments/client_e2e/run_trace.py`는 합성 파이썬 클라이언트(`fixtures/e2e/py-client`: httpx `base_url` 결합 주문 API, requests 상품·
결제 호출, 그것을 부르는 화면 함수)에 `routes --role client`와 역방향 `impact`(root = route-call usr 전체)를 실행하고, Phase 6
서버 기록(`experiments/e2e/recorded/`의 shop-api route·persistence·sql 문서와 forward·reverse·DB 순회)과 함께 workspace trace
context(link `python->api`, host `api.example.com` 귀속)를 써서 isthmus `trace`(main `3a45450` 빌드)를 실행한다.
`tests/test_client_e2e.py`가 기록을 오프라인으로 다시 확인한다.

| route | 파이썬 호출부(exact) | 영향 심볼(깊이 1) | 서버 핸들러 | relation-use 테이블 |
|---|---|---|---|---|
| `GET /api/orders/{}/` | `shopcli/api.py#OrdersApi.fetch` | `shopcli/screens.py#OrderScreen.show` | `store/views.py#OrderViewSet.retrieve` | `main.store_order` |
| `POST /api/orders/{}/cancel/` | `shopcli/api.py#OrdersApi.cancel` | `shopcli/screens.py#OrderScreen.tap_cancel` | `store/views.py#OrderViewSet.cancel` | `main.store_order`, `main.store_auditentry` |
| `GET /api/products/` | `shopcli/api.py#list_products` | `shopcli/screens.py#catalog_refresh` | `store/views.py#ProductListView.get` | `main.store_product` |
| `POST /api/checkout/` | `shopcli/api.py#checkout` | `shopcli/screens.py#pay` | `store/views.py#CheckoutView.post` | `main.store_order`, `main.store_orderline` |

trace 요약: calls 4, chains 4, clientSymbols 4, handlers 4, relationUses 18, databaseVertices 17(evidence 전부 `direct`). gap 두 개는
서버 forward 순회의 `reach-possibly-incomplete`(Phase 6 기록과 같다)다.
