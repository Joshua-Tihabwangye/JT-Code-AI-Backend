# Deploying n8n (queue mode)

n8n runs **separately** from the JT-Code API, as three roles that share a Redis
queue and a Supabase PostgreSQL schema:

| Service | Command | Role |
| --- | --- | --- |
| `n8n-main` | (default) | Editor UI, public REST API (used by `manage.py n8n_workflows push`) and triggers |
| `n8n-webhook` | `webhook` | Receives Django's signed dispatches at `WEBHOOK_URL` and enqueues executions |
| `n8n-worker` | `worker` | Executes workflows; scale with `--scale n8n-worker=N` |

## 1. Database

Run `supabase-n8n-role.sql` as the `postgres` user, after setting a strong
password. It creates the `n8n` role and schema. The role has no access to the
application schema; n8n stores only its own state, and JT-Code's state changes
only through signed API callbacks.

## 2. Environment

```bash
cp infra/n8n/n8n.env.example infra/n8n/n8n.env   # git-ignored
```

The variables that must match Django:

| n8n | Django | Direction |
| --- | --- | --- |
| `WEBHOOK_URL` | `N8N_WEBHOOK_BASE_URL` | Django → n8n webhooks |
| `JT_CODE_DISPATCH_SECRET` | `N8N_DISPATCH_SECRET` | Django signs, n8n verifies |
| `JT_CODE_CALLBACK_SECRET` | `N8N_WEBHOOK_SECRET` | n8n signs, Django verifies |
| `JT_CODE_RELAY_SECRET` | `N8N_SENTRY_RELAY_SECRET` | error workflow → Sentry relay |
| `JT_CODE_API_BASE_URL` | `N8N_CALLBACK_BASE_URL` | where n8n calls back |

Generate each secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## 3. Start

```bash
docker compose -f infra/n8n/docker-compose.yml --env-file infra/n8n/n8n.env up -d --scale n8n-worker=3
```

## 4. Credentials and API key

In the n8n UI (`n8n-main`), complete these steps:

1. Create an API key (Settings → n8n API) and set it as Django's `N8N_API_KEY`.
2. Create the provider credentials used by the workflows:
   * Slack (`slackApi`)
   * SMTP (`smtp`)
   * Google Drive (`googleDriveOAuth2Api`)
   * Notion (`notionApi`)
   * GitHub (`githubApi`)
3. Put each credential's id in Django's environment:
   * `N8N_CREDENTIAL_SLACK`
   * `N8N_CREDENTIAL_SMTP`
   * `N8N_CREDENTIAL_GOOGLE_DRIVE`
   * `N8N_CREDENTIAL_NOTION`
   * `N8N_CREDENTIAL_GITHUB`

## 5. Deploy the workflows from Git

```bash
python manage.py n8n_workflows validate   # contract checks
python manage.py n8n_workflows push       # create/update, link the error workflow, activate
python manage.py n8n_workflows check      # CI/cron drift check
```

Never edit production workflows in the UI. Change the JSON in `n8n/workflows/`,
bump the version (`<key>.v<N+1>.json`), then push. Django dispatches only to
the newest registered version, and `push` deactivates superseded versions.

## Operations

* **Monitoring:**
  * n8n exports Prometheus metrics (`N8N_METRICS=true`) on `n8n-main:5678/metrics`.
  * Django exports `jt_workflow_*` metrics.
  * The *Workers, events and workflows* Grafana dashboard covers both.
* **Data retention:** successful executions are not stored. Failed executions
  are kept for 14 days (`EXECUTIONS_DATA_MAX_AGE=336` hours).
* **Back up `N8N_ENCRYPTION_KEY`.** Without it, the stored credentials cannot be decrypted.
