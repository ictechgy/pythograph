#!/usr/bin/env python
"""합성 상점 프로젝트의 manage.py다."""

import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
