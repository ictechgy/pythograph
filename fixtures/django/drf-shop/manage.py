#!/usr/bin/env python
"""합성 상점 프로젝트의 Django 관리 명령 진입점이다."""

import os
import sys


def main() -> None:
    """설정 모듈 기본값을 정하고 Django 관리 명령을 실행한다."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
