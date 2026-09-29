"""긴 앱 이름으로 PostgreSQL·MySQL 절단까지 만드는 모델."""

from django.db import models


class InventoryAdjustmentRecord(models.Model):
    """기본 이름 54자. M2M 기본 이름은 63자를 넘는다."""

    quantity = models.IntegerField()
    approved_by_supervisors_and_managers = models.ManyToManyField("accounts.User")
