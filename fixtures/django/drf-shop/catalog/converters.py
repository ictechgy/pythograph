"""카탈로그 앱의 사용자 등록 경로 변환기다."""


class FourDigitYearConverter:
    """네 자리 연도만 받는 변환기다. `register_converter`로 "yyyy" 이름에 등록한다."""

    # 변환기가 맞추는 정규식이다.
    regex = "[0-9]{4}"

    def to_python(self, value):
        """경로 문자열을 정수 연도로 바꾼다.

        :param value: 경로에서 잡은 문자열
        :returns: 정수 연도
        """
        return int(value)

    def to_url(self, value):
        """정수 연도를 네 자리 문자열로 바꾼다.

        :param value: 정수 연도
        :returns: 0으로 채운 네 자리 문자열
        """
        return "%04d" % value
