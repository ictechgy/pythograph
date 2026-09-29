# isthmus 공유 적합성 벡터

isthmus(`f9dcd1d093605b4748c380287d463e07ce28b17e`)의 `conformance/`를 그대로 가져온 사본이다. 정본은 isthmus가
소유하며, 이 디렉터리 파일을 직접 고치지 않는다. 갱신할 때는 isthmus main의 파일과 `SHA256SUMS`를 함께 다시
복사하고(새 suite 파일 포함) 저장소 루트의 `conformance.lock`에 커밋과 파일별 sha256을 적은 뒤
`uv run pytest tests/test_conformance.py`로 확인한다.

pythograph가 실행하는 사례(생산자 대상):

| suite | ruleId | 검사 |
|---|---|---|
| `http-template` | `template.grammar` | 정규 문법 검사기가 소비자와 같은 판정·거부 사유를 낸다 |
| `http-template` | `template.normalize` | URI 경로 정규화 결과가 같다 |
| `http-limitation-scope` | `scope.validate` | 스코프 검증기가 소비자와 같이 판정한다(생산 문서가 거부되지 않게) |
| `http-limitation-scope` | `scope.applies` | 참조 구현의 적용 판정이 같다(스코프 설계가 기대는 규칙 확인) |
| `http-dispatch` | `dispatch.validate` | `order` 검증기(`exchange/order.py`)가 소비자와 같이 판정한다. routes 출력 golden에도 적용한다 |

건너뛰는 사례와 이유: `consumer` 전용 사례(`match.*`, `dispatch.match`, `dispatch.shadow`), 다른 생산자의 프레임워크 변환(`framework.openapi.*`는
`producer:openapi`, `framework.spring.*`는 `producer:kartograph`), `url-compose` 전체(클라이언트 `route-call` 조립
규칙이며 pythograph는 아직 서버 선언만 낸다).
