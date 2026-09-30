# 도그푸딩 결과 (Phase 6)

공개 파이썬 웹 앱 네 개에 `routes`·`schema`·`graph`·`reach`·`impact`를 실행하고, 프레임워크 자신의 해석 결과(오라클)와
schemagraph·isthmus 조인으로 사실을 검증했다. 대상은 스크래치 디렉터리에만 얕게 복제했고 저장소에는 코드를 넣지 않았다.
여기에는 지표와 문제의 일반화한 설명만 적는다(재현용 회귀 테스트는 모두 합성 코드다).

## 대상과 방법

| 앱 | 구성 | 라이선스 | 커밋 | 파이썬 파일 |
|---|---|---|---|---|
| HackSoftware/Django-Styleguide-Example | Django 5 + DRF | MIT | `a70ef43` | 190 |
| babybuddy/babybuddy | Django 5 + DRF, django-filter | BSD-2-Clause | `3bcc2a5` | 234 |
| miguelgrinberg/microblog | Flask 3 + Flask-SQLAlchemy 3(SQLAlchemy 2 `Mapped`), 앱 팩토리·블루프린트 | MIT | `a975ef6` | 34 |
| netbox-community/netbox | Django 6.x + DRF, 대형(규모·충돌 점검용) | Apache-2.0 | `9bcfd73` | 1,282 |

- **route 오라클**: `experiments/oracle/`의 덤프 스크립트를 스크래치로 복사해 앱별 설정 모듈·앱 팩토리만 바꿨다. 각 앱의
  의존성을 스크래치 가상 환경에 설치하고 URLconf·`url_map`만 import했다(서버를 띄우지 않았다). 정밀도는 정적 route-decl
  사실 중 표본 경로가 같은 핸들러로 풀리고 선언한 method가 허용된 비율, 재현율은 resolver가 순회한 끝점 항목 중 검증된
  사실이 덮은 비율이다.
- **persistence 조인**: 앱의 마이그레이션을 스크래치 SQLite에 적용(Django `migrate`, Flask-Migrate `upgrade`)하고
  schemagraph `703a21f`로 카탈로그·sql 사실을 만든 뒤 isthmus `f9dcd1d` `check --pairs`로 pythograph `schema` 문서와
  조인했다. netbox는 PostgreSQL 전용 마이그레이션(ICU 정렬·`ltree` 확장)이라 스크래치 임베디드 PostgreSQL에서 끝까지
  적용하지 못해 조인을 생략했다(불완전한 카탈로그는 거짓 `relation-use-without-decl`을 만든다).
- **순회**: `graph` 스냅샷의 direct 간선으로 route 핸들러(route-decl `symbol.usr`)마다 도달 집합을 구해 정적 relation-use를
  가진 심볼에 닿는 비율을 셌다. isthmus `trace`는 서버 member(routes·schema·sql 문서, pythograph `reach`·`impact`,
  schemagraph `impact --format language-traversal`)와 선택 경로마다 route-call 하나를 가진 합성 클라이언트로 실행했다.
- 실행 시간·최대 RSS는 macOS `/usr/bin/time -l`(Apple Silicon, Python 3.13)이다.

## 결과 요약

수정 전은 `main` `5db1a6e`, 수정 후는 이 브랜치다. babybuddy는 설정 모듈을 환경 파일로 정해서 두 경우 모두
`--settings babybuddy.settings.base`로 실행했다.

### routes (오라클 대비)

| 앱 | 수정 전 정밀도 | 수정 후 정밀도 | 수정 전 재현율 | 수정 후 재현율 |
|---|---|---|---|---|
| Django-Styleguide-Example | 21/21 | 21/21 | 21/22 | 21/22 |
| babybuddy | 192/214 (89.7%) | 205/221 (92.8%) | 192/332 | 205/332 |
| microblog | 18/35 (51.4%) | 35/35 (100%) | 18/35 | 35/35 |
| netbox | 0/0 (사실 102건 모두 dynamic) | 100/104 (96.2%) | 0/3,614 | 99/3,614 |

- 검증되지 않은 사실은 모두 계약상 의도한 근사다: 알 수 없는 제3자 기반 클래스(django-filter `FilterView`,
  drf-spectacular·GraphQL 뷰)를 상속한 클래스 뷰의 `ANY`와 그 템플릿 스코프의 `route-coverage:` 한계(거짓 match 쪽, 거짓
  error 아님). 이를 빼면 네 앱 모두 정밀도 100%다. 수정 전 microblog의 17건은 블루프린트 접두사를 찾지 못해
  `pathAnchor: "base"`로 낸 사실이었다(거짓은 아니지만 오라클 경로와 맞지 않았다).
- 남은 재현율 손실은 정적으로 증명할 수 없는 구성이며 모두 스코프 있는 한계로 보고된다: `urls` 속성이나 `__init__`에서
  라우트 표를 바꾸는 DRF 라우터 하위 클래스(babybuddy API 108개 항목, netbox API 1,247개), 데코레이터 레지스트리에서 URL
  목록을 만드는 함수(netbox 283개 목록), 프로젝트 밖 URL 모듈(`dbsettings`, `django.conf.urls.i18n`).

### persistence (schemagraph·isthmus 조인)

| 앱 | 사실(수정 전 → 후) | dynamic | relation-use 조인 | column-use 조인 | isthmus error |
|---|---|---|---|---|---|
| Django-Styleguide-Example | 163 → 163 | 18 → 18 | 48/48 | 97/97 | 0 |
| babybuddy | 416 → 604 | 112 → 19 | 103/105 → 219/219 | 193/199 → 366/366 | 0 (warning `relation-use-without-decl` 2 → 0) |
| microblog | 130 → 130 | 2 → 2 | 45/45 | 83/83 | 0 |
| netbox (조인 생략) | 7,770 → 9,430 | 3,821 (49%) → 431 (4.6%) | — | — | — |

남은 `relation-decl-without-use-unverified` warning은 코드가 쓰지 않는 테이블(프레임워크·확장 앱 테이블, 마이그레이션 표)이다.

### 순회와 trace

| 앱 | 핸들러 → relation-use 도달(수정 전 → 후) | 그래프 정점/간선(수정 후) | 미해석 호출 상위 이유(수정 후) |
|---|---|---|---|
| Django-Styleguide-Example | 14/21 → 14/21 | 541 / 331 | untyped-receiver 32, dynamic-callee 30, framework-callback 18 |
| babybuddy | 176/192 → 170/194 | 1,741 / 2,677 | framework-callback 469, untyped-receiver 202 |
| microblog | 20/26 → 20/26 | 165 / 134 | untyped-receiver 14 |
| netbox | 36/64 → 74/100 | 25,538 / 64,420 | untyped-receiver 9,860, unknown-base 696 (unresolved-name 1,973 → 0) |

- babybuddy의 도달 수가 줄어든 것은 폼 클래스를 모델로 오인해 생긴 거짓 relation-use가 사라졌기 때문이다.
- isthmus `trace`(앱마다 경로 3개): 세 앱 모두 3개 경로가 핸들러·relation-use·DB 정점으로 이어졌다(DSE 49 uses → DB
  정점 48, babybuddy 146 → 92, microblog 58 → 42, 근거 등급 모두 `direct`). 남은 gap은 합성 클라이언트에 역방향 분석이
  없어서(`analysis-missing`), 미해석 호출 뒤(`reach-possibly-incomplete`), 핸들러 194개를 한 번에 root로 준 문서의 root
  귀속 상한 64(`analysis-truncated`)다.

### 실행 시간과 메모리 (수정 후)

| 앱 | routes | schema | graph | reach/impact |
|---|---|---|---|---|
| Django-Styleguide-Example | 0.09 s / 33 MiB | 0.16 s / 34 MiB | 0.16 s / 36 MiB | — |
| babybuddy | 0.21 s / 58 MiB | 0.45 s / 47 MiB | 0.59 s / 71 MiB | — |
| microblog | 0.08 s / 32 MiB | 0.13 s / 32 MiB | 0.12 s / 33 MiB | — |
| netbox | 1.3 s / 294 MiB | 5.8 s / 286 MiB | 출력 상한 초과(종료 코드 2) | 6.3 s / 약 480 MiB |

netbox `graph` 스냅샷은 별 import를 따라가 정점·간선이 늘면서 16 Mi 문자 출력 상한을 넘는다(부분 문서 없이 2). isthmus가
받는 `reach`·`impact` 문서는 정상이며, 오류 문구가 이를 안내하도록 고쳤다. 크래시(종료 코드 1 또는 예기치 못한 2)는 없었다.
이 상한은 뒤의 [재측정](#재측정-bound-근거-등급과-대형-그래프-스냅샷)에서 스냅샷만 256 Mi 문자로 떼어 해결했다(netbox `graph` 종료 코드 0).

## 찾아서 고친 문제

모두 합성 최소 재현으로 회귀 테스트를 두었다(`tests/`).

1. **패키지 `__init__`을 거치는 `from x import *`를 따라가지 않음**(netbox): 이름 해석기가 별 import를 전혀 따라가지 않고,
   그래프는 한 단계만 따라갔다. 모델·뷰 이름이 풀리지 않아 매니저 사용 3,526건이 dynamic, 그래프 `unresolved-name` 1,973건이었다.
   이제 프로젝트 모듈의 `*`를 거듭 따라가고 리터럴 `__all__`·밑줄 규칙을 지키며, 외부·`__all__` 미확정 모듈의 `*`는 가림으로 본다.
2. **`django.forms` 필드를 모델 필드로 오인**(babybuddy, netbox): 모르는 기반 클래스 + `forms.CharField` 선언을 가진 ModelForm이
   모델이 되어 없는 테이블(`<앱>_<폼 이름>`)의 relation-use를 냈다(isthmus warning 2건, netbox 앱 라벨 미해석 215건의 주원인).
3. **모듈 속성으로 닿은 모델(`from app import models` 뒤 `models.Book.objects`)을 풀지 않음**(babybuddy): 사실이 없거나 dynamic이었다.
   맨 앞 이름을 함수(바깥 함수 포함)에서 다시 묶으면 풀지 않는다.
4. **조건부 `INSTALLED_APPS.remove(...)`가 앱 목록 전체를 불완전하게 만듦**(netbox): 조건부 제거는 상위 집합으로 무시하고 무조건
   제거는 적용한다.
5. **맨 앞 include 문자열이 설정값이면 모든 경로가 dynamic**(netbox `path(settings.BASE_PATH, include(...))`): 그 조각을 떼고
   `pathAnchor: "base"`와 `unresolved-route-prefix:`로 낸다(계약의 확정하지 못한 접두사 규칙).
6. **`django.contrib.auth.views`와 `Base…View`·`DeletionMixin`이 알려진 클래스 표에 없음**(babybuddy, netbox): 로그인·비밀번호 뷰가
   `ANY`와 usr 없는 사실로, 믹스인 조합 뷰는 GET을 잃었다. Django 5.2.17 설치본에서 조사한 핸들러로 표를 넓혔고
   `LogoutView`의 `http_method_names`(post만)를 반영한다. 호출 그래프의 프레임워크 표도 같은 클래스로 다시 만들어, route usr인
   상속 핸들러(`…#Logout.post`)가 그래프 정점이 되게 했다(전에는 `reach`가 root-not-found로 잘렸다).
7. **앱 팩토리 안의 import를 따라가지 않음**(microblog): `create_app()` 안의 `from app.auth import bp as auth_bp` 뒤
   `register_blueprint(auth_bp, url_prefix=...)`를 풀지 못해 블루프린트 경로 17건이 base 앵커였다. 이제 함수 지역 import를 따라가고,
   import 뒤 대입·반복 변수로 다시 묶은 이름(안쪽 함수 포함)은 풀지 않는다.
8. **설정 모듈을 진입 파일이 이름으로 적지 않을 때 안내 없이 사실 0건**(babybuddy): 한계 문구에 `--settings <module>` 안내를 더했다.
9. **`graph` 출력 상한 오류가 "isthmus rejects"라고 안내**: 스냅샷은 isthmus 입력이 아니므로 상한과 `reach`/`impact` 사용을 안내한다.

GLM 리뷰(신뢰하지 않는 입력)로 받은 지적 중 재현한 네 건(Flask 지역 묶음 가림, 바깥 함수 클로저 가림, 함수 안 지역
`__all__`이 내보내기를 흐리는 문제, 무조건 `remove`)을 같은 변경에서 고쳤다.

## 남은 한계와 다음 후보

- DRF 라우터 하위 클래스: `urls`를 재정의하거나 `__init__`에서 `routes` 매핑을 바꾸는 라우터는 풀지 않는다(스코프 있는 한계).
  `get_api_root_view`만 바꾸는 하위 클래스는 모델링할 여지가 있다.
- 레지스트리 기반 URL 생성(데코레이터가 채운 레지스트리를 URLconf가 읽는 구조)은 정적으로 증명할 수 없다.
- 알 수 없는 제3자 기반 클래스의 클래스 뷰는 `ANY` 근사다(django-filter·drf-spectacular 등의 표를 두면 줄일 수 있다).
- 미해석 호출은 타입 모르는 수신자(`untyped-receiver`)가 대부분이다 — `bound`로 다시 쟀다(아래 재측정).
- ~~대형 프로젝트의 `graph` 스냅샷 크기(16 Mi 문자 상한)~~ — 스냅샷 상한을 256 Mi 문자로 떼어 해결(아래 재측정). 1초를 넘는
  스캔 시간은 남았다.

## 재측정: `bound` 근거 등급과 대형 그래프 스냅샷

같은 커밋의 같은 네 앱을 스크래치에 다시 복제해(`babybuddy` `3bcc2a5`, `microblog` `a975ef6`, `Django-Styleguide-Example`
`a70ef43`, `netbox` `9bcfd73`) 파싱만 했다(설치·실행하지 않았다). 수정 전은 `main` `50d079f`, 수정 후는 `bound` 브랜치다. 도달은
route-decl 핸들러 usr마다 모드가 허용하는 간선으로 너비 우선 탐색해 정적 relation-use를 가진 심볼에 닿는 핸들러 수다. 실행
시간·최대 RSS는 같은 기계(Apple Silicon, Python 3.13)의 `/usr/bin/time -l`이다.

### 핸들러 → relation-use 도달과 `bound`

| 앱 | 도달 direct(전 → 후) | 도달 bound | 도달 candidates | `bound` 후보 | 이은 호출 | 열린 이유 상위 | 프로그램 판정 |
|---|---|---|---|---|---|---|---|
| Django-Styleguide-Example | 14/21 → 14/21 | 14/21 | 14/21 | 30 | 0 | framework-base 10, method-parameter 8, call-result 6, referenced 4 | application(테스트가 아닌 `factories.py`가 테스트 패키지를 import → 테스트까지 프로그램) |
| babybuddy | 170/194 → 170/194 | 170/194 | 170/194 | 210 | 0 | call-result 117, class-attribute 23, variadic-parameter 15, loop-variable 12, method-parameter 12 | application |
| microblog | 20/26 → 20/26 | 20/26 | 20/26 | 12 | 0 | call-result 12 | application |
| netbox | 74/100 → 74/100 | 74/100 | 74/100 | 10,002 | 0 | call-result 3,039, class-attribute 2,691, method-parameter 1,085, dynamic-expression 787, code-execution 444, scan-incomplete 372 | library(`pyproject.toml` `[project]`) + 불완전한 스캔(4 MiB 넘는 데이터 모듈 1개) |

- **이은 호출은 네 앱 모두 0건이다.** 후보의 대부분은 프레임워크가 만든 객체(`request`·`validated_data`·`options`·ORM 결과·
  매니저)의 메서드 호출이라 흐름이 외부 호출 결과(`call-result`)·클래스 객체 속성(`class-attribute`)·메서드 매개변수·
  `**kwargs`에서 끝난다. 프로젝트 클래스를 생성자로 주입하는 코드(DI)가 이 앱들에는 거의 없다. netbox는 라이브러리 판정
  (플러그인이 공개 API를 import한다)·불완전한 스캔·스크립트 실행 기능의 `exec`로 모듈 수준 함수·생성자가 모두 열린다.
- 그래서 **기본 모드는 `direct`를 유지한다**(`docs/GRAPH.md`). `bound`는 도달·등급을 바꾸지 않고 문서의 `dispatch` 선언과
  한계 문구만 바꾼다. 합성 DI 코드에서 `bound`가 잇는 것은 `tests/test_graph_bound*.py`가 확인한다.
- 대상을 모르는 계산된 이름의 쓰기(모델링하지 않은 틈 `unknownTargetWrites`): DSE 1, babybuddy 2, microblog 0, netbox 30 —
  모두 `setattr(instance, field, value)` 모양의 모델 갱신이다. 막으면 `bound`가 전부 사라져 tsograph처럼 가정으로 두고 수를 싣는다.

### 미해석 호출(수정 전 → 후)

| 앱 | direct | bound | candidates | 간선 |
|---|---|---|---|---|
| Django-Styleguide-Example | 86 → 86 | 86 | 86 | 331 → 331 |
| babybuddy | 684 → 684 | 684 | 684 | 2,677 → 2,670 |
| microblog | 14 → 14 | 14 | 14 | 134 → 134 |
| netbox | 11,159 → 11,167 | 11,167 | 11,085 | 64,451 → 64,446 |

- direct 등급 수정(다시 쓰는 모듈 전역·클래스 본문 속성은 정확한 값이 아니다)의 영향: babybuddy는 모델 클래스 본문 속성
  `settings = NapSettings(...)`가 다른 곳의 `x.settings = …` 쓰기 때문에 정확한 값이 아니게 되어 `attribute` 간선 7개가 빠졌다
  (이름 기준이라 보수적이다, 핸들러 도달은 같다). netbox는 같은 이유로 16개 호출이 `dynamic-attribute`로 옮겨 direct 미해석이 8개
  늘었다(`dynamic-callee`에서 넘어온 것 포함).

### 실행 시간과 메모리(수정 후)

| 앱 | graph | reach | reach `--dispatch bound` | impact |
|---|---|---|---|---|
| Django-Styleguide-Example | 0.25 s / 41 MiB | 0.25 s / 42 MiB | 0.25 s / 42 MiB | 0.24 s / 41 MiB |
| babybuddy | 0.56 s / 73 MiB | 0.56 s / 71 MiB | 0.55 s / 71 MiB | 0.55 s / 72 MiB |
| microblog | 0.14 s / 34 MiB | 0.14 s / 34 MiB | 0.14 s / 34 MiB | 0.14 s / 34 MiB |
| netbox | **7.9 s / 513 MiB, 종료 코드 0** | 7.8 s / 487 MiB | 7.8 s / 489 MiB | 7.9 s / 490 MiB |

- **netbox `graph`가 끝까지 나온다**: 스냅샷은 21,250,374 문자(16 Mi = 16,777,216 문자 초과)로 전에는 종료 코드 2였다. 스냅샷
  상한을 256 Mi 문자로 떼었고(스냅샷은 isthmus 입력이 아니다) 직렬화가 더하는 메모리는 약 26 MiB(같은 그래프의 `reach` 대비)다.
- `bound` 분석 비용: 같은 기계에서 netbox `reach`가 7.1–7.4 s / 458 MiB(수정 전)에서 7.8–8.0 s / 485 MiB로 늘었다. 모듈 AST 훑기를
  하나로 합치고 `nonlocal`·테스트 소스 import 판정을 캐시해 처음 구현(9.4 s)보다 줄였다. DSE는 테스트 패키지까지 프로그램으로
  훑어 0.16 s → 0.25 s다.
