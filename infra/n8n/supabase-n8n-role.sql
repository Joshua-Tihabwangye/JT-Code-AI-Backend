-- Isolated Supabase role and schema for n8n's own state (Phase 16).
-- n8n may never read or write JT-Code's application tables: canonical state
-- lives in Django/PostgreSQL and changes only through signed API callbacks.
-- Run once as the postgres user; replace the password placeholder first.
CREATE ROLE n8n LOGIN PASSWORD 'CHANGE_ME' NOINHERIT;
CREATE SCHEMA IF NOT EXISTS n8n AUTHORIZATION n8n;
REVOKE ALL ON SCHEMA public FROM n8n;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM n8n;
ALTER ROLE n8n SET search_path = n8n;
-- Keep n8n's tables out of the Supabase Data API.
REVOKE ALL ON SCHEMA n8n FROM anon, authenticated;
