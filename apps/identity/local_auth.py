"""Local-development logins without the Supabase Admin API (DEBUG only).

Since Phase 3 the gateway serves ``/auth/v1/admin`` on its internal listener only, so ``manage.py`` run
on the host (``scripts/local_manage.py docker …``) cannot reach it. The local commands
(``create_local_account``, ``join_local_organization``) then create the confirmed e-mail login directly
in the stack's own ``auth`` schema, the way Supabase seed scripts do. GoTrue signs such a user in as
usual. Production never takes this path: it refuses to run unless DEBUG is on.
"""

from __future__ import annotations

import json
import uuid

from django.conf import settings
from django.db import DatabaseError, connection, transaction

from apps.identity.supabase_admin import SupabaseAdminError, create_auth_user

_INSTANCE = "00000000-0000-0000-0000-000000000000"


def create_auth_user_in_database(email: str, password: str, metadata: dict | None = None) -> tuple[str, bool]:
    """Create (or reuse) a confirmed e-mail login in ``auth.users``; return ``(user_id, created)``."""
    if not settings.DEBUG:
        raise SupabaseAdminError("Creating logins in the database is for local development (DEBUG) only.")
    email = email.strip().lower()
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT id FROM auth.users WHERE lower(email) = %s LIMIT 1", [email])
            row = cursor.fetchone()
            if row:
                return str(row[0]), False
            user_id = str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO auth.users (instance_id, id, aud, role, email, encrypted_password,"
                " email_confirmed_at, raw_app_meta_data, raw_user_meta_data, created_at, updated_at,"
                " confirmation_token, recovery_token, email_change_token_new, email_change)"
                " VALUES (%s, %s, 'authenticated', 'authenticated', %s,"
                " extensions.crypt(%s, extensions.gen_salt('bf')), now(), %s, %s, now(), now(),"
                " '', '', '', '')",
                [
                    _INSTANCE,
                    user_id,
                    email,
                    password,
                    json.dumps({"provider": "email", "providers": ["email"]}),
                    json.dumps(metadata or {}),
                ],
            )
            cursor.execute(
                "INSERT INTO auth.identities (provider_id, user_id, identity_data, provider, created_at,"
                " updated_at) VALUES (%s, %s, %s, 'email', now(), now())",
                [user_id, user_id, json.dumps({"sub": user_id, "email": email, "email_verified": True})],
            )
    except DatabaseError as exc:
        raise SupabaseAdminError(f"Could not create the login in the auth schema: {exc}") from exc
    return user_id, True


def create_local_login(email: str, password: str, metadata: dict | None = None) -> tuple[str, bool]:
    """The Admin API when it answers; otherwise, in development, the stack's ``auth`` schema."""
    try:
        return create_auth_user(email, password, metadata)
    except SupabaseAdminError as admin_error:
        if not settings.DEBUG:
            raise
        try:
            return create_auth_user_in_database(email, password, metadata)
        except SupabaseAdminError as database_error:
            raise SupabaseAdminError(f"{admin_error} Database fallback: {database_error}") from database_error
