# Billing and Stripe (Phase 14)

Stripe is the source of truth for money and subscription state; Django owns
plans, entitlements, credits and the immutable ledger (see
[USAGE_METERING.md](USAGE_METERING.md)). Card data never reaches this API.

## Plans and entitlements

Migration `billing.0006` seeds `free`, `pro`, `team` and `business` (the
frontend's plan slugs). Each plan has monthly/yearly prices, `monthly_credits`,
hard/soft/unlimited `Entitlement` quotas per feature, and `limits`
(`max_concurrent_*`, `rate_multiplier`) used by metering and rate limiting.
**The seeded prices, credits and caps are defaults to review**; edit them in
the Django admin (existing rows are never overwritten by migrations).

Unsubscribed organizations use `BILLING_DEFAULT_PLAN` (`free`); the
`grant_free_plan_credits` task gives them the free plan's credits once per
calendar month.

## Setting up Stripe

1. Set `STRIPE_SECRET_KEY` (`sk_…`/`rk_…`), `STRIPE_PUBLISHABLE_KEY`,
   `STRIPE_WEBHOOK_SECRET` (`whsec_…`) and `FRONTEND_URL` (https in deployed
   environments). `STRIPE_API_VERSION` is pinned (`2023-10-16`).
2. Run `python manage.py sync_stripe_prices` — it creates (or links, by lookup
   key `jt-code-<slug>-<interval>`) a Product and monthly/yearly Prices per
   paid plan and stores their ids on the plan. Re-run after changing prices;
   existing subscribers keep their old price.
3. Point a Stripe webhook endpoint at `POST /api/v1/webhooks/stripe/` with the
   events: `checkout.session.completed`, `customer.subscription.*`,
   `invoice.paid`, `invoice.payment_succeeded`, `invoice.payment_failed`,
   `invoice.finalized`, `invoice.updated`, `invoice.voided`,
   `invoice.marked_uncollectible`, `payment_intent.succeeded`,
   `payment_intent.payment_failed`, `charge.refunded`, `setup_intent.succeeded`.
4. Enable the Stripe Billing Portal (used by `POST /api/v1/billing/portal/`).

## Webhooks: verification and idempotency

- Signatures are verified by the Stripe SDK with a
  `STRIPE_WEBHOOK_TOLERANCE_SECONDS` timestamp window (replay protection).
- Each event is stored once in `StripeEvent` (unique `event_id`) and processed
  under that row's lock; redeliveries and concurrent duplicates return
  `{"duplicate": true}` and change nothing.
- Subscription events re-read the subscription from Stripe and apply it, so
  delayed or reordered events cannot regress state; an older snapshot than the
  one already applied is ignored.
- A handler failure rolls back its partial work, marks the event `failed` and
  returns 500 (Stripe redelivers); `retry_failed_stripe_events` also retries
  every 5 minutes up to `STRIPE_EVENT_MAX_ATTEMPTS`.

## Reconciliation (`reconcile_stripe_billing`, hourly)

1. **Missed events:** lists Stripe events from the last 72 hours and
   processes any that were never delivered.
2. **Subscriptions:** re-applies every customer's Stripe subscriptions, so
   local status, plan, period and cancellation always converge on Stripe.
3. **Pending payments:** asks Stripe about top-ups still pending after 30
   minutes and settles or fails them.

## Policy

- **Subscriptions:** Checkout only (no proration endpoint). Credits are granted
  per *paid* invoice — `monthly_credits` for monthly plans, ×12 for yearly —
  only for `subscription_create`/`subscription_cycle` invoices, once per
  invoice. Cancellation is at period end (`POST /subscriptions/cancel/`) and
  can be undone before then (`/subscriptions/reactivate/`). Plan changes,
  payment methods and invoice history are also available in the Billing
  Portal. Granted credits do not expire.
- **Top-ups:** `POST /wallets/me/topup/` creates a PaymentIntent for
  `BILLING_TOPUP_MIN_CENTS`–`BILLING_TOPUP_MAX_CENTS`. The credit amount is
  computed at purchase (`amount / BILLING_CREDIT_VALUE_USD`) and stored on the
  intent, so later price changes never alter a paid top-up. Credits are
  granted only when Stripe confirms the payment.
- **Auto top-up:** with a saved default payment method (added through a
  SetupIntent from `POST /payment-methods/`), `run_auto_topups` charges
  `auto_topup_amount` off-session when the available balance drops below the
  threshold, at most once per `BILLING_AUTO_TOPUP_COOLDOWN_MINUTES`.
- **Refunds:** issued in Stripe. On `charge.refunded`, top-up credits are
  reversed in proportion to the refunded amount, never below the available
  balance; any shortfall is recorded on the payment (`unreversedCredits`) for
  follow-up. Subscription refunds do not claw back credits automatically.
- **Invoices:** mirrored from Stripe (`/invoices/`); PDFs are streamed from
  Stripe's hosts only (`/invoices/{id}/download/`).
- **Spending limit:** `PATCH /wallets/me/` `monthly_spending_limit` caps
  credits charged + held per month (enforced by metering).

## API (frontend contract)

| Method & path | Purpose |
| --- | --- |
| `GET /plans/`, `GET /plans/{slug}/` | Plan catalogue (camelCase) |
| `POST /plans/{slug}/subscribe/` | `{interval, successUrl?, cancelUrl?}` → `{checkoutUrl, sessionId}` |
| `GET /subscriptions/` | Subscriptions with `plan`, `isActive`, `daysRemaining` |
| `POST /subscriptions/cancel/`, `/subscriptions/reactivate/` | Cancel at period end / undo |
| `GET, PATCH /wallets/me/` | Wallet; auto top-up and spending-limit settings |
| `POST /wallets/me/topup/` | `{amount_cents}` → `{clientSecret, paymentIntentId, credits}` |
| `GET /payment-methods/me/`, `POST /payment-methods/` | Default card summary / SetupIntent |
| `POST /billing/portal/` | Stripe Billing Portal URL |
| `GET /invoices/`, `/invoices/{id}/`, `/invoices/{id}/download/` | Invoices (array) and PDF |
| `GET /payments/`, `GET /ledger/` | Payments and the credit ledger |
| `POST /webhooks/stripe/` | Signed Stripe events |

Return URLs must be on `FRONTEND_URL` or a `CORS_ALLOWED_ORIGINS` origin.
Note for the frontend: subscribe returns a Checkout URL to redirect to, top-up
and payment-method calls return Stripe client secrets for Stripe Elements, and
raw card fields are rejected.
