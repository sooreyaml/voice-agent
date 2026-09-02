# Call Agent — Frontend Implementation Plan

Target stack: **Next.js (App Router) + React + TypeScript**, React Query for
server state, Tailwind for styling, `react-hook-form` + `zod` for forms.

Scope: the customer dashboard for the multi-tenant backend described in
[USER_FLOW.md](USER_FLOW.md) — signup, the agent editor (draft → publish), call
& lead review, integrations, team, billing, and the account-lifecycle shell.
This is the first UI for the project; there is no existing frontend to extend.

---

## 1. Constraints that shape every decision

These come from the backend as it is today. Read them before designing anything.

| Constraint | Consequence |
| --- | --- |
| Auth is an **httpOnly `session` cookie**, not a bearer token | The browser can't read it; there is no token to put in a header or `localStorage`. Auth state is derived from `GET /api/v1/me` succeeding. |
| **CSRF is double-submit**: `csrf` cookie (JS-readable) echoed as `X-CSRF-Token` | Every non-GET must copy the cookie into the header. Seed the cookie with any GET (`/api/v1/ping`). |
| Cookies are `SameSite=Lax`, `Secure` outside dev | **Cross-site XHR will not send them.** The SPA must be served **same-origin** with `/api/v1` (same scheme+host+port), or the backend must grow a CORS layer and switch to `SameSite=None` (not recommended — weakens CSRF). |
| **No CORS middleware exists** in the backend | Same-origin is currently the only option that works at all. |
| **No websocket / SSE** channel for the dashboard | Live-ish data (calls list, "number pending") is **polled**. |
| Error shape is fixed: `{ error: { code, message, field_errors, request_id } }` | One error parser; switch on `code`. |
| `/docs` (OpenAPI) is served outside production | Generate the TS client types from `/openapi.json` in dev/staging; commit the output. |
| Billing routes 404 unless `BILLING_ENABLED` | Feature-flag the billing section from `GET /health` (`billing_enabled`). |
| One organization = one agent, one number | No "select a profile" UI. Routes are `/orgs/{id}/agent`, singular. |

### Deployment shape (same-origin)

```
                    ┌─────────────── one public domain ───────────────┐
  browser ──HTTPS──▶│ reverse proxy (Caddy / Coolify / nginx)         │
                    │   /                → Next.js server (SSR + RSC)  │
                    │   /api/*           → FastAPI                     │
                    │   /openai/webhook  → FastAPI (no auth)          │
                    │   /health          → FastAPI                    │
                    └────────────────────────────────────────────────┘
```

Note for the standalone Caddy setup in the repo README: Caddy currently
basic-auths every route except `/openai/webhook` and the seed route. The SPA and
`/api/v1` must be exempted from basic auth too — the application does its own
session auth. Update the `Caddyfile` when this ships.

Local dev: run FastAPI on `:8000` and Next on `:3000`, and use Next's
`rewrites()` to proxy `/api/*` and `/health` to `:8000` so the browser only ever
talks to `:3000` (same-origin, cookies flow).

```js
// next.config.js
async rewrites() {
  return [
    { source: "/api/:path*", destination: "http://localhost:8000/api/:path*" },
    { source: "/health", destination: "http://localhost:8000/health" },
  ];
}
```

---

## 2. Project structure

```
web/
├── next.config.js
├── src/
│   ├── app/
│   │   ├── layout.tsx                 # <html>, providers
│   │   ├── page.tsx                   # marketing redirect or landing
│   │   ├── (auth)/
│   │   │   ├── login/page.tsx
│   │   │   ├── signup/page.tsx
│   │   │   ├── verify-email/page.tsx
│   │   │   └── reset-password/page.tsx
│   │   ├── invitations/[token]/page.tsx
│   │   └── (app)/
│   │       ├── layout.tsx             # auth gate + org shell + lifecycle banner
│   │       ├── page.tsx               # org picker / redirect to last org
│   │       └── orgs/[orgId]/
│   │           ├── layout.tsx         # sidebar nav, org context
│   │           ├── overview/page.tsx
│   │           ├── agent/page.tsx     # the editor (client component)
│   │           ├── calls/page.tsx
│   │           ├── calls/[callId]/page.tsx   (or drawer)
│   │           ├── leads/page.tsx
│   │           ├── integrations/page.tsx
│   │           ├── team/page.tsx
│   │           ├── webhooks/page.tsx
│   │           ├── api-keys/page.tsx
│   │           ├── billing/page.tsx
│   │           └── settings/page.tsx
│   ├── lib/
│   │   ├── api/
│   │   │   ├── client.ts              # fetch wrapper (below)
│   │   │   ├── errors.ts              # ApiError, envelope parsing
│   │   │   ├── types.ts               # generated from /openapi.json + hand types
│   │   │   └── endpoints/             # one module per domain (auth, agent, calls…)
│   │   ├── query.ts                   # QueryClient config, key factory
│   │   ├── auth.ts                    # useSession, sign-in/out mutations
│   │   └── config.ts                  # runtime flags from /health
│   ├── components/
│   │   ├── ui/                        # Button, Input, Dialog, Drawer, Toast, Table…
│   │   ├── shell/                     # Sidebar, Topbar, LifecycleBanner, OrgSwitcher
│   │   └── agent/                     # editor sections + PromptPreview + PublishBar
│   └── hooks/                         # useOrg, usePagedQuery, useCsrf…
└── tests/                             # Playwright (e2e) + Vitest + RTL (unit)
```

Keep it a sibling `web/` directory in this repo (or a separate repo that deploys
behind the same proxy). Do not put it under `app/` — that is the Python package.

---

## 3. The API layer

### 3.1 Fetch wrapper (`lib/api/client.ts`)

```ts
import { ApiError, parseEnvelope } from "./errors";

const BASE = "/api/v1"; // always same-origin

function readCookie(name: string): string | null {
  const m = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : null;
}

export async function api<T>(
  path: string,
  opts: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { "Content-Type": "application/json" };

  if (method !== "GET" && method !== "HEAD") {
    const csrf = readCookie("csrf");
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }

  const res = await fetch(BASE + path, {
    method,
    headers,
    credentials: "include",         // send the session + csrf cookies
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    signal: opts.signal,
  });

  if (res.status === 204) return undefined as T;

  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, parseEnvelope(data));
  return data as T;
}
```

- `credentials: "include"` on every call.
- CSRF header auto-attached for mutations.
- One `ApiError` type carrying `status`, `code`, `message`, `fieldErrors`,
  `requestId`.
- A tiny bootstrap on app load: `await api("/ping")` to guarantee the `csrf`
  cookie exists before the first mutation.

### 3.2 Error handling (`lib/api/errors.ts`)

```ts
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly env: { code: string; message: string;
                    fieldErrors: Record<string, string>; requestId: string },
  ) { super(env.message); }
}
```

Global React Query `onError`:

- `status === 401` → `queryClient.clear()`, hard-redirect to `/login?next=…`.
- `code === "csrf_failed"` → `await api("/ping")`, retry the mutation once.
- `status === 404` on an org route → route to `/` (not a member).
- otherwise → toast `env.message`, keep `requestId` in the toast's "details".
- `fieldErrors` are consumed by the form, not toasted.

### 3.3 Types

Generate from the live schema in dev/staging and commit:

```
npx openapi-typescript http://localhost:8000/openapi.json -o src/lib/api/types.ts
```

Hand-write the few response models the OpenAPI misses precisely (the agent
`configuration` object is `dict` in the schema — model it as a discriminated,
partial `AgentConfig` type on the frontend).

### 3.4 Endpoint modules

One file per domain, each a thin typed wrapper:

```ts
// lib/api/endpoints/agent.ts
export const agentApi = {
  get:        (orgId: string) => api<AgentState>(`/organizations/${orgId}/agent`),
  getDraft:   (orgId: string) => api<AgentVersion>(`/organizations/${orgId}/agent/draft`),
  saveDraft:  (orgId: string, config: AgentConfig) =>
                api<AgentVersion>(`/organizations/${orgId}/agent/draft`,
                                  { method: "PUT", body: config }),
  discardDraft: (orgId: string) =>
                api<void>(`/organizations/${orgId}/agent/draft`, { method: "DELETE" }),
  publish:    (orgId: string) =>
                api<AgentVersion>(`/organizations/${orgId}/agent/publish`, { method: "POST" }),
};
```

---

## 4. Auth & session in Next

There is **no readable token**, so:

- **Middleware** (`middleware.ts`) does a cheap check: if the request is under
  `/(app)` and there is no `session` cookie at all, redirect to `/login`. It
  cannot validate the cookie (no secret in the edge runtime) — that is fine, it
  only catches the logged-out case cheaply.
- **The `(app)/layout.tsx`** is a client component that calls
  `GET /api/v1/me` via React Query. While pending → full-page skeleton. On
  `401` → redirect to `/login`. On success → provides `{ user, organizations }`
  via context and renders the shell.
- **Sign in / up / out** are mutations that hit `/auth/*`; on success they
  `invalidate(["me"])` and `router.push` into the app. The cookie is set by the
  response `Set-Cookie`; the client never touches it.
- **Org selection**: `/(app)/page.tsx` reads `me.organizations`. Zero orgs is
  impossible (signup makes one); one → redirect to
  `/orgs/{id}/overview`; many → an org picker. Persist "last org" in
  `localStorage` for the redirect.

Prefer **client components + React Query** for all authed data. RSC data
fetching would need the incoming cookies forwarded to an absolute API URL and
gets awkward with the same-origin proxy; not worth it for a dashboard. Use RSC
only for static shell/marketing.

---

## 5. State management

| State | Tool | Notes |
| --- | --- | --- |
| Server data (me, agent, calls, leads, integrations, members, billing) | **React Query** | `staleTime` 30 s default; `calls` list `refetchInterval` 8 s while focused |
| Forms (agent editor, integration connect, invite, settings) | **react-hook-form + zod** | zod schema mirrors backend validation; map `ApiError.fieldErrors` onto RHF errors on 422 |
| Ephemeral UI (drawer open, active tab, toast queue) | local `useState` / a tiny context | no global store needed |
| Cross-cutting flags (`billing_enabled`) | React Query on `/health`, `staleTime: Infinity` | gate nav items |

Query key factory:

```ts
export const qk = {
  me: ["me"] as const,
  health: ["health"] as const,
  agent: (o: string) => ["org", o, "agent"] as const,
  calls: (o: string, cursor?: string) => ["org", o, "calls", cursor ?? "head"] as const,
  call: (o: string, id: string) => ["org", o, "call", id] as const,
  leads: (o: string, cursor?: string) => ["org", o, "leads", cursor ?? "head"] as const,
  integrations: (o: string) => ["org", o, "integrations"] as const,
  members: (o: string) => ["org", o, "members"] as const,
  billing: (o: string) => ["org", o, "billing"] as const,
};
```

---

## 6. Screen specs

### 6.1 Shell (`(app)/orgs/[orgId]/layout.tsx`)

- Left sidebar: Overview, Agent, Calls, Leads, Integrations, Team, Webhooks,
  API keys, Billing (if enabled), Settings.
- Topbar: org switcher, user menu (verify-email nudge if `!email_verified`,
  logout).
- **LifecycleBanner** driven by `agentApi.get(orgId).lifecycle` (or a dedicated
  `/me`-level field if added):
  - `provisioning` → "Finish setting up billing to keep your number" +
    checkout button.
  - `suspended` → red "Your account is suspended — update payment" + portal
    button; the whole app is browsable read-only.
  - `closed` → interstitial, "sign up again".
- Empty/loading/error states are components, not ad-hoc JSX.

### 6.2 Signup (`(auth)/signup`)

Form: email, password (with strength hint from
`MIN_PASSWORD_LENGTH`), organization name. On submit → `POST /auth/signup`.

Handle the 201 body per [USER_FLOW.md §2.1]:

- `checkout_url` present → `window.location = checkout_url`.
- else → `router.push("/orgs/{id}/overview")`, show a "Your number:
  {phone_number}" hero (or the "assigning your number" panel if null).
- 409 `email_taken` → field error + "log in instead".
- 422 → map `field_errors`.

Email verification is a **6-digit code**, not a link:

- After signup, show a "enter the code we emailed {email}" input (6 digits,
  accepts spaces/dashes). Submit → `POST /auth/verify-email/confirm` with
  `{ "code": "123456" }` — **authenticated**, so the signup session cookie must
  be set first.
- `200` → verified; refresh `me`.
- `400 invalid_token` → "that code is wrong or expired", let them retry.
- `429 too_many_attempts` → the code locked after 5 wrong tries; show "request
  a new code".
- "Resend" / "request a new code" → `POST /auth/verify-email/request` (also
  authenticated). Each new code invalidates the previous one; codes expire
  after 15 minutes.

### 6.3 Overview

- Phone number card (tap to copy) + "Make a test call" helper.
- Small stat row: calls this week, leads open, agent version live.
- Recent calls (5) and recent leads (5) with "view all" links.
- If `provisioned === false`: replace the number card with the pending panel,
  poll `agentApi.get` every 8 s until it flips.

### 6.4 Agent editor — the core screen

Route `/orgs/[orgId]/agent`, a client component. This is the feature that
justifies the whole first release, so it gets its own section (§7).

### 6.5 Calls

- Table: started_at, from_number, outcome badge, duration, cost.
- Cursor pagination: "Load more" using `page.next_cursor`; `refetchInterval` 8 s
  on the first page only.
- Row → right-hand **drawer** (`calls/[callId]` as intercepting route or plain
  client fetch): transcript (role-labelled turns), structured summary card,
  cost breakdown, outcome, links to any leads from this call.
- Empty state: "No calls yet — dial {number} to test your agent."

### 6.6 Leads

- Table: created_at, intent, caller_name, callback_number, urgency, status.
- Inline status control: `new` → `handled` / `dismissed` via
  `PATCH …/leads/{id}` with optional note; optimistic update.
- Filter chips by status; cursor pagination.

### 6.7 Integrations

- Two cards: **Cal.com**, **HubSpot**. Each shows connected/not, last test
  result, and a form (API key + event-type id / private-app token).
- `PUT` to connect/replace, `DELETE` to disconnect, `POST …/test` for "Test
  connection" (show latency + ok/fail).
- Never render a stored secret (backend won't return it); the field is always
  empty with a "replace" affordance.

### 6.8 Team

- Members table with role dropdown (owner-only mutate; guard last-owner and
  self-demote client-side too, mirror the 409s).
- Pending invitations list + "Invite" dialog (email + role ≤ your role).
- `/invitations/[token]` public page: preview (`GET /invitations/{token}`),
  "Accept" (must be signed in as the invited email; if not, prompt to
  login/signup with that address).

### 6.9 Webhooks & API keys

- CRUD tables. Secrets shown once in a copy-once dialog after create/rotate,
  with an explicit "I've copied it" confirm.
- Webhook deliveries sub-view: status filter, per-attempt history, "retry".

### 6.10 Billing (feature-flagged)

- If `!billing_enabled` → hide the nav item entirely.
- Overview: plan, status, current period, usage meter vs included minutes,
  spend-limit editor (`PUT …/spend-limit`).
- Buttons: "Manage payment" → `POST …/portal` → redirect; "Start subscription"
  (if `provisioning`) → `POST …/checkout` → redirect.

### 6.11 Settings

- Rename organization (`PATCH /organizations/{id}`).
- Privacy: transcript retention (`GET/PATCH …/privacy`).
- Danger zone: data export / scheduled deletion (`…/data-requests/*`), leave
  organization.

---

## 7. Agent editor — detailed design

### 7.1 Data model on the client

```ts
type AgentVersion = {
  version_number: number;
  configuration: AgentConfig;      // the full document
  rendered_prompt: string;
};

type AgentState = {
  provisioned: boolean;
  lifecycle: "provisioning" | "active" | "suspended" | "closed";
  editable: boolean;
  active_phone_numbers: string[];
  slug: string | null;
  name: string | null;
  published: AgentVersion | null;
  draft: AgentVersion | null;
};

type AgentConfig = {
  business: { name: string; timezone: string; what_we_do?: string };
  agent: { name: string; greeting: string; voice?: string;
           role?: string; style?: string };
  hours?: Record<string, string> & { notes?: string };
  location?: { address?: string; directions?: string };
  contact?: { phone?: string; email?: string; website?: string; transfer_to?: string };
  services?: { name: string; price?: string; duration?: string; notes?: string }[];
  faqs?: { question: string; answer: string }[];
  knowledge?: string;
  guardrails?: { never?: string[]; transfer_when?: string[] };
};
```

`business.phone_numbers` is deliberately **absent** from `AgentConfig` — the
server injects it. Never render or send it.

### 7.2 Editor state machine

```
                 load GET /agent
                       │
        ┌──────────────┼───────────────┐
   provisioned=false            provisioned=true
        │                              │
   "assigning number" panel      draft ? ──yes──▶ seed form from draft.configuration
   (poll every 8s)                  │              banner: "Editing an unpublished draft"
                                    no
                                    │
                          seed form from published.configuration

   form state: pristine ──edit──▶ dirty ──Save draft (PUT)──▶ saved(draft)
                                    │                              │
                                    └──Discard (DELETE)────────────┘  (only if a draft exists)

   saved(draft) ──Publish (POST)──▶ live ; refetch GET /agent ; toast "Live — version N"

   editable=false at any point ⇒ form is read-only, Save/Publish hidden, banner shown
```

### 7.3 Layout

Two-pane:

- **Left (form)** — collapsible sections: Identity, Greeting & voice, Hours,
  Services, FAQs, Transfer, Guardrails, Knowledge. `react-hook-form` with a
  `zod` schema. Array sections (services, faqs, guardrails lists) use
  `useFieldArray`.
- **Right (preview)** — read-only `rendered_prompt` from the last `PUT`
  response, in a monospace panel, with a note: *"The spoken greeting is
  delivered separately and isn't shown here."* Also a "diff vs live" toggle
  (published.rendered_prompt vs draft.rendered_prompt).

### 7.4 Save / publish bar (sticky footer)

- `Draft saved · v{n}` / `Unsaved changes` / `No draft` status.
- `Save draft` — disabled unless dirty & valid & `editable`.
- `Discard draft` — visible only when a draft exists; confirm dialog.
- `Publish` — disabled unless a draft exists & `editable`; confirm dialog
  ("Your agent will answer the next call with these settings").
- After publish: refetch `qk.agent(orgId)`, reset form baseline to the new
  published config, clear dirty.

### 7.5 Validation mapping

- Client `zod`: `business.name` non-empty ≤ 200; `business.timezone` from an
  IANA list (ship `Intl.supportedValuesOf("timeZone")`); `agent.name` ≤ 100;
  `agent.greeting` non-empty ≤ 500; `contact.transfer_to` optional E.164
  (`/^\+[1-9]\d{7,14}$/`).
- On `422` from `PUT`: read `error.field_errors` (keys like `business.timezone`)
  and `setError` on the matching RHF path; scroll to the first.
- `409 agent_locked` → toast + flip form to read-only (refetch state).
- `409 agent_not_provisioned` → swap to the pending panel.

### 7.6 Concurrency

Two admins editing: last `PUT` wins (backend archives the previous draft). Show
`draft.version_number` in the bar; if a refetch shows a higher version than the
one this tab last wrote, banner "This draft was updated elsewhere — reload".
No locking in v1.

---

## 8. Design system

Small and boring on purpose. Tailwind + a handful of primitives (Radix UI for
Dialog/Dropdown/Tabs/Toast). Tokens: one neutral gray scale, one brand accent,
semantic `success/warning/danger`. Dark mode via `class` strategy from day one.
Every screen implements the four states explicitly: **loading** (skeleton),
**empty** (illustration + one CTA), **error** (message + retry + requestId),
**data**.

---

## 9. Environment & config

Frontend needs almost nothing — it's same-origin.

| Var | Purpose |
| --- | --- |
| `NEXT_PUBLIC_APP_NAME` | display only |
| (dev) rewrite target | `http://localhost:8000` in `next.config.js` |

Runtime flags (`billing_enabled`, storage backend, model) come from
`GET /health` at load, not build time.

---

## 10. Testing

- **Unit** (Vitest + RTL): the fetch wrapper (CSRF header, 204, envelope
  parsing), the editor state machine, `field_errors` → RHF mapping.
- **Component**: agent editor with a mocked `agentApi` — draft/publish/discard,
  read-only when `editable:false`, pending panel when `provisioned:false`.
- **E2E** (Playwright against a real backend with `BILLING_ENABLED=false` and a
  seeded pool number):
  1. signup → land in dashboard → number visible;
  2. edit greeting → save draft → preview updates → publish → `GET /agent`
     shows new version;
  3. discard draft path;
  4. invalid timezone → inline error;
  5. suspend the org via a test hook → editor read-only.
- Reuse the backend's own `tests/test_agent_profile.py` as the contract these
  E2E tests mirror.

---

## 11. Accessibility & performance

- Keyboard: every action reachable; dialogs trap focus; the save bar is a
  `<form>` submit.
- Announce async results via an `aria-live` region (toast).
- Labels on every input; error text tied via `aria-describedby`.
- Code-split per route (App Router does this). The editor (`react-hook-form` +
  IANA tz list) is the heaviest bundle — lazy-load the tz list.
- React Query cache + `staleTime` keeps navigation instant; only `calls`
  polls.

---

## 12. Build order (milestones)

1. **Foundation** — Next app, Tailwind, `api()` wrapper, `ApiError`, React Query
   provider, `/ping` bootstrap, `/health` flags, generated types.
2. **Auth** — signup, login, logout, `me` gate, middleware, verify-email nudge,
   password reset. Same-origin proxy working end to end.
3. **Shell** — org layout, sidebar, org switcher, LifecycleBanner, the four
   state components.
4. **Agent editor** — read `GET /agent`, form for Identity + Greeting only,
   PUT/publish/discard, preview pane, save bar, read-only + pending states.
   Ship this — it closes the gap that blocks the instant-onboarding branch.
5. **Agent editor — full config** — hours, services, FAQs, transfer, guardrails,
   knowledge; validation mapping; diff-vs-live.
6. **Calls & leads** — tables, pagination, call drawer, lead status.
7. **Integrations** — Cal.com + HubSpot connect/test/disconnect.
8. **Team** — members, roles, invitations, `/invitations/[token]`.
9. **Billing** (behind the flag) + **Settings/Privacy**.
10. **Webhooks & API keys**.

Milestones 1–4 are the minimum that makes the product self-serviceable.

---

## 13. Open questions for the backend

1. **CORS or same-origin?** This plan assumes same-origin. If the dashboard must
   live on its own domain, the backend needs a CORS middleware
   (`allow_credentials=True`, explicit origin) and `SameSite=None` cookies — and
   a stronger CSRF story than `Lax`-backed double-submit.
2. **`lifecycle` on `/me`?** The shell wants the account state on first paint.
   Today it comes from `GET /organizations/{id}/agent`. A field on `/me`'s org
   entries (or a dedicated `GET /organizations/{id}` field) would avoid a
   waterfall.
3. **A cheaper "number ready" signal** than polling `/agent` — e.g. a
   `GET /organizations/{id}` `phone_number` field.
4. **Email verification gating** — is an unverified owner allowed to publish an
   agent? Decide and enforce server-side; the UI will mirror it.
5. **`voice` options** — is there an enumerable list, or free text? The editor
   needs it for a select vs input.
