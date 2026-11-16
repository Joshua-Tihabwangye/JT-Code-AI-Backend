# JT-Code backend: rules for agents

The active plan is [docs/EXECUTION_BACKLOG.md](docs/EXECUTION_BACKLOG.md). Its evidence is in
[docs/SYSTEM_AUDIT_2026-11.md](docs/SYSTEM_AUDIT_2026-11.md). The frontend lives in `../jt-code-frontend`.

## Guardrails

* **G1 Plan first.** Work only on backlog tasks, in phase order. Finish (☑ or ⚠) and gate a phase before
  starting the next. If something new and necessary turns up, add it to the backlog as a task, with the
  reason, before doing it. Do not drift into unrelated refactors, renames or "while I'm here" changes.
* **G2 One phase, one commit per repo.** When a phase's gate passes:
  * commit it to the current local branch (`Working_256`), with a message naming the phase;
  * never push, never create a remote branch or PR, never rewrite history.
* **G3 Gates are mandatory.** Run the phase's gate from the backlog and paste the result into the progress
  log. A failing test is fixed or reported, never skipped, deleted or weakened to pass.
* **G4 Architecture is fixed** (ADRs in `docs/adr/`):
  * Supabase owns identity and passwords; Django owns profile, tenancy and authorization;
  * PostgreSQL (Supabase) is the only database, with pgvector for vectors;
  * the outbox feeds Kafka; Celery runs over Redis;
  * Python builds every visual;
  * deployment is Docker Compose on one host, not Kubernetes.

  Do not propose replacing these unless the user asks.
* **G5 Secrets.**
  * Never print, log or commit values from `.env`, `.env.docker` or `.env.production`; read key names only.
  * `.env*` files are the user's. Do not edit them; tell the user which keys to add.
  * Examples (`.env*.example`) carry placeholders only.
* **G6 No destructive operations without asking:**
  * dropping or resetting databases or volumes;
  * `docker compose down -v`;
  * deleting users or rows in a live database;
  * rotating secrets.

  Read-only queries against local databases are fine.
* **G7 Docker needs sudo,** which the agent does not have. Validate compose changes with
  `python3 scripts/compose_config_check.py` and `tests/test_compose_stacks.py`. Mark live checks ⚠ for the user
  (`make docker-up`, then `scripts/system_check.py`).
* **G8 Both stacks keep working.**
  * Native (`.env`, `make native-up`) and Docker (`.env.docker`, `make docker-up`) share ports 8000 and
    54321, and only one runs at a time.
  * New settings default to the current behaviour. Compose sets the hardened value explicitly.
* **G9 Security changes fail closed and are tested.** Every new check gets a test that proves it rejects the bad
  case and accepts the good case.
* **G10 Match the codebase.** Follow existing patterns (settings via `env()`, validation in
  `config/settings/validation.py`, tenant checks via `apps.identity.authorization`, docs in `docs/`).
  Register any new third-party import in `inventory_matrix.INTEGRATIONS`.
* **G11 Frontend contract.**
  * After changing frontend API calls, regenerate the snapshot:
    `python scripts/contracts/extract_frontend_contract.py ../jt-code-frontend`.
  * Keep `pytest tests/test_phase18_verification.py -k contract` green.
* **G12 Report honestly.** In the progress log, write what was verified, what was not (and why), and every
  ⚠ operator step.

## Commands

* Lint and format: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
* Migrations check: `.venv/bin/python manage.py makemigrations --check --dry-run`
* Tests run against the native PostgreSQL (`TEST_DATABASE_URL`): `.venv/bin/pytest <files> -n 4`.
  Use `--create-db` for phase gates that add migrations.
* Compose check (no daemon or real secrets needed): `python3 scripts/compose_config_check.py`
* Frontend: `cd ../jt-code-frontend && npm run typecheck && npm run lint && npm run test && npm run build`
