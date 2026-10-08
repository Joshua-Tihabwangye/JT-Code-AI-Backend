-- Service roles used by GoTrue, Storage and PostgREST. This script runs only
-- while the named Supabase database volume is first initialized.
\getenv pgpass POSTGRES_PASSWORD

ALTER USER authenticator WITH PASSWORD :'pgpass';
ALTER USER supabase_auth_admin WITH PASSWORD :'pgpass';
ALTER USER supabase_storage_admin WITH PASSWORD :'pgpass';
