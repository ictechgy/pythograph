"""합성 상점 프로젝트의 WSGI 진입점이다."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop.settings")

# WSGI 서버가 부르는 애플리케이션 객체다.
application = get_wsgi_application()
