"""Store organization invite tokens as SHA-256 digests (bearer secrets must not sit in the database).

Existing raw tokens (``secrets.token_urlsafe(32)``, 43 characters) are replaced by their digest, so
links already sent keep working: acceptance hashes the presented token and compares digests.
"""

import hashlib
import re

from django.db import migrations

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def hash_tokens(apps, schema_editor):  # noqa: ARG001
    Invite = apps.get_model("identity", "OrganizationInvite")
    for invite in Invite.objects.only("id", "token").iterator():
        if not _DIGEST.match(invite.token):
            digest = hashlib.sha256(invite.token.encode()).hexdigest()
            Invite.objects.filter(pk=invite.pk).update(token=digest)


class Migration(migrations.Migration):
    dependencies = [("identity", "0009_unusable_passwords")]

    operations = [migrations.RunPython(hash_tokens, migrations.RunPython.noop)]
