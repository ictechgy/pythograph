"""마이그레이션(과거 스키마). pythograph는 마이그레이션을 읽지 않는다."""

from django.db import migrations


class Migration(migrations.Migration):
    """합성 마이그레이션. 지금은 없는 테이블을 만든다."""

    initial = True
    dependencies = []
    operations = [migrations.RunSQL("CREATE TABLE store_legacy_inventory (id integer)")]
