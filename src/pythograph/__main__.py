"""`python -m pythograph` 진입점이다. 콘솔 스크립트와 같은 `main`을 부른다."""

from pythograph.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
