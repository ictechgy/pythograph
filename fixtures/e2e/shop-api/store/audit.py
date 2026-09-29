"""감사 기록 함수다."""

from store.models import AuditEntry


def record(action, order_id):
    """감사 기록을 한 줄 남긴다.

    :param action: 동작 이름
    :param order_id: 주문 id
    """
    AuditEntry.objects.create(action=action, order_id=order_id)
