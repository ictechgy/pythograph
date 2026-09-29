"""루트 URLconf가 쓰는 합성 함수 뷰다. DB를 쓰지 않는다."""

import functools

from django.http import HttpResponse


def traced(view):
    """뷰를 감싸기만 하는 합성 데코레이터다. method 제한과 무관한 데코레이터 사례다.

    :param view: 감쌀 뷰 함수
    :returns: 같은 동작의 래퍼
    """

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        """원래 뷰를 그대로 호출한다."""
        return view(request, *args, **kwargs)

    return wrapper


@traced
def home(request):
    """홈 화면이다. method 제한이 없다(ANY)."""
    return HttpResponse("home")


def health_live(request):
    """생존 확인이다."""
    return HttpResponse("live")


def health_ready(request):
    """준비 확인이다."""
    return HttpResponse("ready")


def inline_a(request):
    """인라인 목록 include의 첫 뷰다."""
    return HttpResponse("a")


def inline_b(request, pk):
    """인라인 목록 include의 정수 변환기 뷰다."""
    return HttpResponse(f"b {pk}")


def yearly_report(request, year):
    """사용자 등록 변환기(yyyy) 뷰다."""
    return HttpResponse(f"report {year}")


def legacy_year(request, year):
    """정규식 경로(re_path) 뷰다."""
    return HttpResponse(f"legacy {year}")


def archive(request, slug):
    """끝 슬래시가 선택인 정규식 경로 뷰다."""
    return HttpResponse(f"archive {slug}")


def ping(request):
    """비포획 대안 정규식 경로 뷰다."""
    return HttpResponse("pong")


def promo(request, code):
    """전방 탐색이 있어 템플릿으로 바꿀 수 없는 정규식 경로 뷰다."""
    return HttpResponse(f"promo {code}")


def file_json(request, name):
    """부분 세그먼트(`<name>.json`) 뷰다."""
    return HttpResponse(f"file {name}")


def docs_page(request, rest):
    """끝 catch-all(path 변환기) 뷰다."""
    return HttpResponse(f"docs {rest}")


def page_detail(request, slug):
    """append()로 등록한 slug 변환기 뷰다."""
    return HttpResponse(f"page {slug}")


def beta(request):
    """조건부로만 등록되는 뷰다."""
    return HttpResponse("beta")
