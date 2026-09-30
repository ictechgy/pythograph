# isthmus 공유 적합성 벡터

isthmus(`2954375ceffb335780a08e2e245baad833d5d636`)의 `conformance/`를 그대로 가져온 사본이다. 정본은 isthmus가
소유하며, 이 디렉터리 파일을 직접 고치지 않는다. 갱신할 때는 isthmus main의 파일과 `SHA256SUMS`를 함께 다시
복사하고(새 suite 파일 포함) 저장소 루트의 `conformance.lock`에 커밋과 파일별 sha256을 적은 뒤
`uv run pytest tests/test_conformance.py`로 확인한다.

pythograph가 실행하는 사례(`appliesTo`에 `producer` 또는 `producer:pythograph`가 있는 사례, 135건):

| suite | ruleId | 검사 |
|---|---|---|
| `http-template` | `template.grammar` | 정규 문법 검사기가 소비자와 같은 판정·거부 사유를 낸다 |
| `http-template` | `template.normalize` | URI 경로 정규화 결과가 같다 |
| `http-limitation-scope` | `scope.validate` | 스코프 검증기가 소비자와 같이 판정한다(생산 문서가 거부되지 않게) |
| `http-limitation-scope` | `scope.applies` | 참조 구현의 적용 판정이 같다(스코프 설계가 기대는 규칙 확인) |
| `http-dispatch` | `dispatch.validate` | `order` 검증기(`exchange/order.py`)가 소비자와 같이 판정한다. routes 출력 golden에도 적용한다 |
| `url-compose` | `compose.interpolation`·`query-tail`·`suffix`·`normalize`·`strip`·`mask` | 클라이언트 URL 조각 조립(`routes/client/compose.py`)이 같은 템플릿·접두사·authority·마스킹을 낸다 |
| `url-compose` | `compose.base-join` | `rfc3986`·`slash-join`·`dio-concat`·`httpx-base-url`·`aiohttp-base-url` 결합. `versionRange`가 있으면 그 하한을 증명한 프로젝트로 실행한다 |
| `url-compose` | `wrapper.method`·`wrapper.location` | 래퍼 동사 인자 바인딩, 여러 줄 호출의 시작 줄(실제 스캐너) |

건너뛰는 사례와 이유: `consumer` 전용 사례(`match.*`, `dispatch.match`, `dispatch.shadow`), 다른 생산자의 사례(`framework.openapi.*`는
`producer:openapi`, `framework.spring.*`·Spring `base-join`은 `producer:kartograph`, Go·Rust `base-join`은 `producer:gartograph`·
`producer:rustograph`), `scope.dynamic-*`(`dynamicScope` 검증 — pythograph는 아직 `dynamicScope`를 내지 않는다).
