# HANDOFF

세션을 이어받을 때 먼저 읽는 진행 상태 문서다. 작업 규칙의 정본은 [AGENTS.md](AGENTS.md)이고, 여기 적힌 상태는
실제 Git 상태·코드·검증 결과와 대조한 뒤 쓴다. 자기 분석 기록은 [DOGFOOD.md](DOGFOOD.md)에 있다.

## 현재 상태 (2026-11-09)

- 발행본은 **0.3.1**이다. PyPI 게시(2026-11-03), 태그 `v0.3.1`, `pyproject.toml` 버전이 일치한다.
- isthmus `compatibility.json`의 pythograph 핀은 아직 **0.2.1**이다. isthmus가 게시본을 검증한 뒤 바꾸는 절차다
  (RELEASING 10단계). pythograph 저장소에서 고치지 않는다.
- 열린 작업은 PR #31(브랜치 `ccr-37904670-hxdjsx`)이다. 2026-11-03 계열 9개 도구 검토의 pythograph 항목과, 그 PR의 독립
  리뷰 지적을 반영했다. 변경은 모두 CHANGELOG `[Unreleased]`에 있고 발행 전이다.

## PR #31에서 처리한 것

- **그래프 정확성**
  - 같은 점 경로의 재정의(property setter, `@overload` 뒤 구현, `singledispatch` 등록, 조건부 정의)의 모든 본문을 훑는다.
  - 모듈 수준 재묶음(정의·import 섞임)은 `local-value`로 센다. 다른 모듈의 import·모듈 속성 접근과 재수출 사슬에도 같은
    판정을 쓴다.
  - 일부 정의에서만 묶인 지역 이름은 다른 정의로 새지 않게 모름으로 센다. `@overload`는 `typing`·`typing_extensions` 출처일
    때만 스텁으로 뺀다.
  - 모두 외부인 대체 묶음(`ListView`/`object`)은 평소 해석을 써서 프레임워크 정점과 routes usr를 지킨다.
  - 모듈 루트 밖 암묵 루트 import는 `unresolved-import`로 센다. Celery 큐 호출은 `callback` 간선이고 태스크·시그널 수신자는
    `framework-dispatch:`로 센다.
- **순회 범위**: 이름으로 건너뛰던 `build`·`dist`·`env`·`venv`가 패키지면 순회한다. 소스를 담은 채 건너뛴 디렉터리는 한계로
  센다(한 단계 아래 namespace 패키지 포함, setuptools 산출물과 파이썬 없는 산출물 디렉터리 제외).
- **change-impact**
  - 비분석 파일은 바이트 대신 git blob id로 비교한다. `pyvenv.cfg`는 graph와 같은 판정을 위해 캡처한다.
  - 커밋 캡처는 `cat-file --batch` 한 번으로 읽는다. 정수 옵션 문법을 맞추고 `--generated-at`을 추가했다.
  - `input.files`는 이제 분석 파일 수만 세고, `input.contentDigest` 값은 0.3.1과 다르다.
- **routes 한계**: 지원하지 않는 서버 프레임워크 import를 센다(프로젝트 안 같은 이름 패키지는 제외). Flask 호스트 옵션을
  센다(끈 리터럴은 제외).
- **CI·문서**
  - CI 매트릭스는 3.10–3.14 전부, 릴리스는 `twine check --strict`와 sdist 설치를 검사한다. CI에 자기 분석을 추가했다.
  - 보안 문서에 change-impact의 git 사용을 반영했다. isthmus 적합성 벡터는 `c76e105`로 다시 벤더링했다.

## PR #31에서 제외한 것 (이유)

| 항목 | 이유 |
|---|---|
| `--source-root` 옵션 | 모든 명령의 입력 정체성(스냅샷·change-impact 캡처)에 닿는 새 입력 표면이다. 이번에는 `unresolved-import` 계수로 조용한 누락만 막았다. |
| FastAPI·Starlette 라우트 지원 | 새 프레임워크 규칙과 오라클이 필요하다. 이번에는 감지와 한계만 넣었다. |
| Windows CI 잡 | 테스트가 심볼릭 링크·권한 같은 POSIX 전제를 쓰는지 먼저 확인해야 한다. |
| 외부 타입 주석으로 `untyped-receiver` 줄이기 | 자기 분석 미해석의 대부분이지만 별도 설계가 필요하다. |
| 재정의 본문별 이름 해석 | 호출 수집(`collect`·`flow`·`exposure`)에 본문 맥락을 넘겨야 해 범위가 크다. 지금은 일부 정의에서만 묶인 이름을 모름으로 센다. |
| isthmus 호환 핀 갱신 | isthmus 저장소의 일이고, 게시본 검증 뒤에만 바꾼다. |
| dead·cycles·rules·MCP | 생산자 전용 범위라 해당하지 않는다. |

## 머지 뒤 다음 할 일

1. 0.3.2 또는 0.4.0 릴리스. change-impact `input.files`의 의미가 바뀌었으므로 버전 판단에 반영하고 RELEASING 절차를 따른다.
2. isthmus에 게시본 검증과 `compatibility.json` 핀 갱신을 요청한다.
3. 위 제외 항목 중 사용자가 고른 것.

## 주의

- **리뷰 방식**: AGENTS가 요구하는 GLM 리뷰(`packet-ask`)는 PR #31 작업 환경에 없어 실행하지 못했다. 대신 독립 Claude 리뷰를
  받았고, 지적을 코드·fixture로 확인한 뒤 반영했다. 다음 PR에서 GLM을 쓸 수 있으면 원래 절차로 돌아간다.
- **발행된 CHANGELOG 절은 고치지 않는다.** PR #31 작업 중 0.3.1 절의 첫 줄을 번역해 다시 쓴 적이 있어 원문으로 되돌렸다.
- **로컬 git 버전**: `change-impact`는 git 2.45 이상의 `--no-lazy-fetch`를 쓴다. 낮은 git에서는 partial clone 관련 경로가
  거부로 끝난다.
- **pytest 요약 줄**: 저장소 설정에 `-q`가 있어, `-q`를 또 주면 통과 개수 요약이 사라진다. 개수를 볼 때는 `-q` 없이 돌린다.
- **자기 분석**: `routes --role server --project .`는 Django·Flask fixture가 함께 있어 `--framework` 없이 exit 64로 끝난다.
  예상된 사용법 오류다.
