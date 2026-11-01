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
   rm -rf dist && uv build --build-constraints build-constraints.txt --require-hashes
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

6. Actions의 "Release" 실행을 확인한다. `verify`(ubuntu·macOS × Python 3.10·3.13) → `build`(태그·버전 대조, 고정한 빌드 백엔드로
   sdist·wheel, 깨끗한 가상 환경에 wheel을 설치해 `--version`과 CLI 계약 확인) → `publish`(환경 `pypi` 승인 후 게시) 순서다.
7. 게시 뒤 확인:

   ```sh
   uv tool install pythograph==0.1.0 && pythograph --version
   pipx install pythograph==0.1.0
   ```

8. (선택) GitHub Releases에 태그 기준 릴리스 노트를 만든다(CHANGELOG 해당 절을 붙인다).
9. README·README.ko의 설치 절에서 "아직 PyPI에 없다"는 문장을 지우고 `uv tool install pythograph`·`pipx install pythograph`를
   기본으로 바꾼다.
10. [isthmus](https://github.com/ictechgy/isthmus)에 새 버전의 호환 검증과 producer 핀 갱신을 요청한다(이슈 또는 PR).
    isthmus의 `compatibility.json`과 `docs/COMPATIBILITY.md`는 isthmus가 게시본을 직접 검증한 뒤에만 바꾸므로(재현:
    `node scripts/verify-python-producer.mjs <isthmus-main.js> <pythograph-executable>`), 요청하지 않으면 그 표가 이전
    pythograph 버전에 머문다. 이 저장소에서는 isthmus 파일을 고치지 않는다.

## 빌드 도구 고정

게시하는 sdist·wheel이 태그를 push한 시점의 PyPI 최신판에 좌우되지 않도록 빌드 도구를 고정한다.

- **빌드 백엔드**: `build-constraints.txt`가 hatchling과 그 의존성(Python 3.10용 `tomli` 포함)을 정확한 버전과 해시로 고정한다.
  CI의 wheel 검증과 릴리스 `build`는 `uv build --build-constraints build-constraints.txt --require-hashes`로 빌드한다.
  `--require-hashes`가 있어야 이 파일에 없는 빌드 의존성이 해시 없이 최신판으로 들어오지 않는다(빠진 것이 있으면 빌드가 실패한다).
  `pyproject.toml`의 `requires = ["hatchling>=1.25"]`는 sdist에서 직접 빌드하는 사용자를 위해 범위로 둔다.
- **uv**: `ci.yml`·`release.yml`의 `astral-sh/setup-uv` `version`(세 곳)으로 고정한다. setup-uv는 받은 uv를 액션에 내장된 체크섬 표
  (`src/download/checksum/known-checksums.json`)로 먼저 검증하고, 표에 없는 버전이면 실행할 때 받는 버전 매니페스트의 체크섬을 쓴다.
  체크섬까지 커밋 SHA로 고정되도록 그 표에 있는 버전으로 올리고, 표에 없는 uv가 필요하면 setup-uv도 그 버전을 아는 릴리스의 커밋
  SHA로 함께 올린다. 올리기 전에 uv 보안 권고(https://github.com/astral-sh/uv/security/advisories)를 확인한다.
  지금 고정한 0.12.17은 setup-uv v10.2.0 표의 최신판이다. 이 버전에 남은 권고 GHSA-2cv4-cqwr-gwf7은 Windows 호스트에만 해당해
  ubuntu·macOS 러너와 무관하다.

빌드 의존성을 올리거나 `[build-system] requires`를 바꿨을 때 다시 만든다.

```sh
uv pip compile build-constraints.in --universal --python-version 3.10 --generate-hashes --no-build --exclude-newer P14D -o build-constraints.txt
rm -rf dist && uv build --build-constraints build-constraints.txt --require-hashes
```

- 입력 `build-constraints.in`은 `pyproject.toml`의 `[build-system] requires`와 같게 둔다. 고정한 판이 `requires`를 만족하지 못하거나
  `requires`에 이 파일에 없는 패키지가 생기면 `uv build`가 해석에 실패한다.
- `--universal --python-version 3.10`은 지원하는 모든 Python·운영체제에 맞는 파일 하나를 만든다(3.10 전용 `tomli`에는 표식이 붙는다).
- `--exclude-newer P14D`는 올라온 지 14일이 안 된 판을 고르지 않는다(막 게시된 악성 판을 피한다). `--no-build`는 해석 중에 sdist를
  빌드(코드 실행)하지 않는다.
- 바뀐 버전과 해시는 PR diff로 검토하고, 파일 머리에 uv가 적은 명령이 위 명령과 같은지 본다.

## 실패했을 때

- **`build`가 `Hash mismatch`나 `pinned upfront`로 실패**: 받은 빌드 의존성의 해시가 `build-constraints.txt`와 다르거나, 그 파일이
  `[build-system] requires`와 맞지 않는다. 해시가 다르면 그 파일을 믿지 말고 원인부터 확인한다. 의도한 변경이면 "빌드 도구 고정"의
  명령으로 다시 만들어 PR로 올린다.
- **태그와 버전이 다름**: `build`가 멈춘다. 태그를 지우고(`git push --delete origin v<버전>`, `git tag -d v<버전>`) 버전을 맞춘 뒤
  다시 만든다. PyPI에 올라가기 전이라 되돌릴 것이 없다.
- **`publish`가 `invalid-publisher`로 실패**: PyPI의 게시자 설정(저장소·워크플로 파일 이름·환경 이름)이 위 값과 정확히 같은지
  확인한다. 실패한 실행은 Actions에서 "Re-run failed jobs"로 다시 돌린다(같은 파일을 다시 빌드하지 않는다).
- **이미 올라간 버전은 덮어쓸 수 없다**: PyPI는 같은 버전 파일의 재업로드를 거부한다. 문제가 있으면 PyPI에서 그 버전을 yank하고
  패치 버전을 올려 다시 릴리스한다.
