# Changelog

이 프로젝트의 주요 변경 사항을 기록한다.

## [Unreleased]

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
