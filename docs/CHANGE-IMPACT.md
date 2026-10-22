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

항상 `status: incomplete`다. 빈 결과는 안전 보장이나 테스트 생략 허가가 아니다. 두 순회 문서의 미해석 호출·깊이·행
제한을 함께 읽어야 한다. signals, middleware, settings 이름으로 연결되는 클래스 및 framework가 생성하는
serializer/permission callback은 일부 누락될 수 있다. 선언 없는 설정·API·DB 계약 변경은 `unmappedFiles`로 남으며
영향을 추측해 pass로 바꾸지 않는다.

캡처 상한은 20,000파일, 파일당 4 MiB, 총 128 MiB다. 초과는 부분 분석 없이 코드 2다. 보고서 상한은 16 Mi 문자다.
`cost`는 준비와 전체 분석 시간을 포함한다. ignored 파일, symlink, submodule은 포함하지 않는다. 작업 트리는 두 번
읽은 내용이 같을 때만 분석하지만 파일 시스템 원자적 snapshot은 아니다. 분석 후 새 편집은 재분석해야 한다.
작은 fixture 결과를 전체 앱의 정확도나 실제 실패 예측으로 일반화하지 않는다.
