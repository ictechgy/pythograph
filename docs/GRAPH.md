# 호출 그래프와 순회 (`pythograph graph`·`reach`·`impact`)

pythograph가 파이썬 프로젝트의 호출 그래프를 만들고 isthmus [`language-traversal` v1](https://github.com/ictechgy/isthmus/blob/main/docs/LANGUAGE-TRAVERSAL.md)
순회 문서를 내는 규칙이다. 분석은 표준 라이브러리 `ast`로만 한다. 분석 대상 프로젝트와 프레임워크를 import·실행하지
않는다. 정점 id는 `routes`·`schema`가 싣는 `symbol.usr`와 같은 문자열이라 isthmus `trace`가 문자열 일치로 잇는다.

```sh
pythograph graph  --project <root> [--include-tests] [--revision <id>] [--generated-at <ts>] [--format json]
pythograph reach  --project <root> [--dispatch direct|bound|candidates] [--max-depth <n>] [--max-reached <n>]
                  [--roots-from <file|->] [--include-tests] [--revision <id>] [--generated-at <ts>] [--] <id>...
pythograph impact (reach와 같은 옵션)
```

- `graph`: pythograph 자체 형식 `pythograph-graph` v1 스냅샷(isthmus 입력이 아니다). 정점(`id`·`kind`·`location`,
  미해석 호출이 있으면 모드별 `unresolvedCalls`와 이유별 `unresolvedReasons`), 간선(`from`·`to`·`kinds`·`evidence`),
  `statistics`, `direct` 모드 `limitations`, `graphRevision`, `revision`.
- `reach`: root가 기대는 정점(`direction: "dependencies"`), `impact`: root에 기대는 정점(`"dependents"`).
- 종료 코드: 0 성공, 2 읽을 수 없는 프로젝트·출력 16Mi 문자 초과(부분 문서 없음), 64 사용법 오류(표준 출력 비어
  있음) 또는 root-not-found(문서를 쓴 뒤 64). 1은 예약이다.

## 정점

| 종류 | id | 뜻 |
|---|---|---|
| `module` | `<경로>#<module>` | 모듈 수준 문장(정의 본문 밖). 위치는 경로만 싣는다(줄·열을 1로 채우지 않는다) |
| `function`·`method`·`class` | `<경로>#<어휘적 점 경로>` | 모든 함수·메서드·클래스와 중첩 정의(`create_app.index`, `Model.Meta`). 람다·컴프리헨션은 감싼 정의의 일부다 |
| `inherited-method` | `<등록 클래스 id>.<멤버>` | 클래스가 정의하지 않고 물려받은 멤버. 위치는 그 클래스다 |

- 같은 점 경로를 두 번 정의하면(조건부 정의, property setter) 한 정점이다.
- 상속 멤버 정점은 두 곳에서 만든다. (1) 뷰 클래스의 핸들러 이름(아래 [프레임워크 디스패치](#프레임워크-디스패치)) — routes가
  클래스 핸들러를 URL에 등록한 클래스 기준 id(`catalog/views.py#BrandView.get`)로 내므로 그 id가 정점이어야 한다. (2) 정확한
  수신자(생성자로 만든 인스턴스)의 물려받은 멤버 호출(`Plain("p").describe()` → `Plain.describe`).
- 테스트 소스(routes와 같은 규칙)는 기본으로 정점이 아니다. `--include-tests`로 더한다. 마이그레이션은 정점이다(relation-use
  사실이 없으므로 체인에 영향이 없다).

## 간선

모든 간선은 `evidence`(`direct`·`bound`·`candidate`)를 싣는다. 같은 두 정점 사이에는 등급마다 간선 하나이고, 더 강한 간선이
종류를 모두 싣는 약한 간선은 뺀다(어느 모드의 순회도 바뀌지 않는다, tsograph와 같은 규칙).

| 종류 | 뜻 |
|---|---|
| `call` | 호출(함수·메서드·`super()`·리터럴 `getattr(x, "m")()`), property·프레임워크 property 읽기, property setter 쓰기 |
| `new` | 클래스 호출. `__init__`을 MRO로 찾아 프로젝트 정의면 그 정의(정확한 수신자가 물려받았으면 상속 멤버 정점), 프레임워크 구현이면 상속 멤버 정점, 없거나 외부면 클래스 정점 |
| `callback` | 인자로 넘긴 함수·클래스·메서드 값(`sorted(items, key=slugify)`, `router.register("x", ViewSet)`) |
| `reference` | 그 밖의 값 참조(대입·반환·컨테이너 원소). 장식자 식도 감싼 범위의 참조다 |
| `decorator` | 장식한 정의 → 프로젝트 장식자(팩토리 호출이면 팩토리). 장식한 함수를 부르면 장식자 래퍼를 거친다 |
| `attribute` | 프로젝트 클래스 본문 속성 읽기(`self.queryset`, 프레임워크가 읽는 `serializer_class`) → 그 클래스 정점. 클래스 본문의 relation-use 사실(`queryset = Order.objects.all()`)은 클래스 id를 싣는다 |
| `inherit` | 클래스 → 프로젝트 기반 클래스, 상속 멤버 정점 → 물려받은 정의 |
| `dispatch` | 뷰 핸들러 → 프레임워크 디스패치 경로가 부르는 프로젝트 훅 |
| `framework` | 프레임워크가 구현한 상속 멤버 → 그 구현이 부르는 프로젝트 훅 |

- 모듈 정점을 import하는 쪽에서 잇지 않는다(import 시점 부작용은 모듈 정점에 남는다).
- 클래스 이름을 속성 수신자로만 쓴 경우(`Order.objects.filter(...)`)는 참조 간선이 아니다. 그 질의의 relation-use는 감싼
  함수에 이미 실리고, 참조로 이으면 모델 선언 전체(모든 컬럼·외래 키 대상 모델)로 넓어지기 때문이다. 모델을 값으로 넘기거나
  (`get_object_or_404(Order, …)`) 생성하면(`Order(...)`) 클래스 정점에 닿는다.

## 이름과 수신자 해석

- 이름은 파이썬 LEGB 규칙이다: 함수 지역 → 바깥 함수(클래스 범위는 건너뛴다) → 모듈 전역 → 내장 이름. 클래스 본문은 자기
  본문 이름을 먼저 본다. `global`·`nonlocal`을 따른다.
- 모듈 전역: 정의, import(절대·상대·`from x import y as z`, 조건문 안 포함), 모듈 속성 접근(`models.Base`), `__init__` 재수출,
  단순 대입 값(`orders = OrderService()`는 정확한 인스턴스), 프로젝트 모듈의 `from x import *`. 풀지 못한 프로젝트 import
  (`from .missing import x`)는 외부로 보지 않고 `unresolved-import`다.
- 지역 이름은 흐름을 따지지 않는다. 한 범위에서 두 번 이상 묶이거나 반복 변수·`with … as`·예외·풀기·match 캡처로 묶인 이름은
  모르는 값(`local-value`)이다.
- 수신자: 메서드 첫 매개변수(`self`는 그 클래스 또는 하위 클래스, `classmethod`의 `cls`), 프로젝트 클래스 주석
  (`Optional[X]`·`X | None`·`Annotated[X, …]`·문자열 주석 포함), 생성자 결과(정확한 클래스), 프로젝트 클래스 반환 주석,
  `type(self)`, 인자 없는 `super()`.
- MRO는 파이썬 `type.mro()`와 같은 C3 선형화다. 프로젝트 클래스, 알려진 프레임워크 클래스(`framework_table`의 선형화), 표에 없는
  외부 클래스(멤버를 모른다), 해석하지 못한 기반(프로젝트 클래스일 수도 있다)을 항목으로 둔다. 병합이 실패하거나 순환하면 왼쪽
  우선 깊이 우선으로 근사하고 `mro-approximated:`로 센다. 멤버 조회는 표에 없는 외부 클래스를 건너뛰되 기억하고, 뒤에서
  프로젝트·프레임워크 정의를 찾으면 그것을 쓴다(외부 클래스가 같은 이름을 가리는 경우는 근사다).
- 정확한 수신자(`Plain("p")`, 모듈 수준 인스턴스, 프레임워크가 만든 뷰 인스턴스)의 메서드 호출은 그 클래스의 멤버 정점(정의 또는
  상속 멤버)으로 간다. 상속 멤버 정점은 물려받은 정의로 `inherit` 간선을 두고, 그 본문의 `self.X` 사용을 정확한 수신자 MRO로 다시
  풀어 잇는다(다이아몬드의 `Diamond.describe`는 `Left.label`과 `Right.suffix`에 닿는다).
- `self`·`cls`·주석처럼 하위 클래스일 수 있는 수신자는 정적으로 찾은 정의에 `direct` 간선, 프로젝트 하위 클래스가 재정의한 같은
  이름의 메서드에 `candidate` 간선을 둔다. 수신자 클래스에는 없고 하위 클래스에만 있는 메서드도 `candidate`다. `super()` 호출은
  정의한 클래스 다음 MRO 항목이라 후보가 없다.

### 외부로 확정하는 호출과 이름 필터

외부 이름으로 푼 호출(표준 라이브러리·설치 패키지·프레임워크 멤버, 외부 기반 클래스에서 물려받은 멤버 `Order.objects`)은 외부다.
타입을 모르는 수신자(매개변수, 외부 호출 결과)의 메서드 호출 `x.m()`은, 프로젝트의 어떤 클래스·모듈도 `m`을 정의하지 않고 어떤
속성 쓰기(`obj.m = …`, 리터럴 `setattr`)도 `m`을 대입하지 않으면 프로젝트 코드에 닿을 수 없으므로 외부로 확정한다. 그 이름을
프로젝트가 쓰면 `untyped-receiver`로 센다. 프로젝트 클래스가 `__getattr__`·`__getattribute__`를 정의하면 필터를 끈다. 계산된 이름의
`setattr`로 붙인 함수는 필터가 보지 못하므로 `dynamic-attribute-writes:`로 센다. 외부 코드가 프로젝트로 되돌아오는 호출(시그널,
콜백을 받은 라이브러리)은 따라가지 않는다 — 콜백을 넘긴 곳의 `callback` 간선만 있다.

## 미해석 호출 (`unresolvedCalls`)

정점 **자신의** 호출 지점(람다·컴프리헨션 포함, 중첩 정의 본문 제외) 중 대상을 잇지 못한 수다. 모드별로 센다: `direct`·`bound`는
재정의 후보가 있는 호출(`overridden-method`)과 하위 클래스 전용 메서드 호출(`subclass-method`)을 포함하고, `candidates`는 그 호출을
후보 간선으로 이었으므로 뺀다. 외부 호출은 미해석이 아니다. 이유:

| 이유 | 뜻 |
|---|---|
| `parameter` | 매개변수(콜백)나 람다 매개변수를 호출 |
| `local-value` | 값을 모르는 지역 이름을 호출(재대입·반복 변수·`with`·풀기·match 캡처) |
| `untyped-receiver` | 타입 모르는 수신자의 메서드 호출인데 그 이름을 프로젝트가 쓴다 |
| `dynamic-attribute` | 프로젝트 클래스에 없는 속성(인스턴스 속성 `self.repo.save()`, 함수 속성) 호출 |
| `dynamic-callee` | 피호출 식이 호출 결과·첨자·람다 등이다(`handlers[k]()`, `factory()()`) |
| `getattr` | 계산된 이름의 `getattr(x, name)()` |
| `unresolved-name`·`unresolved-import`·`star-import` | 정의를 찾지 못한 이름·프로젝트 import·외부 `*` import 뒤 이름 |
| `unknown-base` | 해석하지 못한 기반 클래스를 지나 멤버를 찾지 못했다 |
| `excluded-source` | 테스트 소스(정점이 아님)의 정의를 호출 |
| `overridden-method`·`subclass-method` | 위 재정의 후보(`direct`·`bound`만) |
| `framework-callback` | 프레임워크가 클래스 속성 값으로 만들어 부르는 프로젝트 코드(아래) |
| `framework-implementation` | 핸들러 이름이 프레임워크 표 밖의 구현이다 |

`unresolvedCalls`가 0인 정점도 완전성의 증거가 아니다. 연산자 메서드(`__add__`)·문맥 관리자(`__enter__`)·반복자·서술자
암묵 호출은 호출 지점으로 세지 않는다.

## 프레임워크 디스패치

`experiments/graph/dump_framework_hooks.py`가 Django 5.2.17, djangorestframework 3.18.1, Flask 3.1.3 설치본 소스를 `ast`로 읽어
`src/pythograph/graph/framework_table.py`를 만든다. 알려진 기반 클래스(Django generic view·auth 믹스인, DRF `APIView`·generic
view·viewset·mixin·serializer, Flask `View`·`MethodView`)마다 C3 선형화, 메서드가 `self`/`cls`로 **호출하는** 이름(`cls(...)`는
`__init__`), 호출하지 않고 읽는 이름, `super().X` 이름, property 여부, 클래스 속성을 담는다. 런타임은 표만 읽는다.

- **핸들러 정점**: 모듈 수준 뷰 클래스(routes와 같은 `analyze_class`)의 HTTP 핸들러 이름(`get`·`post`·…), DRF viewset 표준
  action(`list`·`create`·`retrieve`·`update`·`partial_update`·`destroy`)과 `@action`·`mapping` 메서드, Flask `View`의
  `dispatch_request`마다, 클래스가 정의했으면 그 정의, 물려받았으면 상속 멤버 정점이다. drf-shop·blog-app·persistence fixture의
  모든 routes·schema usr가 정점임을 테스트로 확인한다.
- **프레임워크 구현 따라가기**: 이름을 수신자 클래스 MRO로 푼다. 프로젝트 메서드면 훅, 프로젝트 클래스 속성이면 속성 간선,
  프레임워크 메서드면 그 메서드가 호출하는 이름으로 계속 따라간다(`super().X`는 정의한 클래스 다음부터 찾아 믹스인 뒤의 프로젝트
  클래스에 닿는다). 호출하지 않고 읽기만 하는 메서드(`View.setup`의 `self.head = self.get`)는 훅이 아니다. 수신자는 프레임워크가
  `as_view()`로 만든 인스턴스라 정확한 클래스다.
- **디스패치 경로**: `as_view()`(Django `setup`·`dispatch`, DRF `initial`·권한·스로틀·콘텐츠 협상, 인스턴스 생성 `__init__`, Flask
  `dispatch_request`)가 부르는 훅을 그 클래스의 모든 핸들러 정점에서 `dispatch` 간선으로 잇는다.
- **프레임워크 구현 멤버**: `ModelViewSet.retrieve` → `get_object` → 프로젝트 `get_queryset`, `ListAPIView.get` → `get_queryset`,
  `ModelSerializer.save` → 프로젝트 `create`처럼 상속 멤버 정점에서 `framework` 간선으로 잇는다. `super()`로 닿은 프레임워크 구현이
  부르는 훅은 호출한 정점에서 직접 잇는다.
- **프레임워크 콜백**: 프레임워크가 클래스 속성 값으로 인스턴스를 만들어 부르는 프로젝트 코드(값이 프로젝트 클래스·함수이거나 그
  목록인 속성 — `serializer_class`, `permission_classes`, `pagination_class` 등)는 따라가지 않는다. 그 속성을 읽는 경로마다
  `framework-callback` 미해석 호출로 센다(속성 간선으로 클래스 정점에는 닿는다). 시그널·미들웨어·설정에 이름으로 적은 클래스는
  모델링하지 않는다(`framework-dispatch:` 한계).
- Django ORM 매니저 호출(`Order.objects.filter`)은 외부 호출이다. 그 relation-use 사실의 usr가 감싼 함수라서 순회가 그 함수에
  닿으면 trace가 사실을 잇는다.

## 근거 등급과 디스패치 모드

| 모드 | 따라가는 간선 | 문서 |
|---|---|---|
| `direct`(기본) | `direct` | 재정의 후보가 있는 호출은 미해석으로 센다 |
| `bound` | `direct`·`bound` | pythograph는 아직 `bound` 간선을 만들지 않으므로 `direct` 그래프와 같다(`bound-dispatch:` 한계) |
| `candidates` | 모든 간선 | 하위 클래스 재정의 후보를 `candidate`로 싣고 그 호출을 미해석에서 뺀다 |

**`bound`를 구현하지 않은 이유.** tsograph의 `bound`는 "인터페이스 자리로 들어오는 관찰된 흐름이 모두 알려진 프로젝트 구현"이라는
전체 프로그램 값 흐름 증명이 필요하다. 파이썬에서는 `as_view(**initkwargs)`가 `setattr`로 임의 속성을 쓰고, DI 컨테이너·목이
흐름을 넣으며, 동적 속성 대입이 흔해서 그 보장을 지키려면 속성 쓰기·매개변수 흐름 전체를 추적해야 한다. 이 PR은 증명하지 못한
흐름을 `bound`로 부풀리지 않고 `direct` + `candidate`만 낸다. 정확한 수신자로 확정한 호출(생성자·모듈 수준 인스턴스·프레임워크가 만든
뷰 인스턴스)은 이미 `direct`다.

## 순회 문서 (`language-traversal` v1)

- `dispatch`를 항상 싣는다. 계약상 모든 도달 정점의 `evidence`를 분류하고(항상 싣는다) 미해석 호출이 1개 이상인 모든 root·도달
  정점에 `unresolvedCalls`(1~1,000,000, 넘으면 상한과 `unresolved-calls-capped:`)를 싣는다는 선언이다. 다른 root에서 닿은 root는
  `roots[]`와 `reached[]`에 같은 값을 싣는다.
- `roots[]`: 요청 순서(위치 인자 다음 `--roots-from`, 중복은 처음 나온 것만). `reached[]`는 (depth, usr) 순(UTF-16 코드 단위).
  `depth`는 가장 가까운 root까지 거리, `via`는 가장 가까운 root(같으면 작은 인덱스)에서 depth-1인 선행 정점 중 가장 작은 id, `roots`는
  닿는 모든 root 인덱스(자기 제외, 64개 초과면 작은 64개와 `rootsTruncated`), `relationships`는 via 간선 종류(모드가 허용하는 등급을
  합친다). 모두 모드가 허용하는 전체 그래프 기준이다.
- `evidence`는 **root별 하한**이다: 정점에 깊이 상한 안에서 닿는 모든 root 각각의 가장 강한 등급 중 가장 약한 것. 약한 간선 출발점에
  닿지 못하는 root는 비교에서 빼고, 나머지는 root당 비트 하나로 정확히 비교한다. 메모리가 64 MiB를 넘을 것 같으면 약하게 적을 수는
  있어도 부풀리지 않는 근사로 바꾸고 `evidence-approximated:`를 싣는다.
- 한 번의 단계 동기 다중 출발 패스다. 정점이 자기 자신이 아닌 더 작은 인덱스 root를 65개 이상 가지면 더 큰 인덱스 root는 거기서
  전파를 멈춘다. `tests/test_traversal_oracle.py`가 무작위 그래프 120개(UTF-16 정렬이 코드 포인트와 다른 id 포함)와 root 80개
  그래프에서 root별 너비 우선 오라클과 도달·via·depth·roots·relationships·등급·잘림을 비교한다.
- 상한: `--max-depth` 1~128(기본 128), `--max-reached` 1~100,000(기본 100,000). 잘리면 `truncated: true`와 `truncationReasons`
  (`depth`·`max-reached`). root는 10,000개까지.
- **정점이 아닌 root**(root-not-found): 원문을 `id`에 두고 `symbol`·`unresolvedCalls`를 생략하며 `truncated: true`,
  `truncationReasons`의 `root-not-found`, `root-not-found:` 한계를 싣는다. 나머지 root로 순회한 문서를 쓰고 64로 끝난다(표준 오류에는
  수만 쓴다). 제어 문자·빈 id, 10,000개 초과, 읽지 못하는 `--roots-from`은 문서 없는 64다.
- `--roots-from <file|->`: JSON 문자열 배열 또는 bridge-facts 문서(사실의 `symbol.usr`), `-`는 표준 입력, 16 MiB까지.
- `revision`: `--revision`, 없으면 프로젝트 루트 git의 HEAD — 작업 트리가 깨끗할 때만(추적하지 않는 파일도 더럽다). git은 프로젝트
  루트에 `.git`이 있을 때만 `GIT_OPTIONAL_LOCKS=0`·`core.fsmonitor=false`로 60초 제한을 두고 실행한다(isthmus capture와 같은 설정).
  분석 대상 코드는 실행하지 않는다.
- `graphRevision`: 정점 id·종류·모드별 미해석 수와 간선(등급 포함)의 SHA-256(`sha256:`), 위치 제외. 같은 그래프의 `graph`·`reach`·
  `impact`는 모드와 무관하게 같은 값이다.
- `limitations`: 모드의 그래프 한계(`unresolved-calls:`, `overridden-methods:`/`candidate-dispatch:`, `bound-dispatch:`,
  `external-calls:`, `framework-dispatch:`, `dynamic-attribute-writes:`, `mro-approximated:`, `unparsed-files:`, `scan-incomplete:`)와
  문서 한계(`root-not-found:`, `evidence-approximated:`, `unresolved-calls-capped:`).
- 같은 입력이면 같은 바이트다(`--generated-at`으로 시각 고정).

## 검증

- `tests/test_graph_resolution.py`: 해석 fixture(`fixtures/graph/resolution`)의 기대 간선·미해석 이유, golden 스냅샷, 기존
  fixture의 모든 routes·schema usr가 정점인지.
- `tests/test_graph_cases.py`: 디스패치 경로 훅(믹스인 뒤 `super().dispatch`), DRF 콜백 속성·직렬화기 훅, Flask `MethodView`, C3 실패·
  순환 계층, 이름 필터, 지역 이름 규칙, 주석 변형, 한계 문구.
- `tests/test_traversal_oracle.py`: 단일 패스 = root별 오라클(무작위 그래프, 전파 중단, 결정성, 근사가 부풀리지 않음).
- `tests/test_e2e_trace.py`와 `experiments/e2e/`: Phase 6 종료 조건(아래).
- 공개 샘플(HackSoftware/Django-Styleguide-Example, encode/django-rest-framework, pallets/flask, 스크래치 복제)에서 충돌 없이
  스냅샷을 만든다.

## Phase 6 종료 조건: Django 백엔드 × iOS/Android 체인

`experiments/e2e/run_trace.py`가 합성 서버(`fixtures/e2e/shop-api`, Django 5.2 + DRF 3.18)와 합성 클라이언트(`experiments/e2e/clients/`,
SwiftPM iOS와 Kotlin/JVM Retrofit)를 실제 도구로 잇는다: pythograph `routes`·`schema`·`reach`·`impact`, schemagraph(`shop-api.sql` —
Django가 만드는 SQLite DDL과 합성 뷰 `store_open_orders`)의 `facts`·`impact --format language-traversal`, cartograph·kartograph의
route-call과 역방향 순회(`record_clients.py`가 기록), isthmus `trace`(workspace: 서버·iOS·Android member와 host link). 입력과 출력은
`experiments/e2e/recorded/`에 있고(절대 경로 없음, project는 `/e2e/...` 합성 경로), `tests/test_e2e_trace.py`가 지금의 pythograph
출력이 기록과 같은지와 세 질문의 기대 경로를 오프라인으로 확인한다.

| 질문 | 선택 | 기대 경로(기록한 trace와 일치) |
|---|---|---|
| (a) API → DB 테이블 + DB 의존자 | `GET /api/orders/{}/` | `OrderViewSet.retrieve`(상속 멤버) → `framework` → `get_queryset` → `selectors.orders_for_customer` → `store_order`(·`customer_id`) → 의존자 `store_open_orders`(뷰)·`store_orderline`(FK). `POST …/cancel/` → `OrderService.cancel` → `store_order` 그리고 `audit.record` → `store_auditentry`. `GET /api/products/` → `ListAPIView.get` 훅 `get_queryset` → `active_products` → `store_product` |
| (b) API → 클라이언트 호출부 → 영향 심볼 | 같은 route | iOS `OrdersAPI.fetchOrder` → `OrderDetailViewModel.load`(1) → `OrderDetailScreen.appear`(2), `cancelOrder` → `cancel`(1) → `tapCancel`(2), 상품 목록은 iOS `ProductsAPI.list` → `CatalogViewModel.refresh`와 Android `ProductsClient.list` → `CatalogViewModel.refresh` |
| (c) 테이블 → API → 클라이언트 | `store_auditentry`, `store_product` | `audit.record` ← `OrderService.cancel` ← `OrderViewSet.cancel` → `POST /api/orders/{}/cancel/` → iOS `cancelOrder` → `cancel` → `tapCancel`; `active_products` ← `get_queryset` ← `ProductListView.get` → `GET /api/products/` → iOS·Android 목록 호출 |

기대 gap(개수까지 고정): route 선택은 `reach-possibly-incomplete` 2개(`retrieve`·`ProductListView.get`의 `serializer_class`
`framework-callback`)와 `unattributed-calls-omitted` 3개(Android Retrofit 호출은 base 앵커라 host link에 귀속되지 않는다),
relation 선택은 `non-http-entry` 3개(모델 선언 사실의 usr인 모델 클래스는 핸들러에서 닿지 않는다)와 `unattributed-calls-omitted`
2개. kartograph는 Retrofit 인터페이스 메서드에 JVM usr를 붙이지 못해(`missing-route-usrs:`) Android 주문 호출의 영향 심볼은 없다 —
그래서 합성 Android 클라이언트에 usr가 붙는 `java.net.URL` 호출(상품 목록)을 두었다.
