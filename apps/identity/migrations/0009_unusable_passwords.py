"""Mark empty Django passwords as unusable.

Supabase Auth owns credentials: the bcrypt hash lives in ``auth.users.encrypted_password``. Rows that
Django created from a Supabase session stored ``''`` in ``password``, which looks like data loss. The
explicit unusable marker (``!<random>``) states the intent: this row can never be used for a Django
password login.
"""

from django.contrib.auth.hashers import make_password
from django.db import migrations


def mark_unusable(apps, schema_editor):  # noqa: ARG001
    User = apps.get_model("identity", "User")
    for user_id in User.objects.filter(password="").values_list("pk", flat=True).iterator():
        User.objects.filter(pk=user_id).update(password=make_password(None))


class Migration(migrations.Migration):
    dependencies = [("identity", "0008_user_avatar_asset")]

    operations = [migrations.RunPython(mark_unusable, migrations.RunPython.noop)]
