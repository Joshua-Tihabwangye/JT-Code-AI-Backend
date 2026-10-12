"""Generate the self-hosted Supabase API keys (anon and service_role) from SUPABASE_JWT_SECRET.

    python scripts/generate_supabase_keys.py "$SUPABASE_JWT_SECRET"

Prints SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY lines for .env.production.
GoTrue, PostgREST and Storage verify these HS256 tokens with the same secret; regenerate
both keys whenever the secret changes.
"""

from __future__ import annotations

import sys
import time

import jwt

TEN_YEARS = 10 * 365 * 24 * 3600


def key(role: str, secret: str) -> str:
    now = int(time.time())
    return jwt.encode({"role": role, "iss": "supabase", "iat": now, "exp": now + TEN_YEARS}, secret, "HS256")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or len(argv[1]) < 32:
        print(__doc__.strip(), file=sys.stderr)
        print("\nThe secret must be at least 32 characters (openssl rand -hex 32).", file=sys.stderr)
        return 2
    secret = argv[1]
    print(f"SUPABASE_ANON_KEY={key('anon', secret)}")
    print(f"SUPABASE_SERVICE_ROLE_KEY={key('service_role', secret)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
