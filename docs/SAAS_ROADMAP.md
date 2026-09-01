# Call Agent SaaS Architecture and Roadmap

## Purpose

This document describes how to evolve Call Agent from a single-business voice
agent into a multi-tenant SaaS product that businesses can sign up for, configure,
pay for, and connect to third-party systems.

The current application is a useful call-runtime foundation. The SaaS product
needs a control plane around it for customer accounts, tenant isolation, billing,
telephone provisioning, integrations, operations, and self-service onboarding.

## Recommended tenancy model

Start with:

- One shared OpenAI project and production webhook.
- One platform-owned Twilio account and shared SIP trunk, with every purchased
  number mapped to exactly one customer organization in the application database.
- One PostgreSQL database with strict organization-level isolation.
- A modular monolith with separate API, call-runtime, and worker processes from
  the same repository.
- A subscription with included minutes and metered overage.
- A general integration framework, followed by individual provider connectors.

Do not create a separate deployment for every small customer. Dedicated OpenAI
projects or isolated deployments can be added later for enterprise customers.

## Target architecture

```text
API clients / admin automation
      |
      v
Management / Public API
      |
      +-- Authentication and organizations
      +-- Agent and business configuration
      +-- Phone-number provisioning
      +-- Billing and usage
      +-- Integration management
               |
               v
        PostgreSQL + job queue
               |
      +--------+---------+
      |                  |
      v                  v
Call runtime       Background worker
      |                  |
      |                  +-- OAuth refresh
      |                  +-- CRM/calendar synchronization
      |                  +-- webhook delivery
      |                  +-- billing reconciliation
      |                  +-- number provisioning
      v
OpenAI Realtime
      |
      v
Twilio SIP and telephone network
```

The call runtime must remain fast and focused. Slow CRM, calendar, notification,
billing, and webhook operations should run through background jobs.

## 1. Establish multi-tenancy

Every customer should be represented by an `organization`. Every tenant-owned
record must carry an `organization_id`.

Suggested initial tables:

```text
organizations
users
memberships
business_profiles
agent_versions
phone_numbers
calls
turns
leads
subscriptions
usage_events
integration_connections
webhook_endpoints
webhook_deliveries
api_keys
audit_logs
```

Important rules:

- `phone_numbers.e164` must be globally unique.
- Calls, transcripts, leads, integrations, and usage must always be scoped to an
  organization.
- Users access organizations through memberships with roles such as `owner`,
  `admin`, `member`, and `viewer`.
- Store the exact agent/profile version used for each call so historical behavior
  can be audited.
- Consider PostgreSQL row-level security as a second line of defense, not as a
  substitute for application authorization.
- Add tenant-isolation tests that attempt cross-organization reads and writes.

## 2. Move business configuration into PostgreSQL

[`app/business.py`](../app/business.py) currently reads YAML and resolves a
business by telephone number. Replace direct file access with a repository
interface:

```python
profile = await business_repository.find_by_phone_number(to_number)
```

Keep YAML import/export for templates and local development, but use database-backed
configuration in production.

The management API should expose validated operations for:

- Business identity and timezone.
- Greeting, voice, role, and speaking style.
- Hours, services, prices, FAQs, and knowledge.
- Transfer rules and transfer number.
- Guardrails.
- Enabled integrations.
- Notification and retention preferences.

Use draft and published profile versions. Validate and preview the generated prompt
before publishing so an incomplete edit cannot affect live calls.

## 3. Add authentication and authorization

The existing call-history endpoints do not provide customer authentication. Add:

- Signup, login, logout, and email verification.
- Password reset or managed identity authentication.
- Organization creation, invitations, and memberships.
- Role-based authorization.
- Authenticated management APIs.
- Tenant-scoped call and lead queries.
- Separate platform-administrator permissions.
- Audit logging for security, configuration, and integration changes.

Only explicitly required callback routes should remain public, for example:

```text
POST /openai/webhook
POST /webhooks/integrations/{provider}
GET  /oauth/{provider}/callback
```

Every public webhook must validate the provider signature and implement
idempotency.

## 4. Automate telephone onboarding

Convert [`scripts/twilio_setup.py`](../scripts/twilio_setup.py) from a CLI-only
script into a reusable `TwilioProvisioningService`.

Recommended onboarding flow:

1. The customer chooses a country and number type.
2. The product displays applicable regulatory requirements.
3. The customer searches available numbers.
4. Payment is collected before purchasing a number.
5. Staff explicitly confirms the billable purchase.
6. The platform purchases the number with its existing Twilio credentials.
7. The platform creates or reuses its shared SIP trunk.
8. The trunk is pointed at the OpenAI SIP project.
9. The phone number is associated with the organization.
10. The customer completes a test call before activation.

Customers do not need Twilio accounts or subaccounts. Provider credentials stay
server-side, while the globally unique destination number selects the organization
and published profile at call time. The provisioning service stores the Twilio
number and trunk identifiers against that organization for reconciliation and
support.

Support two modes eventually:

- **Managed telephony:** the SaaS owns the Twilio account, number, and provider
  billing.
- **Bring your own Twilio:** the customer connects their own account and pays
  Twilio directly.

Start with managed telephony and introduce bring-your-own-account after the core
provisioning workflow is reliable.

## 5. Use a shared OpenAI project initially

A shared OpenAI project supports the first SaaS version because each Twilio trunk
can point to the same project SIP address. The incoming webhook contains the
destination number, allowing [`app/main.py`](../app/main.py) to resolve the correct
organization before accepting the call.

The OpenAI project ID determines the SIP destination and associated webhook:
[OpenAI Realtime SIP documentation](https://developers.openai.com/api/docs/guides/realtime-sip).

Use:

- One production OpenAI project.
- A project service-account API key.
- One production `realtime.call.incoming` webhook.
- Internal per-organization usage accounting.
- Platform and tenant spending limits.

Dedicated OpenAI projects can later be offered to enterprise customers. OpenAI
provides project, service-account, rate-limit, spend-alert, and spend-limit APIs,
but automating a project per small customer adds operational complexity:
[OpenAI Projects API](https://developers.openai.com/api/reference/typescript/resources/admin/subresources/organization/subresources/projects).

## 6. Build billing and usage accounting

Do not use [`app/pricing.py`](../app/pricing.py) as the sole invoicing source. It is
useful for immediate estimates, but provider prices can change and Twilio charges
must also be reconciled.

Create an immutable usage ledger:

```text
usage_events
- organization_id
- call_id
- event_type
- quantity
- unit
- provider_cost
- customer_charge
- source
- idempotency_key
- occurred_at
```

Track at least:

- OpenAI audio input and output usage.
- Caller transcription usage.
- Summary-model usage.
- Twilio call duration.
- Phone-number rental.
- Transfers and optional integration usage.

Recommended initial pricing structure:

```text
Monthly subscription
+ included minutes
+ per-minute overage
+ optional phone-number or premium-integration fees
```

Billing work includes:

- Checkout and customer billing portal.
- Trial and plan lifecycle.
- Plan entitlements.
- Usage limits and alerts.
- Payment-failure handling.
- Invoice and provider-usage reconciliation.
- Idempotent billing webhooks.

Stripe supports subscriptions combined with meters and meter events:
[Stripe usage-based billing](https://docs.stripe.com/billing/subscriptions/usage-based/how-it-works).

## 7. Create a third-party integration framework

Do not add every provider directly to [`app/tools.py`](../app/tools.py). Define a
stable provider interface:

```python
class IntegrationProvider:
    async def authorize(...): ...
    async def refresh_credentials(...): ...
    async def tools(...): ...
    async def execute(action, arguments, context): ...
    async def handle_webhook(...): ...
```

Store connections separately:

```text
integration_connections
- organization_id
- provider
- status
- encrypted_access_token
- encrypted_refresh_token
- token_expires_at
- scopes
- external_account_id
- settings
```

Tokens must be encrypted using an application encryption key or external key
management system. Never return stored tokens to the browser after connection.

### Integration delivery order

1. **Generic outbound webhooks**

   Deliver `call.completed`, `lead.created`, and `call.transferred`. This supports
   workflow platforms, custom CRMs, and customer-owned systems immediately.

2. **Calendar integration**

   Support availability lookup, tentative holds, booking, rescheduling, and
   cancellation.

3. **CRM integration**

   Find or create contacts, create leads, append call notes, and assign follow-up
   tasks.

4. **Notifications**

   Send post-call email, SMS, or team-chat notifications.

5. **Public REST API**

   Allow customers to retrieve calls and leads and manage configuration with
   scoped API keys.

### Outbound webhook requirements

- HMAC signatures.
- Unique event IDs and timestamps.
- Idempotency keys.
- Exponential retries.
- Delivery history.
- Manual replay.
- Strict timeouts.
- Dead-letter status.
- Secret rotation.

Inbound provider webhooks must validate signatures. Twilio likewise recommends
validating signed webhook requests:
[Twilio webhook security](https://www.twilio.com/docs/usage/security).

## 8. Make voice tools tenant- and integration-aware

[`app/tools.py`](../app/tools.py) currently exposes a static set of tools. Build
the available tools for each call based on the organization, plan, and connected
integrations:

```python
tools = await tool_registry.for_organization(
    organization_id=organization.id,
    connected_integrations=connections,
    plan=subscription.plan,
)
```

Examples:

- No calendar connected: expose `capture_caller_need` only.
- Calendar connected: add `check_availability` and `book_appointment`.
- CRM connected: add `find_customer` and `create_follow_up`.
- Transfer configured: add `transfer_to_human`.

Voice tools need strict timeouts and graceful failure behavior. If a third-party
system is slow or unavailable, the agent should capture a follow-up instead of
leaving the caller in silence.

## 9. Add background jobs and shared runtime state

The current application keeps webhook deduplication, active sessions, and task
references in process memory. Move shared state to Redis or PostgreSQL before
running multiple replicas.

Shared state should include:

- Webhook idempotency keys.
- Active-call ownership and heartbeat.
- Distributed rate limits.
- Background-job queues.
- OAuth state and nonces.
- Short-lived configuration caches.

Add a worker process for:

- Outbound webhook delivery.
- CRM and calendar synchronization.
- OAuth token refresh.
- Billing meter events.
- Number provisioning.
- Provider-usage reconciliation.
- Notifications and non-urgent summaries.

Active calls remain attached to one application process. Deployments therefore
need graceful draining so existing calls finish before the old process stops.

## 10. Expand the self-service API and operations

Recommended API-driven onboarding workflow:

```text
Sign up
  -> Create organization
  -> Select plan
  -> Enter business information
  -> Choose or connect a phone number
  -> Configure the voice agent
  -> Connect calendar or CRM
  -> Make a test call
  -> Activate
```

Backend capabilities:

- Onboarding progress and status resources.
- Agent configuration and prompt preview.
- Phone numbers and transfers.
- Calls, transcripts, and summaries.
- Leads and follow-up status.
- Integrations.
- Team members and roles.
- Usage and billing.
- Security and retention settings.
- Audit log.

Expose a test-call verification operation before activation. No frontend is part
of this repository or roadmap; external clients and operational scripts consume
the versioned API.

## 11. Security and compliance requirements

Before accepting external customers:

- Remove and rotate all committed credentials.
- Encrypt OAuth tokens and provider credentials.
- Authenticate and tenant-scope every management API query.
- Add transcript retention and deletion controls.
- Add account data export and deletion workflows.
- Redact secrets and unnecessary personal data from logs.
- Add configurable transcription and recording disclosure.
- Establish terms, privacy policy, data-processing agreement, and subprocessors
  list.
- Establish incident response and backup restoration procedures.
- Add per-tenant rate and spending limits.
- Add dependency, authorization, and tenant-isolation security tests.

Healthcare, financial, and other regulated customers should be treated as a
separate compliance tier. The current application should not be presented as
compliant by default.

## Implementation phases

| Phase | Deliverable |
| --- | --- |
| 1 | Database migrations, organizations, users, memberships, and tenant-scoped calls |
| 2 | Database-backed profiles and telephone-number routing |
| 3 | Authentication, management API, and protected call history |
| 4 | Admin-led onboarding for the first businesses |
| 5 | Shared-account Twilio telephone-number provisioning service |
| 6 | Subscription billing and immutable usage ledger |
| 7 | Signed outbound webhooks and background worker |
| 8 | First calendar integration |
| 9 | First CRM integration and public API |
| 10 | Self-service onboarding, scaling, audits, and compliance controls |

### Implementation status (2026-08-31)

Phases 1–10 are implemented. Phase 10 adds tenant-authorized onboarding over the
existing draft/provision/publish/test-call workflow; Redis-backed webhook
idempotency, distributed API-key rate limits, and active-call heartbeats;
per-organization transcript retention; queued, expiring data exports; delayed and
cancellable owner-confirmed deletion; hard/soft monthly spend limits; dependency
auditing; and explicit cross-tenant authorization regressions. Account deletion
pseudonymizes retained call/usage evidence because the billing ledger remains
immutable by design.

### Product revision (2026-09-01): instant self-service signup

The multi-step onboarding here (draft → search → provision → publish →
verify-test-call, both admin-led and self-service) has been **replaced** by
`POST /api/v1/auth/signup` doing everything at once: it claims a number from a
pre-warmed pool, publishes a generic default agent on it, and opens a Stripe
Checkout Session that captures the card and starts the paid subscription (no free
trial). The worker keeps the pool stocked, reaps signups that never pay, suspends
past-due tenants, and exports metered usage to Stripe. `onboarding_records` and
`telephony_provisioning_requests` are dropped (migration `0019`);
`organizations.lifecycle` (`provisioning → active`, plus `suspended` / `closed`)
tracks state instead. The `/api/v1/admin/*` surface is now a read-only
platform-operator overview (org list + `/admin/overview` metrics) — it no longer
creates organizations, edits agents, provisions numbers, or authors billing
plans (the single plan is seeded from `STRIPE_PRICE_ID` at startup). See
`docs/INSTANT_ONBOARDING_PLAN.md`.

## Recommended MVP boundary

The first SaaS milestone should be safely multi-tenant with admin-led onboarding,
not fully self-service.

The MVP is ready when:

- Multiple organizations can use the same deployment without seeing each other's
  data.
- Each organization has a routed telephone number and published agent profile.
- Calls, leads, transcripts, and usage are tenant-scoped.
- Staff can onboard a customer through an authenticated admin workflow.
- Billing and usage are recorded reliably.
- Completed calls can be delivered through signed outbound webhooks.
- Provider failures are visible and retryable.

After operating this workflow for the first few customers and understanding the
provisioning, billing, regulatory, and support failure cases, expose the same
operations through tenant-authorized self-service API endpoints.

## Product decisions required before implementation

1. Which country or countries will the first release support?
2. When, if ever, should bring-your-own Twilio or OpenAI accounts be offered?
3. Which calendar provider should be integrated first?
4. Which CRM provider should be integrated first?
5. What are the subscription, included-minute, and overage prices?
6. What transcript retention period should be the default?
7. Is the first API release admin-onboarded, externally self-service, or a hybrid?
8. Are European data residency or regulated-industry requirements part of the
   initial release?
