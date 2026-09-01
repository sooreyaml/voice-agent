# Plan: instant number on signup

> **Status: implemented** on branch `feat/instant-onboarding` (migrations `0018`
> pool + lifecycle, `0019` drops the onboarding workflow tables). Signup claims a
> pool number + publishes a default agent + opens the Stripe signup checkout; the
> worker runs the reaper, dunning, pool-refill, and usage-export jobs; the whole
> admin/self-service onboarding + telephony-provisioning surface and admin billing
> CRUD are deleted; `/api/v1/admin/*` is now a read-only operator overview.
>
> **Later change: the free trial was dropped.** Checkout captures the card and
> starts the paid subscription immediately — no `TRIAL_PERIOD_DAYS`, no `trialing`
> lifecycle state. Lifecycle is `provisioning → active` (+ `suspended` / `closed`).
> References to a trial below are the original design and are superseded.
>
> This document is kept as the design record.

## The flow we actually want

1. A user registers (email + password + card).
2. The moment registration succeeds they **already have a live phone number** and a
   working default receptionist agent on it. They can call it and test immediately.
3. They then edit the agent — business identity, greeting, hours, FAQs, transfer
   target, integrations — and keep testing by calling their own number.
4. The card was captured at signup. A **3-day free trial** runs, then the
   subscription **auto-charges** and continues. No separate "convert" step.
5. Everything downstream is unchanged: transcripts, leads, summaries, webhooks,
   CRM/calendar sync.

The user never brings their own number and never picks one. The platform owns
every number and assigns one automatically.

## What this removes from today's design

Today a new tenant has to walk a state machine before the phone works:
`start onboarding → save profile draft → search numbers → select into draft →
approve purchase → publish → make a real test call → verify → active`
(`app/domains/onboarding/`, `app/domains/telephony/`, `onboarding_records`,
`telephony_provisioning_requests`).

Under this plan the common path skips all of it:

| Today | After |
| --- | --- |
| `POST /organizations/{id}/onboarding` to begin | nothing — signup does it |
| search / select / approve / purchase a number | pool number claimed in-process at signup |
| `publish` to go live | a default profile is auto-published at signup |
| `verify-test-call` to activate | number is live at once; "testing" is just calling it |
| admin-led onboarding (`/admin/onboarding/*`) | **deleted** — admin never opens accounts for customers |
| self-service `/onboarding/telephony/provision` | **deleted** for v1 (revisit if "add a second number" is ever needed) |
| per-org telephony provisioning (`/admin/onboarding/{id}/telephony/*`) | **deleted** — replaced by the pool-refill job |

`onboarding_records` stays as a lightweight "have they finished editing their
agent" flag; it is no longer a gate on routing.

## Admin / platform-operator surface

**Principle: admin is a read-only watchful presence over the whole platform. It
never acts on a customer's behalf.** Everything a customer needs, the customer
does through signup and their own dashboard/API. A stuck customer is helped by
support reading logs and the database — not by an operator doing the task in a
panel.

**Kept (view-only, platform-wide), gated by `is_platform_admin`:**

- Every organization: lifecycle (`provisioning`/`trialing`/`active`/`suspended`/
  `closed`), signup date, owner email, assigned number.
- Billing across the platform: MRR, trialing vs active counts, trial→paid
  conversion, failed payments, orgs near their spend limit.
- Usage: calls and spend this month, per org and platform total.
- Number-pool health: `available`/`assigned`/`quarantined` counts, refill alerts.

Today's `GET /admin/organizations[/{id}]` is the thin start of this; the rest are
new read endpoints.

**Deleted:**

| Endpoint(s) | Why it was there | Why it goes |
| --- | --- | --- |
| `POST /admin/onboarding`, `PUT/POST /admin/onboarding/{id}/profile*`, `.../publish` | Phase 4: a human made each tenant before signup existed | the customer signs up and edits their own agent |
| `/admin/onboarding/{id}/telephony/*` (search / provision / verify) | Phase 5: a human provisioned each number | signup pulls a ready number from the pool |
| `POST/PATCH /admin/billing/plans` | tenants can't invent pricing | one plan, seeded from env at startup (see Config) |
| `POST /admin/billing/usage-events`, `.../usage-exports/stripe` | break-glass finance; worker already auto-exports | rare enough to be a `scripts/` lever, not an API |

`is_platform_admin` + `require_platform_admin` stay as the gate on the view
screens. `scripts/grant_platform_admin.py` stays (bootstrap the first operator).
`onboarding_records`, `telephony_provisioning_requests`, the admin/self-service
onboarding routers, and their tests are removed in the same change.

## Why a pre-warmed pool

Buying a number from Twilio takes seconds and can need a regulatory bundle.
"Instant at the click of Register" is only possible if the number is **already
bought and attached to the shared SIP trunk** before the user arrives. So we keep
a small stock of ready numbers and hand one out with a single DB update on the
signup path — zero Twilio calls while the user waits.

- **US local numbers only for v1** (NANP, no regulatory bundle). Other countries
  come later behind a request flow.
- Pool target size is small (e.g. 10) because each idle number costs ~$1.15/mo.
- A worker job refills the pool back to target and raises an alert if it can't.

## Data model (migration `0018`)

### `phone_number_pool`
Numbers the platform has bought and wired to the trunk but not yet given to a
tenant.

| column | notes |
| --- | --- |
| `id` | uuid |
| `e164` | unique |
| `country_code` | `US` for v1 |
| `provider` | `twilio` |
| `provider_number_sid`, `provider_trunk_sid` | from the warm-up purchase |
| `status` | `available` \| `assigned` \| `quarantined` \| `retired` |
| `assigned_organization_id` | nullable FK |
| `assigned_at`, `quarantined_until` | timestamps |
| `created_at`, `updated_at` | |

Claiming is atomic:
`UPDATE phone_number_pool SET status='assigned', assigned_organization_id=?, assigned_at=?
 WHERE id = (SELECT id FROM phone_number_pool WHERE status='available'
 ORDER BY created_at LIMIT 1 [FOR UPDATE SKIP LOCKED]) RETURNING e164, ...`
via `Store.execute_returning` (works on SQLite ≥3.35 and Postgres).

Releasing:
- Number **never took a live call** (abandoned signup) → straight back to
  `available`.
- Number **was ever active on a real call** → `quarantined` with
  `quarantined_until = now + QUARANTINE_DAYS` (default 30) so a new tenant does
  not inherit someone's published number; the refill job promotes it back to
  `available` after that, or `retired` (released to Twilio) if we are over target.

### `subscriptions` (existing table, no schema change)
Already has `status` (`not_started/checkout_pending/trialing/active/past_due/…`),
`trial_end`, `provider_customer_id`, `provider_subscription_id`. We start using it
from signup instead of only from `POST /billing/checkout`.

### `organizations` — add `lifecycle` (or reuse onboarding status)
`provisioning` → `trialing` → `active` → `suspended` → `closed`. Drives the
inbound-call gate and the reaper. Could also live on `onboarding_records.status`;
decide during build.

## Signup change (`app/domains/auth/service.py::signup` + router)

`POST /api/v1/auth/signup` body gains nothing required beyond today (email,
password, organization_name). In one DB transaction:

1. create user + organization + `owner` membership (as today);
2. **claim a pool number** (atomic update above). If the pool is empty, see
   *Pool exhausted* below;
3. **publish a default agent profile** for the org with that number `active`
   — reuse `BusinessRepository.publish()` with a genericised template
   (`businesses/_default.yaml`, no real business data; greeting like
   *"Thanks for calling. How can I help you today?"*, timezone
   `America/New_York`, no transfer number);
4. insert a `subscriptions` row `status='incomplete'`, `billing_plan_id` = the
   configured default plan.

Then, outside the transaction, if `BILLING_ENABLED`:

5. create a Stripe **Customer** (email = owner) and a Stripe **Checkout Session**
   in `subscription` mode with `subscription_data.trial_period_days=3` and
   `payment_method_collection='always'`; store `provider_customer_id`.

Response (superset of today's `SignupResponse`, so existing callers keep working):

```json
{
  "user": {...},
  "organization": {...},
  "phone_number": "+1XXXXXXXXXX",
  "subscription": { "status": "incomplete", "trial_ends_at": null },
  "checkout_url": "https://checkout.stripe.com/..."   // null when BILLING_ENABLED=false
}
```

The number is **already routable** at this point — the user can call it and reach
the default agent before they ever open the Stripe page.

`BILLING_ENABLED=false` (dev / self-host): skip steps 5; set the subscription row
to `active` (or leave it absent) and never run the billing reaper.

## Stripe webhook handling (`app/domains/billing/.../webhooks.py`)

Add handlers (idempotent via the existing `billing_provider_events` receipt):

| Stripe event | effect |
| --- | --- |
| `checkout.session.completed` | subscription → `trialing`, store `provider_subscription_id`, `trial_end = now + 3d`; org `lifecycle='trialing'` |
| `customer.subscription.updated` | mirror Stripe status → our `status`; on `active` set org `lifecycle='active'`; on `past_due`/`unpaid` start dunning |
| `customer.subscription.deleted` | subscription → `canceled`; org `lifecycle='suspended'` |
| `invoice.paid` | clear dunning; ensure org `lifecycle='active'` and number re-activated |
| `invoice.payment_failed` | mark `past_due`; dunning timer starts |

## Worker jobs (`app/worker.py` — new tickers)

1. **Signup reaper.** `subscriptions.status='incomplete'` and
   `organization.created_at < now - SIGNUP_CHECKOUT_GRACE_HOURS` (default 24):
   release the pool number (never-used → `available`), archive the published
   agent version so the number stops routing, set org `lifecycle='closed'`,
   email the user "we released your trial number, sign up again to retry".
2. **Dunning / suspend.** `status='past_due'` beyond `DUNNING_GRACE_DAYS`
   (default 7): set org `lifecycle='suspended'` and deactivate its `phone_numbers`
   (agent stops answering). `invoice.paid` reverses it.
3. **Pool refill (infrastructure, not admin).** Keep `phone_number_pool`
   `available` count ≥ `NUMBER_POOL_TARGET`: buy + trunk-attach more via
   `TwilioProvisioningService`; promote `quarantined` numbers whose
   `quarantined_until` has passed; alert (log + `NOTIFY_WEBHOOK_URL`) if a buy
   fails or the pool is empty with signups waiting.

All three follow the existing claim/backoff/dead-letter pattern
(`FOR UPDATE SKIP LOCKED` on Postgres, single instance on SQLite).

## Inbound-call gate (`app/main.py::openai_webhook`)

Already rejects unknown / inactive numbers and checks
`spend_service.call_is_allowed`. Add: reject with SIP 402 when the org
`lifecycle` is `suspended`/`closed` (defence in depth — the number is also
deactivated in those states, so this is belt-and-braces).

## Config (`app/settings.py` + `.env*.example`)

| var | default | purpose |
| --- | --- | --- |
| `DEFAULT_BILLING_PLAN_CODE` | `starter` | plan the signup subscription uses |
| `TRIAL_PERIOD_DAYS` | `3` | passed to Stripe |
| `NUMBER_POOL_TARGET` | `10` | refill target |
| `NUMBER_POOL_COUNTRY` | `US` | v1 is US-only |
| `SIGNUP_CHECKOUT_GRACE_HOURS` | `24` | reaper window for un-paid signups |
| `DUNNING_GRACE_DAYS` | `7` | past-due → suspend |
| `NUMBER_QUARANTINE_DAYS` | `30` | before a used number is re-offered |

`scripts/warm_number_pool.py <count>` — one-off / cron to pre-buy and attach
numbers (wraps `TwilioProvisioningService`, writes `phone_number_pool` rows).

## Edge cases and failure modes

- **Pool empty at signup.** Account + org are still created; response has
  `phone_number: null`, `subscription.status: "number_pending"`. Pool-refill job
  assigns one when stock returns and emails the user. Loud ops alert. (We do not
  fail the signup — instant account beats a 503.)
- **Stripe down at signup.** Steps 1–4 committed, number live, subscription
  `incomplete`, `checkout_url: null`, response flag `checkout_retry: true`;
  `POST /billing/checkout` (exists today) is the retry. Reaper still cleans up if
  never paid.
- **User abandons the Stripe page.** Reaper reclaims the number after the grace
  window.
- **Card declined at trial end.** `invoice.payment_failed` → `past_due` → dunning
  → suspend; `invoice.paid` restores.
- **Two signups race for the last number.** Atomic conditional update +
  `SKIP LOCKED` — the loser gets the pool-empty path.
- **Number recycling.** Never-used numbers recycle immediately; used numbers are
  quarantined so old callers do not hit a stranger's business.
- **Timezone.** Auto-assigned as `America/New_York`; the user corrects it when
  they edit the profile. (No reliable server-side locale without asking.)
- **Abuse (free numbers before payment clears).** Bounded: pool is small,
  `SIGNUP_CHECKOUT_GRACE_HOURS` is short, email verification still applies, add a
  per-IP signup rate limit. Accept the residual risk — instant is the point.
- **`seed_organization`** keeps working unchanged (it publishes a profile and
  attaches a number directly) — useful for local dev and a one-off manual tenant.
  The admin/self-service *onboarding* routers are deleted.

## Blast radius

- `signup()` return shape changes — keep it a **superset**; update
  `tests/test_auth.py` and `SignupResponse`.
- `BILLING_ENABLED=false` must still produce a usable tenant with a number.
- Every org now has a published profile from minute one → `list_published`,
  `/health`, and `find_by_phone_number` all see more rows (fine).
- Three features most likely to regress: **signup** (transaction now does much
  more — partial failure must roll back the number claim), **inbound routing**
  (default profile must render a valid prompt), **billing checkout** (now created
  in two places — factor the Stripe-session code into one helper).

## Smallest shippable slice

Ship this first, end-to-end, before dunning/quarantine polish:

1. Migration `0018`: `phone_number_pool` + `organizations.lifecycle`.
2. `businesses/_default.yaml` + `scripts/warm_number_pool.py`.
3. `signup()`: claim number → publish default profile → create subscription +
   Stripe trial checkout; extended `SignupResponse`.
4. Stripe `checkout.session.completed` + `customer.subscription.updated` →
   `trialing`/`active`.
5. Signup reaper job (un-paid → release number).
6. Tests: signup gives a routable number (fake realtime call reaches the default
   agent); reaper releases an abandoned number; `BILLING_ENABLED=false` signup
   still yields a working number; pool-empty signup degrades to `number_pending`.

Then, as follow-ups: dunning/suspend, quarantine + auto-refill, per-IP signup
limit, the platform-operator overview endpoints (orgs / billing / usage / pool
health), delete the admin + self-service onboarding routers and their tests,
country expansion.

## Open questions

1. The single billing plan is seeded/updated from env at startup when
   `BILLING_ENABLED` (`STRIPE_PRICE_ID`, `STRIPE_METER_EVENT_NAME`,
   `DEFAULT_BILLING_PLAN_CODE`); startup asserts it resolves. Confirm this over a
   data migration.
2. Trial with a card on file: charge immediately at day 3, or send an invoice
   with a short pay window? (Default: Stripe auto-charge.)
3. When a paying customer churns, do we recycle the number after quarantine or
   always release it to Twilio?
4. Teams: keep invitations + org roles (recommended — already built, invisible to
   solo users) or flatten to single-user orgs?
