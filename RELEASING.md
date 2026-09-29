# 릴리스 절차

pythograph는 `v<버전>` 태그를 push하면 GitHub Actions(`.github/workflows/release.yml`)가 검증 → 빌드 → PyPI 게시를 차례로
실행한다. 게시는 PyPI **Trusted Publishing**(OIDC)으로 하며 API 토큰·비밀값을 저장소에 두지 않는다. 아래 "처음 한 번" 설정을
마치기 전에는 게시 작업이 실패하므로, 그 전에 태그를 만들지 않는다.

## 처음 한 번: PyPI와 GitHub 설정 (저장소 관리자가 직접)

1. **PyPI 계정**: https://pypi.org 에 로그인하고 2단계 인증을 켠다.
2. **신뢰할 수 있는 게시자 등록(pending publisher)**: 프로젝트가 아직 없으므로 https://pypi.org/manage/account/publishing/
   의 "Add a new pending publisher"에서 GitHub을 고르고 다음 값을 넣는다. 첫 게시 때 PyPI 프로젝트 `pythograph`가 만들어진다.
   - PyPI Project Name: `pythograph`
   - Owner: `ictechgy`
   - Repository name: `pythograph`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. **GitHub 환경**: 저장소 Settings → Environments → New environment에서 `pypi`를 만든다. 권장: "Required reviewers"에 본인을
   넣어 게시 전에 수동 승인을 거치게 하고, "Deployment branches and tags"를 `v*` 태그로 제한한다.
4. (선택) **TestPyPI로 먼저 연습**: https://test.pypi.org 에서 같은 pending publisher를 등록하고, 워크플로의 게시 단계에
   `repository-url: https://test.pypi.org/legacy/`를 임시로 넣은 브랜치에서 태그를 만들어 본다. 연습이 끝나면 되돌린다.

## 릴리스할 때마다

1. `main`에서 새 브랜치를 만들고 버전을 올린다. 두 곳이 같아야 한다(테스트가 대조하고, 릴리스 워크플로가 태그와도 대조한다).
   - `pyproject.toml`의 `project.version`
   - `src/pythograph/__init__.py`의 `__version__`
2. `CHANGELOG.md`의 `[Unreleased]`를 `[<버전>] - <YYYY-MM-DD>`로 바꾸고 빈 `[Unreleased]`를 새로 둔다.
3. 로컬 점검(모두 통과해야 한다):

   ```sh
   uv sync
   uv run ruff check src tests && uv run ruff format --check src tests
   uv run mypy
   uv run pytest --cov
   uv run python scripts/verify_cli_contract.py
   rm -rf dist && uv build
   uvx --from twine twine check dist/*
   uv venv /tmp/pythograph-wheel-check && uv pip install --python /tmp/pythograph-wheel-check/bin/python dist/*.whl
   /tmp/pythograph-wheel-check/bin/pythograph --version
   ```

4. PR을 만들어 리뷰·CI를 거친 뒤 `main`에 머지한다.
5. 머지된 `main` 커밋에 태그를 만들고 push한다(태그 이름은 `v` + 버전).

   ```sh
   git switch main && git pull --ff-only
   git tag -a v0.1.0 -m "pythograph 0.1.0"
   git push origin v0.1.0
   ```

6. Actions의 "Release" 실행을 확인한다. `verify`(ubuntu·macOS × Python 3.10·3.13) → `build`(태그·버전 대조, sdist·wheel,
   깨끗한 가상 환경에 wheel을 설치해 `--version`과 CLI 계약 확인) → `publish`(환경 `pypi` 승인 후 게시) 순서다.
7. 게시 뒤 확인:

   ```sh
   uv tool install pythograph==0.1.0 && pythograph --version
   pipx install pythograph==0.1.0
   ```

8. (선택) GitHub Releases에 태그 기준 릴리스 노트를 만든다(CHANGELOG 해당 절을 붙인다).
9. README·README.ko의 설치 절에서 "아직 PyPI에 없다"는 문장을 지우고 `uv tool install pythograph`·`pipx install pythograph`를
   기본으로 바꾼다.

## 실패했을 때

- **태그와 버전이 다름**: `build`가 멈춘다. 태그를 지우고(`git push --delete origin v<버전>`, `git tag -d v<버전>`) 버전을 맞춘 뒤
  다시 만든다. PyPI에 올라가기 전이라 되돌릴 것이 없다.
- **`publish`가 `invalid-publisher`로 실패**: PyPI의 게시자 설정(저장소·워크플로 파일 이름·환경 이름)이 위 값과 정확히 같은지
  확인한다. 실패한 실행은 Actions에서 "Re-run failed jobs"로 다시 돌린다(같은 파일을 다시 빌드하지 않는다).
- **이미 올라간 버전은 덮어쓸 수 없다**: PyPI는 같은 버전 파일의 재업로드를 거부한다. 문제가 있으면 PyPI에서 그 버전을 yank하고
  패치 버전을 올려 다시 릴리스한다.
