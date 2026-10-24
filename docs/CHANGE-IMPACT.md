# 실제 변경 분석

```sh
pythograph change-impact --project . --base HEAD~1 --head HEAD > change.json
pythograph change-impact --project . --base HEAD --max-depth 8 --max-reached 500 > working-change.json
```

`--project`는 Git 저장소 루트다. `--head`가 없으면 base와 작업 트리의 추적 파일·비무시 미추적 파일을 비교한다.
두 시점의 파일 바이트를 별도 임시 디렉터리에 고정하고 AST와 기존 순회 엔진으로 각각 분석한다. 삭제·rename의 과거
호출자는 before에 보존한다. 같은 바이트의 유일한 삭제/추가 파일은 rename 후보로 표시한다. 줄바꿈 차이도 변경이다.
두 시점의 간선을 섞은 경로를 만들지 않는다. 대상 프로젝트를 import하거나 실행하지 않는다.

`pythograph-change-impact` v1에는 before/after 입력의 실제 SHA-256, source revision, seed 이유, 미매핑 파일,
기존 language-traversal 문서, 검토할 호출자와 연결된 정적 테스트 후보가 있다. 테스트 후보 표식은 파일 이름 규칙이며
실행 가능성·실패를 증명하지 않는다. 모든 변경 파일 선언을 seed로 고르는 파일 정밀도여서 무관한 선언·테스트가 포함될
수 있다. 기존 `impact <id>`는 한쪽 그래프 탐색으로 유지한다. MCP 서버는 제공하지 않으며 CLI JSON adapter는 전체
출처·한계를 보존해야 한다. 새 보고서 전체를 기존 language-traversal 입력으로 넘기지 않는다.

두 `traversal.project`는 [공유 순회 계약](https://github.com/ictechgy/isthmus/blob/main/docs/LANGUAGE-TRAVERSAL.md)의
POSIX realpath 규칙에 따라 원래 `--project`의 정규 경로를 보존한다. 임시 캡처 디렉터리는 논리 프로젝트 신원이 아니다.
`testCandidate`는 일반 그래프와 같은 `is_test_path` 정책(`test_*.py`, `*_test.py`, `tests.py`, `conftest.py`,
`test/`·`tests/` 디렉터리)을 쓴다. `testimonials.py`처럼 접두사만 비슷한 소스는 테스트로 표시하지 않는다.

항상 `status: incomplete`다. 빈 결과는 안전 보장이나 테스트 생략 허가가 아니다. 두 순회 문서의 미해석 호출·깊이·행
제한을 함께 읽어야 한다. signals, middleware, settings 이름으로 연결되는 클래스 및 framework가 생성하는
serializer/permission callback은 일부 누락될 수 있다. 선언 없는 설정·API·DB 계약 변경은 `unmappedFiles`로 남으며
영향을 추측해 pass로 바꾸지 않는다.

캡처 상한은 20,000파일, 파일당 4 MiB, 총 128 MiB다. 초과는 부분 분석 없이 코드 2다. 보고서 상한은 16 Mi 문자다.
`cost`는 준비와 전체 분석 시간을 포함한다. ignored 파일, symlink, submodule은 포함하지 않는다. 작업 트리는 두 번
읽은 내용이 같을 때만 분석하지만 파일 시스템 원자적 snapshot은 아니다. 분석 후 새 편집은 재분석해야 한다.
작은 fixture 결과를 전체 앱의 정확도나 실제 실패 예측으로 일반화하지 않는다.

`input.omissions`는 빠진 경로·종류(`symlink`, `submodule`, `directory`, `non-regular`, 실제 삭제인 `missing`)와
`sourceMayBeHidden`을 보존한다. 링크 대상은 조회하지 않는다. 소스를 가릴 수 있는 누락은 새 임시 디렉터리의 스캔과
별도로 원래 프로그램의 불확실성으로 전달하므로, 모듈 수준 호출을 닫힌 전체 프로그램의 `bound` 사실로 승격하지 않는다.
확인한 직접 호출과 함수 내부의 닫힌 지역 흐름은 유지하며 문서 링크만 빠진 경우 정상 `bound`를 일괄 제거하지 않는다.
실제 파일 순회의 링크·오류 개수는 위조하지 않는다. 정상 Git 삭제는 미확인 외부 소스와 구분한다.

작업 파일은 일반 Project 읽기와 같은 nofollow·디렉터리 핸들·nonblock·fstat 신원 검사를 사용한다. 검사 뒤 링크·FIFO·
다른 파일로 교체되면 코드 2이며 부분 성공 문서를 쓰지 않는다. 플랫폼이 디렉터리 핸들을 지원하지 않으면 기존의
경로 재검사와 파일 핸들 신원 대조를 적용한다. 모든 플랫폼의 파일 시스템 원자성을 보장한다는 뜻은 아니다.

캡처가 실행하는 각 Git 명령은 설정 조회부터 stdout·stderr 합산 128 MiB를 읽는 중 제한한다. 초과·읽기 오류·기존
60초 시간 초과는 입력 실패다. 바이트 길이를 다 모은 뒤 검사하는 메모리 제한이 아니다. 직접 소유한 Git 자식만
정리하며 다른 프로세스 그룹을 중단하지 않는다. 이 상한은 명령별 출력 바이트이고 전체 준비 시간·OS RSS 상한은 아니다.
