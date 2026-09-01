# Call Agent — User Flow

How the two people who touch the product move through it: the **business owner /
team member** who runs an organization from the dashboard, and the **caller** who
phones the number. Every step below names the concrete API it uses so the
frontend and the backend stay in lockstep.

Backend today: multi-tenant FastAPI, cookie-authenticated `/api/v1`, one agent
per organization, instant number-on-signup. No UI ships yet — this document plus
[FRONTEND_IMPLEMENTATION.md](FRONTEND_IMPLEMENTATION.md) is the spec for the first
one.

---

## 1. Actors and surfaces

| Actor | Surface | Auth |
| --- | --- | --- |
| Prospective owner | Marketing site → signup form | none |
| Owner / admin / member / viewer | Dashboard SPA (`/api/v1`, session cookie) | httpOnly `session` cookie + `csrf` double-submit |
| Programmatic integrator | Public REST (`Authorization: Bearer cak_…`) | scoped API key |
| Platform operator | Same dashboard, `is_platform_admin` unlocks read-only `/admin/*` | session cookie |
| Caller | The phone line | none — SIP |

Roles rank `viewer < member < admin < owner`. Reads need `member`; agent and
config changes need `admin`; billing, member-role changes, and deletion need
`owner`.

---

## 2. Owner journey

### 2.1 Sign up — one call, live number

```
Marketing "Get started"
  → signup form (email, password, organization name)
  → POST /api/v1/auth/signup
  → 201: { user, organization, phone_number, subscription, checkout_url }
  → session cookie is set; verification email is sent
```

The single request creates the account, the organization (caller becomes
`owner`), **claims a phone number from the pre-warmed pool**, and publishes a
generic default agent on it. On success the number is already routable.

Branches the UI must render from the 201 body:

| Condition | Body | UI |
| --- | --- | --- |
| Normal (billing off) | `phone_number` set, `subscription` null, `checkout_url` null | Land in dashboard, show the number + "Call it now" |
| Normal (billing on) | `phone_number` set, `subscription.status` = `checkout_pending`, `checkout_url` set | Redirect to `checkout_url`; on return, poll `GET /api/v1/organizations/{id}/agent` / billing overview |
| Pool empty | `phone_number` null (subscription/checkout fields unchanged) | Dashboard with a "we're assigning your number, check back shortly" panel; key off `phone_number === null` and keep polling `GET …/agent` (`provisioned` flips to `true` once the refill job assigns one) |
| Stripe unavailable | `phone_number` set, `checkout_url` null | Dashboard usable; show "finish setting up billing" CTA → `POST …/billing/checkout` |
| Email taken | 409 `email_taken` | Inline form error, link to login |
| Weak/invalid input | 422 envelope with `field_errors` | Map `field_errors` onto the form fields |

### 2.2 First test call

Nothing to do in the product — the owner dials the number shown and hears the
default agent. The dashboard should surface, in order of usefulness:

1. the phone number, tap-to-copy;
2. a "make a test call" hint;
3. a live-ish call list (`GET /api/v1/organizations/{id}/calls`) that starts
   empty and gains the test call within a few seconds of hang-up.

### 2.3 Customise the agent — draft → preview → publish

This is the core loop. The agent is edited as a **draft** that does not affect
live calls until published.

```
GET  /api/v1/organizations/{id}/agent
        → { provisioned, lifecycle, editable, active_phone_numbers,
            published: { version_number, configuration, rendered_prompt },
            draft:     same shape | null }

PUT  /api/v1/organizations/{id}/agent/draft        (admin)
        body: the FULL configuration object (see 2.4). Server ignores any
        business.phone_numbers you send and pins the pool number.
        → 200 { version_number, configuration, rendered_prompt }

GET  /api/v1/organizations/{id}/agent/draft        → draft + rendered_prompt
DELETE /api/v1/organizations/{id}/agent/draft      (admin) → 204, draft discarded
POST /api/v1/organizations/{id}/agent/publish      (admin)
        → 200 published version; the next call uses it immediately
```

Editor UX:

- Load `GET …/agent`. If `draft` is present, open it (resume editing);
  otherwise seed the form from `published.configuration`.
- The form is a structured view over the configuration object — identity,
  greeting, hours, services, FAQs, transfer number, guardrails, free-prose
  knowledge. Every save sends the whole object back (PUT is a replace, not a
  patch).
- Show `rendered_prompt` in a read-only "what the agent is told" pane so the
  owner sees the effect of their edit before publishing. (Note: the spoken
  greeting is delivered separately and is not part of `rendered_prompt`.)
- "Save draft" → PUT. "Discard draft" → DELETE. "Publish" → POST, then re-fetch
  `GET …/agent` and show "live as of version N".
- `editable: false` (org suspended/closed) → form is read-only with a banner;
  PUT/publish return `409 agent_locked`.
- `provisioned: false` (no number yet) → editor is replaced by the "assigning
  your number" panel; PUT returns `409 agent_not_provisioned`.

### 2.4 The configuration object

One JSON document, stored immutably per published version. Editable sections:

| Section | Keys | Notes |
| --- | --- | --- |
| `business` | `name`, `timezone` (IANA), `what_we_do` | `phone_numbers` is server-managed — never sent by the client |
| `agent` | `name`, `greeting`, `voice`, `role`, `style` | `greeting` ≤ 500 chars, spoken verbatim at answer |
| `hours` | per-day strings + `notes` | rendered into the prompt |
| `location` | `address`, `directions` | optional |
| `contact` | `phone`, `email`, `website`, `transfer_to` | `transfer_to` (E.164) enables the `transfer_to_human` tool |
| `services` | list of `{ name, price, duration, notes }` | "the only prices you may quote" |
| `faqs` | list of `{ question, answer }` | approved answers |
| `knowledge` | free prose | passed through verbatim |
| `guardrails` | `never[]`, `transfer_when[]` | hard rules in the prompt |

Validation lives server-side (`PublishedBusinessConfiguration`): `business.name`,
`business.timezone`, `agent.name`, `agent.greeting` are required; a bad timezone
is `422 validation_failed` with `field_errors`.

### 2.5 Connect integrations (optional)

```
GET  /api/v1/organizations/{id}/integrations
GET/PUT/DELETE /api/v1/organizations/{id}/integrations/{provider}   (cal_com | hubspot)
POST /api/v1/organizations/{id}/integrations/{provider}/test
```

- `cal_com`: API key + event-type id. `hubspot`: private-app token.
- Credentials are write-only — never returned. Show connection status + a
  "Test connection" button (`/test`).
- A connected calendar unlocks `check_availability` / `book_appointment` on
  calls; a connected CRM unlocks `find_customer` / `create_follow_up` and a
  post-call contact/note/task sync.

### 2.6 Invite the team (optional)

```
GET/POST /api/v1/organizations/{id}/invitations                (admin)
DELETE   /api/v1/organizations/{id}/invitations/{invitation_id}
GET      /api/v1/invitations/{token}          (public preview)
POST     /api/v1/invitations/{token}/accept   (signed-in, email must match)
GET      /api/v1/organizations/{id}/members
PATCH    /api/v1/organizations/{id}/members/{user_id}          (owner)
DELETE   /api/v1/organizations/{id}/members/{user_id}          (owner, or self = leave)
```

Invite email carries a token link → app route `/invitations/{token}` → preview →
"Accept" (must be signed in as the invited address).

### 2.7 Operate — calls, leads, follow-up

```
GET   /api/v1/organizations/{id}/calls[?cursor=&limit=]
GET   /api/v1/organizations/{id}/calls/{call_id}          → transcript + summary + cost
GET   /api/v1/organizations/{id}/leads[?cursor=&limit=]
PATCH /api/v1/organizations/{id}/leads/{lead_id}          → status new|handled|dismissed
GET   /api/v1/organizations/{id}/audit-log                (admin)
```

- Calls list: time, from-number, outcome (`completed`/`transferred`/`error`/
  `no_conversation`), duration, model cost. Row → detail drawer with the full
  transcript, the structured summary (`summary`, `caller_wants`,
  `action_required`, `sentiment`, `unanswered`), and cost breakdown.
- Leads inbox: intent, caller name, callback number, urgency, preferred time,
  details; a status control (`new` → `handled`/`dismissed`) that PATCHes.
- Cursor pagination everywhere: `{ items, page: { next_cursor, has_more } }`.

### 2.8 Outbound webhooks (optional, for integrators)

```
GET/POST     /api/v1/organizations/{id}/webhook-endpoints                 (admin)
GET/PATCH/DELETE /api/v1/organizations/{id}/webhook-endpoints/{eid}
POST         /api/v1/organizations/{id}/webhook-endpoints/{eid}/rotate-secret
GET          /api/v1/organizations/{id}/webhook-endpoints/{eid}/deliveries[?status=]
GET          /api/v1/organizations/{id}/webhook-deliveries/{did}
POST         /api/v1/organizations/{id}/webhook-deliveries/{did}/retry
```

Events: `call.completed`, `call.transferred`, `lead.created`,
`appointment.booked`. Secret shown once on create/rotate. Delivery history view
with per-attempt status and a "retry" action for failed/dead-lettered rows.

### 2.9 API keys (optional)

```
GET/POST  /api/v1/organizations/{id}/api-keys                 (admin)
POST/DELETE /api/v1/organizations/{id}/api-keys/{key_id}[/rotate]
```

Scopes: `calls:read`, `leads:read`, `leads:write`. Secret (`cak_…`) shown once.

### 2.10 Billing (only when `BILLING_ENABLED`)

```
GET  /api/v1/billing/plan
GET  /api/v1/organizations/{id}/billing/overview
POST /api/v1/organizations/{id}/billing/checkout   → hosted Checkout url
POST /api/v1/organizations/{id}/billing/portal     → hosted Portal url
GET  /api/v1/organizations/{id}/billing/usage
GET/PUT /api/v1/organizations/{id}/billing/spend-limit
```

When billing is off every route above returns `404` — the UI must feature-flag
the whole billing section off `GET /health.billing_enabled` (or a `/me`-level
flag) rather than showing dead buttons.

### 2.11 Privacy & data rights

```
GET/PATCH /api/v1/organizations/{id}/privacy                  → transcript retention (admin)
GET/POST  /api/v1/organizations/{id}/data-requests/...        → export / schedule deletion (owner-confirmed)
```

### 2.12 Account lifecycle the UI must reflect

```
provisioning ──pay──▶ active ──miss payment──▶ (past_due) ──grace elapses──▶ suspended
     │                   ▲                                                      │
     │ grace elapses     └──────────────── invoice.paid ──────────────────────┘
     ▼
  closed  (number released, agent archived)
```

- `provisioning`: number is live, agent editable, "finish checkout" banner.
- `active`: everything normal.
- `suspended`: number stops answering; agent read-only (`editable: false`);
  prominent "update payment" banner → billing portal. Reversible on payment.
- `closed`: number gone, agent archived; offer "sign up again".

`GET …/agent` returns `lifecycle` and `editable` on every load so the shell can
render the right banner without a separate call.

---

## 3. Caller journey

The caller never sees the product — but the owner is judged on this flow, so the
dashboard should make each stage observable after the fact.

```
Caller dials the number
  → Twilio → SIP trunk → OpenAI Realtime
  → POST /openai/webhook  (realtime.call.incoming)  to this server
  → resolve number → published agent?           no  → SIP reject 404
                                                yes → spend limit ok?   no → SIP reject 402
                                                                        yes → answer
  → server attaches text sideband channel, sends the fixed greeting
  → conversation: caller speaks ⇄ agent speaks (audio is peer-to-peer with OpenAI)
  → agent calls tools as needed:
        capture_caller_need         → a lead row + lead.created webhook
        transfer_to_human           → SIP REFER to contact.transfer_to
        check_availability / book_appointment   (calendar connected)
        find_customer / create_follow_up        (CRM connected)
        end_call                    → drain goodbye audio, hang up
  → finalize: store transcript, write LLM summary, compute model cost,
              emit call.completed (+ call.transferred), queue CRM sync,
              send opt-in summary email
```

What the owner sees afterwards: a new **call** row (transcript, summary, cost)
and, if the agent captured anything, one or more **leads**. Declined calls
(unknown number, over spend limit, suspended) never create a record — the
dashboard's "calls" list is only answered calls.

---

## 4. Cross-cutting UI rules

- **Error envelope**: every `/api/v1` error is
  `{ "error": { code, message, field_errors, request_id } }`. Switch on `code`;
  fall back to `message`; attach `field_errors` to inputs; log `request_id`.
- **CSRF**: read the `csrf` cookie (not httpOnly) and send it as `X-CSRF-Token`
  on every non-GET. A missing/mismatched token is `403 csrf_failed` → refresh
  via any GET (`/api/v1/ping`) and retry once.
- **401** anywhere → clear local auth state, route to `/login`.
- **404 on an organization** means "not a member" — treat as not-found, never
  as "exists but forbidden".
- **Pagination**: always `cursor` + `limit`; never offsets.
- **Empty states** are first-class: no calls yet, no leads yet, no draft, no
  integrations, pool number pending.
- **Polling, not sockets**: there is no realtime channel. The calls list and the
  "number pending" panel poll (e.g. every 5–10 s while the tab is focused).
