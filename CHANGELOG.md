# Changelog

이 프로젝트의 주요 변경 사항을 기록한다.

## [Unreleased]

### Added

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
- 합성 fixture(`fixtures/django/drf-shop`, `fixtures/flask/blog-app`)와 오라클 하네스(`experiments/oracle/`):
  resolver 순회·DRF 라우터·Flask `url_map` 대비 정밀도 100%, 기록을 오프라인 테스트로 다시 확인한다.
