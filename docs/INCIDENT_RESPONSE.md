# Incident response

## Severity

| Severity | Definition | Response | Updates |
| --- | --- | --- | --- |
| **SEV-1** | Outage or data/security incident: API unavailable, data exposure, billing corruption, compromised credentials | Page immediately; all hands | Every 30 min |
| **SEV-2** | Major degradation: an SLO burning fast, one core feature down (chat, RAG, billing), a provider outage without fallback | Page the on-call engineer | Every 60 min |
| **SEV-3** | Minor degradation or a single tenant affected | Ticket; next business day | Daily |

Alerts labelled `severity: page` start at SEV-2; `ticket` alerts start at SEV-3.
The incident commander can raise or lower the severity at any time.

## Roles

* **Incident commander (IC):** owns the incident, decides, delegates. Does not debug.
* **Operations:** investigates and mitigates using [RUNBOOKS.md](RUNBOOKS.md).
* **Communications:** updates the status page and customers; drafts notices
  for affected tenants.
* **Scribe:** keeps the timeline: alerts, decisions, commands, timestamps.

For a small team, the on-call engineer is the IC until a second person joins.

## Process

1. **Acknowledge** the page within 5 minutes and declare the severity in the
   incident channel.
2. **Stabilise before you fix.** Roll back the last deploy
   (`kubectl rollout undo`), fail over (`READ_ONLY_MODE`), shed load (Cloudflare
   rules), or disable a feature or alias. The deploy pipeline makes every
   rollback a known digest.
3. **Security incidents** (exposure, compromised credentials, forged webhooks
   that were accepted):
   1. Preserve evidence. The audit log is append-only; export it.
   2. Rotate secrets: Terraform `-replace` on the generated secrets, plus
      provider keys.
   3. Revoke Supabase sessions and API keys.
   4. Assess the notification duties for personal data (GDPR: 72 h).
4. **Communicate** on the schedule in the table above, even when there is
   nothing new.
5. **Resolve** once the SLIs are back within target for 30 minutes.
6. **Review:** write a blameless post-mortem within 5 business days for SEV-1
   and SEV-2.

## Post-mortem template

```
Title / severity / duration / incident commander
Impact: users, tenants, requests and SLO error budget consumed; data affected
Timeline (UTC): detection -> mitigation -> resolution
Root cause and contributing factors
What went well / what went poorly / where we got lucky
Action items: owner, due date, tracking issue (prevent / detect / mitigate)
```

## Readiness reviews

Review this process and the runbooks:

* before each production release, as part of the release gate checklist;
* after every SEV-1 or SEV-2;
* at least quarterly, in a game day that replays one chaos experiment and one
  recovery drill (`docs/PRODUCTION_VERIFICATION.md`).
