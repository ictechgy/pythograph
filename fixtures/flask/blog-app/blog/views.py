"""클래스 기반 뷰다(MethodView와 View)."""

from flask.views import MethodView, View


class ItemAPI(MethodView):
    """항목 API다. 정의한 get·post가 methods가 된다."""

    def get(self, item_id=None):
        """항목을 조회한다."""
        return "item"

    def post(self, item_id=None):
        """항목을 만든다."""
        return "created"


class SettingsView(View):
    """`methods`를 직접 선언한 View다. `dispatch_request`가 모든 method를 처리한다."""

    # 이 뷰가 받는 method다.
    methods = ["GET", "PUT"]

    def dispatch_request(self):
        """설정 화면을 처리한다."""
        return "settings"
