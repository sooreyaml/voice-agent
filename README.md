# Call Agent

A voice agent that answers your business phone line. It greets the caller, answers
questions about the business from a single config file, works out what the caller
needs, records it, and can transfer to a human.

## How it works

```
Caller
  └─► Twilio phone number
        └─► Twilio Elastic SIP Trunk
              └─► OpenAI Realtime API  ◄── speaks to the caller directly
                    └─► webhook to this server ── accepts the call, supplies the
                        (sideband WebSocket)     company context and the tools
```

The important part: **audio never passes through this server.** OpenAI handles the
speech directly over SIP. This server only holds a text-only control channel, which
is where tool calls, the transcript and the post-call summary happen. That means it
stays idle during a call and runs comfortably on the cheapest VPS you can rent.

## What a call costs

| Item | Rate |
| --- | --- |
| Twilio US local number | $1.15 / month |
| Twilio inbound minutes | ~$0.0085 / min |
| `gpt-realtime-2.1-mini` | ~$0.02–0.05 / min |
| Caller transcription (optional) | ~$0.006 / min |
| Post-call summary | fractions of a cent per call |

Roughly **$0.03–0.06 per minute all in**, so a typical three minute call costs
about 10–18 cents. The dominant cost is the agent *talking*: output audio bills at
four times the rate of input audio. The prompt in `app/business.py` therefore pushes
hard for one-to-two sentence turns. Resist the urge to give the agent a chatty
persona — it directly inflates the bill.

Every call's actual model cost is computed from the reported token usage and stored
against the call record, so you can see real numbers rather than estimates.

## Setup

### 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`requirements.in` holds the direct dependencies; `requirements.txt` is the pinned
lockfile generated from it with `pip freeze`.

### 2. OpenAI account

1. Sign up at [platform.openai.com](https://platform.openai.com) and add credit
   (start with $10 — that is a lot of test calls).
2. **API keys** → create a key → put it in `.env` as `OPENAI_API_KEY`.
3. **Settings → Project → General** → copy the project id (`proj_...`) into
   `.env` as `OPENAI_PROJECT_ID`. This is what the SIP trunk will dial.

### 3. Resend email

Create an API key at [resend.com/api-keys](https://resend.com/api-keys), verify
the domain you will send from, then set `RESEND_API_KEY` and
`RESEND_FROM_EMAIL` in `.env`. Resend delivers email-verification,
password-reset, organization-invitation, and opted-in post-call summary emails.
For a post-call email, add `business.notify_email` to the published business
profile. In development, missing Resend settings keep the auth and invitation
links in the application log.

### 4. Expose the server

The webhook needs a public HTTPS URL, so start the server and a tunnel:

```bash
uvicorn app.main:app --port 8000 --reload
```

In a second terminal:

```bash
ngrok http 8000
```

Copy the `https://....ngrok-free.app` URL it prints.

### 5. Register the webhook

In **Settings → Project → Webhooks**, create a webhook:

- URL: `https://YOUR-NGROK-URL/openai/webhook`
- Event: `realtime.call.incoming`

Copy the signing secret into `.env` as `OPENAI_WEBHOOK_SECRET`, then restart the
server. Make sure the webhook lives in the **same project** as the project id above,
or the call will arrive and nothing will happen.

### 6. Twilio number and trunk

Sign up at [twilio.com](https://www.twilio.com/try-twilio) and upgrade from trial
(trial accounts restrict inbound calling). Put `TWILIO_ACCOUNT_SID` and
`TWILIO_AUTH_TOKEN` in `.env`, then:

```bash
python scripts/twilio_setup.py search --country US --area-code 617
python scripts/twilio_setup.py provision --country US --area-code 617
```

That buys a number, creates an Elastic SIP Trunk, points the trunk's origination URI
at your OpenAI project, and attaches the number to the trunk. Verify any time with
`python scripts/twilio_setup.py show`.

Outside the US and Canada, use `--contains` rather than `--area-code`, because
Twilio's area-code filter only covers the North American Numbering Plan and is
ignored elsewhere — you would get a number in an arbitrary city. Match the dialling
prefix instead:

```bash
python scripts/twilio_setup.py search --country GB --contains +4420   # London
```

For UK and most European numbers, Twilio requires a verified address and a
regulatory bundle before it will sell you a number. Approval takes a day or two, so
if you just want to test today, buy a US number.

### 7. Preflight

```bash
python scripts/doctor.py
```

This verifies the API key actually works, the realtime model is available to it,
the project id is well formed, the canonical business template parses, and — the part that
catches most first-call failures — that a Twilio trunk really is routing your
number to your OpenAI project. It also flags a number listed in a business config
that you do not actually own. It never prints secrets, only whether they work.

### 8. Call it

Dial the number. You should hear the greeting from its published business profile. The
server logs each turn as it happens. Afterwards:

```bash
curl localhost:8000/calls | python -m json.tool
```

You get the transcript, what the caller wanted, and the exact model cost.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

A full call runs offline. `tests/fake_realtime.py` is a stand-in Realtime server
that replays the same event sequence a real SIP call produces — greeting, caller
transcripts, a tool call, then `end_call` — and records everything the agent sends
back. So the parts that are awkward to test against a live phone line (tool results
being returned in the right shape, the model getting the floor back afterwards,
hanging up without clipping the goodbye, a carrier refusing a transfer) are covered
without spending a cent.

`.github/workflows/security.yml` also audits the locked runtime dependencies with
`pip-audit` and runs the tenant-isolation regression matrix on every pull request.

What is *not* covered: the SIP leg itself and the real `/accept` payload. Those need
live credentials, so the first real call is still the first real test.

## Deploying

`Dockerfile` and `railway.json` deploy this to Railway; any host that runs a
container and hands you a `$PORT` works the same way.

### Coolify

Coolify already owns host ports 80 and 443 and supplies the HTTPS reverse proxy.
Assign the public domain to the `app` service using container port `8000` (for
example, `https://voice.example.com:8000` in the service domain field), then deploy
with the default Compose configuration. Do not enable the `standalone-proxy` profile
in Coolify. The app container runs the bounded, idempotent pending-number backfill
before Uvicorn starts, so older signups are repaired during the next deployment.

The API, worker, PostgreSQL, and Redis remain on the private deployment network;
only the domain assigned to `app` is routed through Coolify's proxy. OpenAI webhook
signatures, application sessions/API keys, Stripe signatures, and the dedicated
seed bearer token are still validated by the application.

### Standalone VPS with Docker Compose

The included `compose.yaml` runs the API, worker, PostgreSQL for durable data,
Redis for short-lived shared coordination, and—when explicitly enabled—Caddy for
automatic HTTPS. Only Caddy publishes ports 80 and 443; the app, worker, database,
and Redis stay on the private Compose network. Caddy leaves the signed OpenAI
webhook and the application-token-protected seed route reachable without browser
basic auth, and password-protects every other HTTP route.

Point a DNS record such as `voice.example.com` at the VPS, then on the server:

```bash
git clone https://github.com/Shapati/call-agent.git
cd call-agent
cp .env.production.example .env
openssl rand -hex 32
docker run --rm -it caddy:2-alpine caddy hash-password
```

Put the generated values and the real credentials in `.env`, including a unique
`SEED_API_TOKEN`. Keep the Caddy hash single-quoted because bcrypt hashes contain
`$`, which Compose otherwise tries to interpolate. Then start the stack. The API
starts successfully with an empty database; until an organization is seeded,
unknown inbound numbers are declined rather than routed to a placeholder profile.

```bash
docker compose --profile standalone-proxy up -d --build
docker compose --profile standalone-proxy ps
docker compose --profile standalone-proxy logs -f app worker caddy
```

`compose.yaml` also starts a `worker` service (`python -m app.worker`) that
delivers outbound webhooks. It needs no inbound ports.

Register the production OpenAI webhook as
`https://<APP_DOMAIN>/openai/webhook`, subscribed to
`realtime.call.incoming`. Verify the private API using the username and the
plain-text password from which the Caddy hash was made:

```bash
curl -u admin https://<APP_DOMAIN>/health
docker compose run --rm app python scripts/doctor.py
```

Business profiles are published database records and each call uses the currently
published immutable version. Application code changes deploy with:

```bash
git pull --ff-only
docker compose --profile standalone-proxy up -d --build
```

Back up the `postgres_data` volume (preferably with regular `pg_dump` jobs).
Webhook idempotency, API-key rate limits, and active-call heartbeats are shared
through Redis, so multiple API replicas can run. Active calls remain attached to
the process that answered them, so deployments must gracefully drain old replicas.

### Clearing production application data

`scripts/clear_database.py` can preview or permanently truncate every application
table while preserving the schema and Alembic revision. It refuses SQLite,
localhost, PostgreSQL system databases, the wrong migration revision, and any
unknown table. It does not release Twilio numbers, cancel Stripe resources, remove
OpenAI trunk associations, or disconnect external CRMs.

Use the manual **Clear production database** GitHub Action. Run it in `preview`
mode first. A `clear` run additionally requires the exact phrase
`CLEAR PRODUCTION DATABASE <database-name>`, a verified Coolify/S3 backup
reference, acknowledgment of external resources, and approval from the protected
`production` GitHub Environment. Configure that environment with these secrets:

- `DATABASE_URL`: the production PostgreSQL connection URL;
- `COOLIFY_DEPLOY_WEBHOOK`: the authenticated deployment webhook URL for this
  Compose application;
- `COOLIFY_TOKEN`: a Coolify API token with only the `deploy` permission.

The production database is private by default. Use a self-hosted GitHub runner
that can reach its Compose network and set the repository variable
`PRODUCTION_DB_RUNNER` to that runner's label. An `ubuntu-latest` runner works only
when `DATABASE_URL` is intentionally reachable from GitHub. After clearing, the
workflow requests a Coolify redeploy so the API and worker discard in-memory state.

The same script can be previewed from the Coolify app terminal without deleting
anything:

```bash
python scripts/clear_database.py --expected-database callagent
```

### Railway

```bash
railway up --service callagent
```

Two things matter once it is not your laptop:

- **`DATABASE_URL`.** A container's disk is wiped on every deploy, so SQLite would
  quietly lose call history. Set `DATABASE_URL` to a Postgres URL and the store
  switches over; `GET /health` reports which backend is live under `storage`.
  Railway resolves `${{Postgres.DATABASE_URL}}` to the attached database. Alembic
  upgrades run at service startup; existing single-business call data is preserved
  and assigned to a legacy organization during the first tenant migration.
- **`REDIS_URL`.** Required in staging and production. It holds expiring webhook
  claims, distributed API-key counters, and active-call heartbeats; durable jobs
  and customer data remain in PostgreSQL.
- **The webhook URL.** Ngrok is only for local development. Register the deployed
  `https://<your-domain>/openai/webhook` with OpenAI instead, and put the signing
  secret it gives you in `OPENAI_WEBHOOK_SECRET`.

The server refuses to start without `OPENAI_API_KEY` and
`OPENAI_WEBHOOK_SECRET`. Outside development it also requires
`AUTH_SESSION_SECRET`, `INTEGRATION_ENCRYPTION_KEY`, and `REDIS_URL`.

## Installing this for a business

The normal path is: the business signs up (`POST /api/v1/auth/signup`), gets a
live number purchased from Twilio and a default agent immediately, then customises that
agent through the `agent` endpoints — `PUT …/agent/draft` to stage a full
replacement configuration, `POST …/agent/publish` to make it answer the next
call. `businesses/_default.yaml` is that starting profile;
`businesses/harborview-dental.yaml` is a fuller worked example. Neither is read
while handling a call — every organization has its own published immutable
version in the database. The assigned number itself is not editable through the API;
it is pinned to whatever the org was assigned at signup.

`scripts/seed_organization.py` (and the **Seed organization** GitHub Action) stay
as an operator escape hatch for creating an organization directly against a number
you have already attached to the trunk — useful for local dev and one-off manual
tenants, not the customer path.

For hosted environments, create matching `staging` and/or `production` GitHub
Environments with:

- environment variable `APP_BASE_URL`, such as `https://voice.example.com`;
- secret `SEED_API_TOKEN`, matching the value deployed in the app's `.env`;
- secret `SEED_OWNER_PASSWORD`, a temporary initial owner password (at least 10
  characters); replace it before provisioning each new owner and have them change it
  after first sign-in.

Then open **Actions → Seed organization → Run workflow** and enter the business
identity and inbound E.164 number. The workflow calls the dedicated bearer-protected
provisioning route over HTTPS; it does not need database network access or Caddy's
browser basic-auth credentials. Rerunning it is safe: unchanged profiles keep their
existing version, and an owner who changed their password keeps the changed password.

For local or server-side provisioning, run the equivalent CLI (it prompts for the
initial password):

```bash
python scripts/seed_organization.py \
  --organization-name "Acme Dental Group" \
  --organization-slug acme-dental \
  --owner-email owner@acme.example \
  --business-name "Acme Dental" \
  --phone-number +442071234567 \
  --business-description "A private family dental practice"
```

The seed overrides the template's business identity, routing number, agent greeting,
and contact identity. Its hours, services, FAQs, knowledge, and guardrails initially
come from the template; update those through the management API before taking live
calls when the new business differs. Unknown numbers are always declined at the SIP
level rather than answered as the wrong organization.

The structured sections (`hours`, `services`, `faqs`, `contact`) are rendered into
the prompt in a consistent shape. Anything that does not fit goes in the free-prose
`knowledge` block, which is passed through verbatim — write that the way you would
brief a new receptionist on their first morning.

The agent is also told the current local time in the business's timezone on every
call, so it can answer "are you open right now?" correctly.

Keep it factual and short. Anything not in the profile, the agent is instructed to admit
it does not know and offer to take a message — which is the behaviour you want,
because a voice agent confidently inventing a price is far worse for the client than
one that says "let me have someone call you back".

## Endpoints

### Call runtime (public)

| Route | Purpose |
| --- | --- |
| `POST /openai/webhook` | Where OpenAI delivers `realtime.call.incoming` |
| `GET /health` | Status, loaded businesses, storage backend, and the SIP URI to configure |
| `POST /api/v1/operations/seed-organization` | Idempotent GitHub provisioning (`SEED_API_TOKEN` bearer auth) |

### Management API (`/api/v1`, cookie-authenticated)

Call and lead history now lives behind a login. Sessions are HTTP-only cookies;
every state-changing request must echo the `csrf` cookie in an `X-CSRF-Token`
header (obtain the cookie from any GET, e.g. `GET /api/v1/ping`). Errors use one
envelope: `{"error": {"code", "message", "field_errors", "request_id"}}`.

| Route | Purpose |
| --- | --- |
| `GET /api/v1/ping` | Liveness; also seeds the CSRF cookie |
| `POST /api/v1/auth/signup` | Create an account + organization (you become `owner`), purchase or reuse a live number, and publish a default agent. Response carries `phone_number` (and `subscription` / `checkout_url` when `BILLING_ENABLED`) |
| `POST /api/v1/auth/login` / `logout` | Start / end an authenticated session |
| `GET /api/v1/me` | The signed-in user and their organizations + roles |
| `POST /api/v1/auth/verify-email/request` · `/confirm` | Email verification |
| `POST /api/v1/auth/password-reset/request` · `/confirm` | Password reset (revokes all sessions) |
| `GET` · `POST /api/v1/organizations` | List the user's orgs · create a new one (creator is `owner`) |
| `GET` · `PATCH /api/v1/organizations/{id}` | Org profile · rename (admin/owner) |
| `GET /api/v1/organizations/{id}/members` | Members and their roles |
| `PATCH /api/v1/organizations/{id}/members/{user_id}` | Change a role (owner only; last-owner and self-role guarded) |
| `DELETE /api/v1/organizations/{id}/members/{user_id}` | Remove a member (owner), or leave (self) |
| `GET` · `POST /api/v1/organizations/{id}/invitations` | Pending invitations · invite by email + role (admin/owner) |
| `DELETE /api/v1/organizations/{id}/invitations/{invitation_id}` | Revoke a pending invitation |
| `GET /api/v1/invitations/{token}` | Public preview of an invitation |
| `POST /api/v1/invitations/{token}/accept` | Accept as the signed-in user (email must match) |
| `GET /api/v1/organizations/{id}/audit-log[?cursor=&limit=]` | Membership/config change history (admin/owner) |
| `GET /api/v1/organizations/{id}/agent` | The org's live agent + any unpublished draft + edit eligibility (member) |
| `PUT /api/v1/organizations/{id}/agent/draft` | Save a full replacement configuration as a draft; live calls unaffected (admin/owner) |
| `GET · DELETE /api/v1/organizations/{id}/agent/draft` | Preview the draft with its rendered prompt · discard it (delete: admin/owner) |
| `POST /api/v1/organizations/{id}/agent/publish` | Publish the draft — the next call uses it (admin/owner) |
| `GET /api/v1/organizations/{id}/calls[?cursor=&limit=]` | Cursor-paginated call history (member cookie **or** `calls:read` key) |
| `GET /api/v1/organizations/{id}/calls/{call_id}` | One call with its transcript (`calls:read`) |
| `GET /api/v1/organizations/{id}/leads[?cursor=&limit=]` | Cursor-paginated captured leads (`leads:read`) |
| `PATCH /api/v1/organizations/{id}/leads/{lead_id}` | Set a lead's follow-up status (`new`/`handled`/`dismissed`) — member+ or `leads:write` |
| `GET /api/v1/admin/overview` | Read-only platform metrics: org lifecycle counts, subscription mix, payment failures, this-month calls/cost, number-pool health (platform administrators only) |
| `GET /api/v1/admin/organizations[/{id}]` | Every organization with lifecycle + billing state (platform administrators only) |
| `GET · PATCH /api/v1/organizations/{id}/privacy` | Read · update transcript retention (admin+) |
| `GET · POST /api/v1/organizations/{id}/data-requests/...` | Export data or schedule/cancel owner-confirmed deletion |
| `GET · PUT /api/v1/organizations/{id}/billing/spend-limit` | Current monthly spend · owner-configured hard/soft limit |
| `GET` · `POST /api/v1/organizations/{id}/webhook-endpoints` | List · register a signed outbound webhook endpoint (admin/owner) |
| `GET` · `PATCH` · `DELETE /api/v1/organizations/{id}/webhook-endpoints/{endpoint_id}` | Inspect / update / remove an endpoint |
| `POST /api/v1/organizations/{id}/webhook-endpoints/{endpoint_id}/rotate-secret` | Issue a new signing secret (shown once) |
| `GET /api/v1/organizations/{id}/webhook-endpoints/{endpoint_id}/deliveries[?cursor=&limit=&status=]` | Delivery history for one endpoint |
| `GET /api/v1/organizations/{id}/webhook-deliveries/{delivery_id}` | One delivery with every attempt and its payload |
| `POST /api/v1/organizations/{id}/webhook-deliveries/{delivery_id}/retry` | Re-queue a failed or dead-lettered delivery (admin/owner) |
| `GET /api/v1/organizations/{id}/integrations` | List connected integrations (members; secrets never returned) |
| `GET · PUT · DELETE /api/v1/organizations/{id}/integrations/{provider}` | Inspect · connect/replace · disconnect an integration (mutations: admin/owner). Providers: `cal_com`, `hubspot` |
| `POST /api/v1/organizations/{id}/integrations/{provider}/test` | Re-verify a connection against the provider (admin/owner) |
| `GET · POST /api/v1/organizations/{id}/api-keys` | List · create a scoped API key (admin/owner); the secret is shown once |
| `POST · DELETE /api/v1/organizations/{id}/api-keys/{key_id}[/rotate]` | Rotate (new secret) · revoke a key (admin/owner) |
| `GET /api/v1/billing/*`, `GET·POST .../organizations/{id}/billing/*`, `.../usage`, `POST /webhooks/stripe` | Subscription plan, overview, checkout/portal, usage ledger, Stripe webhook — **only mounted when `BILLING_ENABLED`** |

Roles rank `viewer < member < admin < owner`. Membership is resolved on every
request: a non-member gets `404` for the organization, an unauthenticated caller
gets `401`, and insufficient role gets `403`. List endpoints return
`{"items": [...], "page": {"next_cursor", "has_more"}}`. Interactive docs at
`/docs` are served outside `ENVIRONMENT=production`. `BILLING_ENABLED` defaults to
`false`: the billing/Stripe routes are registered but every one returns `404`,
signup creates no subscription, and hard spend limits are bypassed — internal
usage and call costs are still recorded. Set it `true` (with the `STRIPE_*`
values) to turn billing on.

### Public REST API (scoped API keys)

The calls/leads endpoints above accept either the dashboard session cookie or a
bearer key: `Authorization: Bearer cak_…`. Keys are created through the
cookie-authenticated `api-keys` endpoints (admin/owner), returned once, and
stored only as a keyed hash. Each key carries an explicit scope set —
`calls:read`, `leads:read`, `leads:write` — and is rate-limited per key
(`API_KEY_RATE_LIMIT_PER_MINUTE`, default 120; `429` with `Retry-After` over the
limit). Bearer requests are exempt from the CSRF check (no ambient cookie);
`revoke`/`rotate` take effect immediately.

Grant platform-admin access with `python scripts/grant_platform_admin.py
<email>` (`--revoke` to remove). Platform admin is a read-only overview of the
whole deployment — it never opens accounts or provisions numbers for tenants.
Invitation and auth emails are sent through Resend when configured; development
falls back to logging their links.

### Signup and phone-number provisioning

Registering is the whole onboarding flow. `POST /api/v1/auth/signup` creates the
account and organization, searches and purchases a matching Twilio number on
demand, attaches it to the shared OpenAI SIP trunk, and publishes a generic
default agent (`businesses/_default.yaml`) on it. The number is live when signup
returns; the owner then customises the agent with the `PUT …/agent/draft` +
`POST …/agent/publish` endpoints (draft edits never touch live calls until
published).

`NUMBER_POOL_TARGET=0` keeps pre-warming disabled. The pool table remains the
ownership and recycling ledger, so a released number can be reused before a new
one is purchased. A non-zero target opts in to pre-buying spare numbers with the
worker. If Twilio has a transient failure, the account remains usable and an
owner can retry idempotently with `POST …/agent/provision`.

The app container runs `scripts/provision_pending_numbers.py` before Uvicorn starts,
and Railway additionally runs it as a pre-deploy command. It processes at most ten
older organizations that still have no agent, so the frontend does not need to call
the recovery endpoint for deployment backfills.

`organizations.lifecycle` is `active` from signup (`provisioning → active` only
matters when billing is on).

**Billing is off right now** (`BILLING_ENABLED=false`). Signup creates a working
tenant with no subscription and no card capture. Turning it on (set
`BILLING_ENABLED=true` and the `STRIPE_*` values) makes signup open a Stripe
Checkout Session that captures the card and starts the paid subscription; the
`POST /webhooks/stripe` route drives `provisioning → active → suspended`
transitions and the worker gains the reaper / dunning sweep and the Stripe
usage-meter export.

### Outbound webhooks and the background worker

An organization can register HTTPS endpoints that receive `call.completed`,
`call.transferred`, `lead.created`, and `appointment.booked` events. Each event
has a stable id and is delivered once per subscribed endpoint. A separate process
drains the queue:

```bash
python -m app.worker            # add a `worker` service alongside `app`
```

It shares the database and drains outbound webhook deliveries, post-call CRM
syncs, transcript retention, account export/deletion requests, Stripe usage-meter
export, the signup reaper / dunning sweep, and number-pool refill. Run one against
SQLite; multiple are safe against Postgres
(`FOR UPDATE SKIP LOCKED`). Failed deliveries retry with exponential backoff up
to `WEBHOOK_MAX_ATTEMPTS` (default 6) then dead-letter; staff can inspect every
attempt and re-queue one with the `retry` endpoint.

Requests carry `X-Callagent-Signature: t=<unix>,v1=<hmac_sha256(secret, "t.body")>`
plus `X-Callagent-Event`, `-Event-Id`, `-Delivery-Id`, and `-Delivery-Attempt`.
Verify it (Stripe-style) — recompute the HMAC over `"{t}.{raw_body}"` with the
endpoint secret, `hmac.compare_digest`, and reject timestamps outside ~5 minutes.
`NOTIFY_WEBHOOK_URL` remains as a single, unsigned, no-retry fallback.

### Integrations (Cal.com calendar, HubSpot CRM)

An organization connects a provider through
`PUT /api/v1/organizations/{id}/integrations/{provider}` — `cal_com` with an API
key and event-type id, `hubspot` with a private-app access token. Credentials are
encrypted at rest with `INTEGRATION_ENCRYPTION_KEY` (a Fernet key — required in
staging/production; development derives a throwaway one) and are never returned by
any endpoint. `.../test` re-verifies a connection live.

Connected integrations unlock voice tools for that organization's calls, each
bounded by an ~8s timeout that degrades to a captured lead rather than leaving
the caller in silence:

- **Calendar:** `check_availability` (free slots for a day) and `book_appointment`
  (books a slot, emits `appointment.booked`, and writes a `book_appointment` lead).
- **CRM:** `find_customer` (recognise the caller mid-call) and `create_follow_up`
  (log a task). When a call finishes, the worker pushes it to the CRM —
  find-or-create the contact, attach a note with the summary, and open a
  follow-up task per captured lead — retrying with backoff and dead-lettering,
  idempotent per `(org, provider, call_id)`.

Adding a provider means a new module under
`app/domains/integrations/providers/`, an entry in `registry._BUILDERS`, and its
name in `CALENDAR_PROVIDERS` / `CRM_PROVIDERS`.

## Tools the agent can call

- `capture_caller_need` — records intent, name, callback number, urgency and details
- `transfer_to_human` — transfers via SIP REFER to `contact.transfer_to`
- `check_availability` / `book_appointment` — only when a calendar integration is connected
- `find_customer` / `create_follow_up` — only when a CRM integration is connected
- `end_call` — hangs up, after waiting for the goodbye to finish playing

Add your own in `app/tools.py`: define the schema in `tool_schemas` and handle it in
`dispatch`.

## Not built yet
- Outbound calling and follow-ups.
- OAuth connectors and token refresh (current Cal.com and HubSpot connectors use
  private API credentials).
- Per-business voicemail fallback when the model or network fails mid-call.
