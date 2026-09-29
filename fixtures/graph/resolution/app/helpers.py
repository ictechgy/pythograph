"""모듈 수준 함수와 장식자다."""

import functools
import json


def slugify(text):
    """문자열을 슬러그로 바꾼다(외부 호출만 한다)."""
    return text.strip().lower().replace(" ", "-")


def traced(function):
    """호출을 감싸는 프로젝트 장식자다."""

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        """감싼 함수를 부른다(매개변수 호출이라 잇지 못한다)."""
        return function(*args, **kwargs)

    return wrapper


def cached(seconds):
    """장식자 팩토리다."""

    def decorate(function):
        """실제 장식자다."""
        return function

    return decorate


def dump(value):
    """외부 라이브러리 호출이다."""
    return json.dumps(value)
