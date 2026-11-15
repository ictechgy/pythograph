# HANDOFF

세션을 이어받을 때 먼저 읽는 진행 상태 문서다. 작업 규칙의 정본은 [AGENTS.md](AGENTS.md)이고, 여기 적힌 상태는
실제 Git 상태·코드·검증 결과와 대조한 뒤 쓴다. 자기 분석 기록은 [DOGFOOD.md](DOGFOOD.md)에 있다.

## 현재 상태 (2026-11-15)

- 발행본은 **0.3.1**이다. PyPI 게시(2026-11-03), 태그 `v0.3.1`, `pyproject.toml` 버전이 일치한다.
- isthmus `compatibility.json`의 pythograph 핀은 아직 **0.2.1**이다. isthmus가 게시본을 검증한 뒤 바꾸는 절차다
  (RELEASING 10단계). pythograph 저장소에서 고치지 않는다.
- PR #31은 2026-11-09에 `main`으로 스쿼시 머지됐다(`1ea33c4`). 2026-11-03 계열 9개 도구 검토의 pythograph 항목과,
  그 PR의 독립 리뷰 지적을 반영한 것이다. 변경은 모두 CHANGELOG `[Unreleased]`에 있고 아직 발행하지 않았다.
- `main` `1ea33c4`의 CI 체크 7개가 모두 성공했다(2026-11-10 확인).
- PR #33(브랜치 `ccr-37904670-hxdjsx`, `main` `aeb2b74` 기준)이 Windows CI 잡과 그 감사에서 나온 수정이다. `ae58402`의 CI가
  첫 `verify-windows`(3.13)까지 모두 통과했다. 독립 리뷰(판정 "수정 후 병합 가능")의 지적 S1–S4·N1–N7을 반영한 `9cee4e0`이
  PR head로 올라갔고, 처음 돈 `verify-windows (3.10)`이 "Windows fatal exception: stack overflow"로 죽었다(아래 "3.10·3.11
  스택 넘침"). `121ac23`·`c76c251`이 고쳐 Windows 3.10은 통과했지만 macOS 3.10이 페이지 배수가 아닌 스택 크기로 실패했고,
  `3a9ac2e`(스택을 1 MiB 배수로)가 고쳐 **`3a9ac2e`의 CI는 macOS·Windows 3.10을 포함해 모두 통과했다**(push됨). 그 뒤 재리뷰가
  찾은 3.10·3.11의 겹친 f-string 스택 넘침 수정은 로컬 커밋이다(push 전).

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
| Windows CI 잡 | 2026-11-15 감사 뒤 추가했다(아래 "Windows CI 감사" 절). |
| 외부 타입 주석으로 `untyped-receiver` 줄이기 | 자기 분석 미해석의 대부분이지만 별도 설계가 필요하다. |
| 재정의 본문별 이름 해석 | 호출 수집(`collect`·`flow`·`exposure`)에 본문 맥락을 넘겨야 해 범위가 크다. 지금은 일부 정의에서만 묶인 이름을 모름으로 센다. |
| isthmus 호환 핀 갱신 | isthmus 저장소의 일이고, 게시본 검증 뒤에만 바꾼다. |
| dead·cycles·rules·MCP | 생산자 전용 범위라 해당하지 않는다. |

## Windows CI 감사 (2026-11-15, 이 브랜치)

POSIX 전제를 먼저 감사했다. 로컬에서는 Windows 조건을 흉내 내 전체 테스트를 돌렸다: 텍스트 모드 쓰기의 CRLF(pathlib
`write_text` 대역), 디렉터리 핸들 없는 대체 경로(`_HAS_DIR_FD=False`), Git의 `core.autocrlf=true`(HOME의 설정), 기존
Windows 건너뛰기 표시. 흉내 내지 못하는 것(NTFS 대소문자 무시, `os.name == "nt"` 분기, 심볼릭 링크 권한, Windows의
`samestat(lstat, fstat)` 신원 일치)은 CPython 소스로 추론만 했고 CI만 확인한다.

- **고친 제품 결함**
  - 표준 출력을 로캘 인코딩으로 써서 Windows(cp1252)에서 한글 경로·식별자가 있으면 종료 2, 그 밖에는 UTF-8이 아닌 바이트와
    CRLF였다. `cli/main.py`의 `_exchange_stdout`이 표준 출력을 UTF-8·LF로, `graph/roots.py`의 `_read_stdin`이
    `--roots-from -`일 때만 표준 입력을 strict UTF-8로 바꾼다.
  - CRLF 선언 파일의 줄 끝 `\r`이 버전 지정자에 남아 거짓 `route-framework-version-unknown:`, aiohttp 하한 하락, schema의
    Flask-SQLAlchemy 이름 규칙·ORM 버전 한계 차이를 냈다(`routes/versions.py`의 `_compact`). Windows 잡에서는
    `make_project`가 CRLF를 쓰므로 Flask 테스트 다섯 개가 이 결함으로 실패했을 것이다.
- **플랫폼별 기대로 고친 테스트**: `pyproject.toml`을 로캘 인코딩으로 읽던 `test_cli`·`scripts/verify_cli_contract.py`(cp1252로
  디코드 실패), 문자열 길이로 바이트 예산을 기대하던 `test_graph_source_diagnostics`, Windows 캡처가 대소문자만 다른 파일 경로를
  먼저 거부하는 `test_change_materialization_identity`, 커밋과 작업 트리를 바이트로 비교하는 `test_change_capture_contracts`
  (`core.autocrlf=false` 고정).
- **건너뛰기**: 기존 표시(디렉터리 핸들·FIFO·`/dev/fd`·장치·POSIX 셸 스크립트·실행 권한 비트·shebang 자식·파일 이름의
  제어 문자)를 그대로 쓴다. 심볼릭 링크 테스트(48개)는 `tests/conftest.py`의 `SYMLINKS`가 Windows에서 링크를 못 만들 때만
  건너뛴다. POSIX에서 못 만들면 건너뛰지 않고 실패한다. Windows CI는 `PYTHOGRAPH_REQUIRE_SYMLINKS=1`이라 러너 권한이 바뀌면
  테스트를 시작하지 않는다(`pytest_configure`의 사용 오류, 종료 4). 첫 CI 실행에서 48개 모두 돌았다.
- **`.gitattributes`**: `* text=auto eol=lf`. Git for Windows 기본값(`core.autocrlf=true`)이 공유 벡터를 CRLF로 바꾸면
  `test_conformance`의 sha256 대조가 깨진다. 잡의 "Line endings" 단계가 체크아웃이 LF인지 확인한다.
- **잡 구성**: `verify-windows`(windows-latest, Python 3.10·3.13 행렬, bash). Windows stat·fstat 구현이 3.12에서 바뀌어
  (FileIdInfo·GetFileInformationByName) 신원 대조를 그 전후로 본다. 테스트·커버리지 게이트·CLI 계약(개발 설치·wheel)·자기 분석.
  Lint·Format·Typecheck는 뺐다. `mypy`는 실행 플랫폼이 기본값이라 Windows에서 `os.O_DIRECTORY`·`os.O_NOFOLLOW`
  (`project.py`, `_HAS_DIR_FD`로 막힘)와 `os.set_blocking`(`inputs.py`, `O_NONBLOCK`으로 막힘)을 없는 속성으로 본다
  (`uv run mypy --platform win32`로 재현). 흉내 낸 Windows 커버리지는 약 93.7%다(게이트 90%).

### 독립 리뷰 반영 (PR #33)

- **S1**: CRLF 수정은 schema 사실도 바꾼다(`declared_majors` → Flask-SQLAlchemy 이름 규칙 메이저·ORM 버전 한계). CHANGELOG에
  적고 LF·CRLF 선언 파일의 schema 문서가 같다는 회귀 테스트를 더했다(수정 전 `User2FA` dynamic·한계 3개로 실패 확인).
- **S2**: 링크 건너뛰기를 Windows로 좁히고 `PYTHOGRAPH_REQUIRE_SYMLINKS`를 더했다.
- **S3·N5**: 표준 입력은 `--roots-from -`이 읽을 때만 strict UTF-8로 다시 연다. UTF-8 모드·C·C.UTF-8 로캘에서 0이던 깨진 바이트
  입력이 이제 64다(파일 입력과 같다). 테스트로 고정했고 docstring·CHANGELOG의 "strict 그대로" 주장을 고쳤다.
- **S4**: Windows 행렬을 3.10·3.13으로 넓혔다. 로컬 Linux 3.10 전체 실행도 통과했다.
- **N1–N4·N7**: 테스트의 CLI subprocess를 UTF-8로 읽고, README의 `project` 표기(UNC·`//?/`)와 PowerShell 5.1 재인코딩,
  `docs/CHANGE-IMPACT.md`의 `ident`·smudge·`working-tree-encoding`·순회 잘림을 적었다. N6은 아래 표에 남겼다.

### 3.10·3.11 스택 넘침 (`9cee4e0`의 `verify-windows (3.10)`, 재리뷰)

- **증상**: `tests/test_persistence_django.py::test_unparseable_parameter_annotations_are_not_models`의 4만 9,999개 `+` 문자열
  주석을 `parse_annotation`(`source/syntax.py`)이 `ast.parse`하다 프로세스가 죽었다(Python 예외·테스트 결과 없이 종료).
- **원인**: 3.10의 `ast2obj`(C 트리 → 파이썬 객체)는 재귀 깊이를 검사하지 않는다(3.11부터 `recursion_depth` 검사). 사슬
  상한 `MAX_CHAIN_TOKENS = 50_000`은 Linux 주 스레드 8 MiB로 정했는데 Windows `python.exe`는 주 스레드 스택을
  2,000,000 B만 잡는다(`PCbuild/python.vcxproj`의 `StackReserveSize`). 3.10.20 실측(Linux clang): 트리 한 단계에 약 64 B,
  1 MiB 스레드는 약 1.6만, 2,000,000 B는 약 3.2만 단계에서 죽는다(메서드 사슬은 절반). 모듈 파싱(`Project._parse`)도 같은
  거름을 쓰므로 사용자 소스 한 줄로도 죽는다.
- **재현**(Linux 3.10.20, `ulimit -s 1024`, 4만 9,999항 주석만 둔 프로젝트·3만 항 모듈만 둔 프로젝트의 schema): 수정 전 둘 다
  종료 139, 수정 뒤 둘 다 0(주석은 모르는 주석, 모듈은 `unreadable-sources:` 한계). graph도 139 → 0(`unparsedFiles` 1).
- **수정(사슬, `121ac23`)**: 3.10에서 사슬 추정치(`chain_bound`: 파일 전체의 사슬 문자 수가 5만 이하면 그 수(문자열 안
  포함), 넘으면 가장 긴 논리 줄의 사슬 토큰 수)가 2,000을 넘으면 토큰당 1 KiB(최소 8 MiB)의 스택을 가진 전용 스레드에서
  파싱한다(`parse_guarded`). 상한·결과는 그대로라 보존 범위가 좁아지지 않는다(넓지만 얕은 줄은 계속 분석).
- **정렬(`3a9ac2e`, coordinator)**: macOS의 `pthread_attr_setstacksize`는 페이지(arm64 16 KiB) 배수가 아니면 거부해(ValueError)
  macOS 3.10 CI가 두 건 실패했다. 스택 크기를 1 MiB 배수로 올린다(`parse_stack_bytes`). 페이지 배수만 받는 대역 테스트 2개.
- **겹친 f-string(재리뷰 S1)**: 3.10·3.11은 f-string 치환 필드마다 새 파서를 만들고 그 깊이 한도(MAXSTACK, 약 6,000단)를
  처음부터 센다. 사슬로 세지 않는 오른쪽 재귀(`~`·`not`·`-`·`lambda`·조건식·`**`)를 겹치면 파서마다 C 스택이 쌓인다. 실측
  (Linux, 파서마다 한도 가까이): 파서 다섯(따옴표 네 종류로 f-string 4단)에 3.10.20 최대 4,672 KiB, 3.11.18 최대 3,712 KiB.
  3.12·3.13은 MemoryError로 끝난다. 재현(`X = ~×5900 f"{~×5900 f'{~×5900 x}'}"`, `ulimit -s 2000`, graph): 3.10·3.11은
  모듈·주석 경로 모두 수정 전 139, 수정 뒤 0이고 출력은 3.12·3.13과 같다(모듈은 unparsed 1, 주석은 무시). 수정: 3.12 전에서
  f-string이 있을 수 있는(`[fF][rR]?['"]`, 모든 접두사 철자를 놓치지 않는 넉넉한 감지) 4,000자 넘는 소스는 16 MiB(실측의
  3.5배)를 더한 전용 스택에서 파싱한다. 4,000자 이하면 겹친 파서들의 깊이 합이 파서 하나의 한도의 2/3를 넘지 못한다.
- **스레드 실패 이유**: 전용 스레드를 만들 수 없으면 이제 "could not start a parser thread with a large enough stack"이라는
  따로 된 이유로 센다(깊이 한도로 가장하지 않는다. 이유 문자열은 닫힌 계약이 아니고 docs/GRAPH.md·README에 적었다).
  작업 스레드의 `BaseException`도 부른 스레드에서 다시 낸다(전에는 결과 목록이 비어 IndexError, 내부 오류 2).
- **3.11**: 긴 사슬에는 영향이 없다(3.11.18·3.12.3·3.13.18 모두 1 MiB·2,000,000 B 스레드의 30만 단계 사슬에서
  RecursionError). 겹친 f-string에는 영향이 있어 위 수정이 3.11도 덮는다. 로컬 3.11 전체 실행으로 확인했고 Windows 행렬에는
  더하지 않았다(3.11의 차이는 플랫폼과 무관한 파서 구조라 Linux 3.11 CI와 로컬 재현이 덮는다).
- **테스트**: 1 MiB 스레드에서 긴 사슬(주석·깊은 모듈·넓은 모듈)과 겹친 f-string(주석·모듈)을 읽는 별도 프로세스 테스트
  (수정 전 3.10·3.11에서 -11), 전용 스택 경로·스레드 실패·`BaseException` 전달·f-string 감지(접두사 10가지 철자, 저장소
  전체 f-string 파일)·길이 임계의 프로세스 내 테스트. 링크 교체 테스트 10개는 교체가 실제로 일어났을 때만 통과한다.
- **알려진 대가**: 스택 크기 설정(`threading.stack_size`)은 프로세스 전체 설정이라 스레드를 시작하는 순간에만 바꾸고
  되돌린다. pythograph를 라이브러리로 여러 스레드에서 함께 부르면 그 순간 만든 다른 스레드도 큰 스택을 받는다.

### 남긴 것 (이유)

| 항목 | 이유 |
|---|---|
| change-impact의 Git 작업 트리 변환 | `core.autocrlf=true`·`ident`·smudge filter·`working-tree-encoding`에서 `--head` 없이 분석하면 손대지 않은 파일이 `modified`다(과다 선택, 누락은 아니다. seed가 많으면 순회 잘림으로 끝날 수 있다). 바이트 신원 설계를 바꾸거나 변환 조건을 감지해 한계로 세야 해 설계가 필요하다. 지금은 `docs/CHANGE-IMPACT.md`에만 적었다(계수하지 않음). |
| Windows `project` 정규화 | bridge-facts v1은 POSIX 경로를 가정하고 Windows 정규화는 isthmus와 별도 합의 사항이다. pythograph는 realpath를 슬래시로 쓴 값(`C:/…`)을 낸다(README에 적음). |
| Windows에서 mypy | 위 세 속성을 `getattr`이나 `sys.platform` 분기로 바꾸면 돌릴 수 있다. 실행 결과에는 영향이 없어 이번에는 뺐다. |
| 금지 문자 파일 이름 테스트 | NTFS가 제어 문자 이름을 거부해 테스트 전체가 건너뛴다. NEL·U+2028 이름은 NTFS도 받으므로 그 둘만 따로 만드는 형태로 나눌 수 있다. |
| 릴리스 워크플로의 Windows 검증 | 요청 범위가 CI다. CI 결과가 안정적이면 `release.yml` verify 매트릭스에 같은 잡을 더할지 정한다. |
| 닫힌 표준 스트림(리뷰 N6) | 기존 결함이다. `--version >&-`는 `sys.stdout`이 None이라 `_write`의 AttributeError가 잡히지 않아 트레이스백·종료 1(예약 코드), `--roots-from - <&-`는 내부 오류 2다. 계약 코드(2)와 원인 문구로 바꾸는 작은 후속이다. |

## 다음 할 일

1. 겹친 f-string 수정 커밋을 push하고 PR #33의 CI(특히 Windows·macOS 3.10의
   `test_stacked_fstring_parsers_do_not_overflow_a_small_stack`)를 확인한다. MSVC 빌드에서 f-string 몫 16 MiB가 넉넉한지는 그
   실행만 증명한다. 행렬로 바뀐 잡 이름(`verify-windows (3.10)`·`verify-windows (3.13)`)을 필수 체크로 둘지 정한다.
2. 0.3.2 또는 0.4.0 릴리스. change-impact `input.files`의 의미가 바뀌었고 표준 출력이 항상 UTF-8·LF가 됐으므로 버전 판단에
   반영하고 RELEASING 절차를 따른다.
3. isthmus에 게시본 검증과 `compatibility.json` 핀 갱신을 요청한다.
4. 위 제외·남긴 항목 중 사용자가 고른 것.

## 주의

- **리뷰 방식**: AGENTS가 요구하는 GLM 리뷰(`packet-ask`)는 PR #31 작업 환경에 없어 실행하지 못했다. 대신 독립 Claude 리뷰를
  받았고, 지적을 코드·fixture로 확인한 뒤 반영했다. PR #31은 GLM 리뷰 없이 머지됐다. Windows CI 브랜치 작업 환경에도
  `packet-ask`가 없어 GLM 리뷰를 받지 못했다. 쓸 수 있으면 원래 절차로 돌아간다.
- **Windows 테스트 입력**: `make_project`는 텍스트 모드로 써서 Windows에서 CRLF 파일을 만든다(일부러 둔다). 바이트 수를
  기대하는 테스트는 파일 크기를 쓰고, Git 저장소를 만드는 테스트가 커밋과 작업 트리 바이트를 비교하면 `core.autocrlf=false`를
  저장소 설정으로 고정한다.
- **발행된 CHANGELOG 절은 고치지 않는다.** PR #31 작업 중 0.3.1 절의 첫 줄을 번역해 다시 쓴 적이 있어 원문으로 되돌렸다.
- **로컬 git 버전**: `change-impact`는 git 2.45 이상의 `--no-lazy-fetch`를 쓴다. 낮은 git에서는 partial clone 관련 경로가
  거부로 끝난다.
- **pytest 요약 줄**: 저장소 설정에 `-q`가 있어, `-q`를 또 주면 통과 개수 요약이 사라진다. 개수를 볼 때는 `-q` 없이 돌린다.
- **자기 분석**: `routes --role server --project .`는 Django·Flask fixture가 함께 있어 `--framework` 없이 exit 64로 끝난다.
  예상된 사용법 오류다.
