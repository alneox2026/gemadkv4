# Production Cloud Middleware Template (Multi-Agent Cluster)

A decoupled, production-grade cloud middleware stack connecting frontend clients (e.g. FlutterFlow, web, mobile) to Google Cloud ADK Agents hosted on **Agent Platform** (Vertex AI Reasoning Engines) and **Cloud Run**.

---

## Architecture Overview

```
                          ┌───────────────────────────┐
                          │   FlutterFlow / Clients   │
                          └─────────────┬─────────────┘
                                        │ HTTPS / SSE Streams
                                        ▼
                       ┌─────────────────────────────────┐
                       │      agent_gateway service      │
                       │ • Firebase Auth Verification    │
                       │ • $0.05 Minimum-Balance Gate    │
                       │ • Real-time SSE Token Streaming │
                       └───────┬─────────────────┬───────┘
                               │                 │
                Asynchronous   │                 │ Real-Time Streaming
                Pub/Sub Events │                 │ (Vertex AI / Cloud Run)
                               ▼                 ▼
          ┌──────────────────────────┐     ┌─────────────────────────────┐
          │ agent_persistence_worker │     │ Upstream ADK Agents         │
          │ • Atomic Firestore Batch │     │ • Agent Platform (Vertex AI)│
          │ • Non-blocking Ledger    │     │ • Cloud Run Hosted Agents   │
          │ • Wallet Turn Settlement │     └─────────────────────────────┘
          └────────────┬─────────────┘
                       │
                       ▼
          ┌──────────────────────────┐
          │     Google Firestore     │
          └──────────────────────────┘
```

---

## Billing preflight invariant — keep this order

Treat this ordering as a hard billing and side-effect boundary:

- Authenticate and validate existing thread/session ownership using server-owned state only.
- Read the wallet and require at least the configured minimum balance (default `$0.05`). If the
  gate fails (including HTTP 402), return without creating a provider-side session or sending
  agent/model input. This is a read-only check: it must not reserve, debit, or write wallet state.
- Only after the balance check succeeds may the gateway create a provider-side session or submit
  agent/model input.
- Emit only a zero-value lifecycle marker for the worker; usage debits happen asynchronously.
- Keep buffered and streaming paths aligned; test that zero-credit requests return 402 with no
  provider-session or agent calls.

Purely local ID generation may happen before the balance check only when it has no provider-side
side effects.

---

## Directory Structure

```
middleware-template/
├── common/                  # Shared Pydantic contracts, ID generators, and security helpers
├── config/                  # Declarative Agent registry and billing model pricing YAMLs
│   ├── agents.dev.yaml      # Development agent registry
│   ├── agents.prod.yaml     # Production agent registry
│   ├── billing.prod.yaml    # Production rate schedule for gemini-3.8-flash
│   └── billing.test.yaml    # Test rate schedule for gemini-3.8-flash
├── infra/                   # Terraform Infrastructure-as-Code for Cloud Run, Pub/Sub, IAM
│   └── terraform/
├── scripts/                 # Cloud Shell build, rollout, and deployment automation scripts
├── services/                # Core microservices
│   ├── agent_gateway_v3/    # Public streaming proxy, auth, and minimum-balance gate
│   └── agent_persistence_worker_v3/ # Background Pub/Sub processor and ledger settlement
└── tests/                   # Unit and integration test suite
```

---

## 🛠️ Files to Modify When Deploying a New Project / Cluster

When using this template to create a new cluster (e.g. `v4`, `hr-agents`, `sales-agents`, or a new GCP project), update the following files:

### 1. Project & Cluster Configuration
* **`infra/terraform/variables.tf`** (or `terraform.tfvars`):
  * `project_id`: Your new Google Cloud Project ID (e.g. `my-new-gcp-project`).
  * `region`: Target GCP deployment region (default: `us-central1`).
  * `gateway_service_name`, `worker_service_name`: Service names for the new cluster.
  * `pubsub_topic_name`: Pub/Sub topic name for turn events.
  * Firestore collection names: runtime collections are namespaced per cluster by the Cloud Shell workflow. Shared wallet, transaction, and billing-period collections retain their Terraform-configured values; use matching values in Central Billing and all middleware clusters.

### 2. Agent Registration
* **`config/agents.prod.yaml`** & **`config/agents.dev.yaml`**:
  * Register the agents for this cluster:
  ```yaml
  my_new_agent:
    agent_id: my_new_agent
    backend: agent_runtime          # or cloud_run_adk
    model: gemini-3.8-flash
    resource_name: projects/YOUR_PROJECT_ID/locations/us-central1/reasoningEngines/YOUR_ENGINE_ID
    region: us-central1
    streaming_enabled: true
    persistence_enabled: true
    auth_policy: firebase
  ```

### 3. Model and Pricing Policy
* This template currently accepts only the canonical runtime model ID `gemini-3.8-flash`. Unknown versions and aliases fail closed; the regional catalog key is selected from the configured Vertex endpoint location and is not a runtime model ID.
* Production and test catalogs contain dated rate schedules. The gateway selects the rate effective on the current UTC date and records both `pricing_version` and `pricing_effective_from` with each usage record. Current introductory rates run through December 31, 2026; the published standard schedule applies starting January 1, 2027. These token rates do not include explicit cached-content storage time or grounding requests; do not enable those billable features for cost recovery without metering them separately. Review Google's pricing page before making later catalog changes.
* Do not map a new model name to this model's rate. Add a separately reviewed exact model entry, endpoint-specific prices, and regression tests before enabling another model.

### 4. Build & Deployment Shell Scripts
* **`scripts/cloudshell_build_middleware.sh`**:
  * Builds only this repository's two stack-prefixed images.
  * Use `PROJECT_ID`, `REGION`, and `REPOSITORY` environment variables when the defaults are not appropriate; do not edit shared image names into the script.
* **`scripts/cloudshell_deploy_middleware.sh`**:
  * Deploys this middleware into a stack-specific Terraform state prefix, with stack-scoped runtime collections and Terraform-configured shared billing collections.
  * It derives the stack name from the clone directory, does not import resources by default, refuses delete/replace plans unless explicitly approved, and prompts before applying a reviewed plan.

For production, set `DEPLOYMENT_ENV=production` and provide a non-empty
`ALERT_NOTIFICATION_CHANNELS_JSON` value (a JSON array of Cloud Monitoring
channel resource names). The deploy script enables Cloud Run deletion
protection and refuses known development agent targets unless
`PRODUCTION_AGENT_TARGETS_REVIEWED=true` is deliberately set after review.
Automated applies require the explicit `TERRAFORM_AUTO_APPROVE=true` opt-in.
The worker retryable-failure alert matches structured Cloud Logging events
directly; it does not depend on a newly-created user log metric propagating
before Terraform creates the alert policy.
Billing reconciliation defaults to once per hour (at minute 0). Delayed-usage
lookups use an initial 5-minute delay, exponential backoff capped at 6 hours, and a
7-day automated retention window. Configure
`billing_usage_reconciliation_initial_backoff_seconds`,
`billing_usage_reconciliation_max_backoff_seconds`, and
`billing_usage_reconciliation_retention_seconds` in Terraform. The billing
reconciliation scheduler must remain enabled whenever wallet enforcement is
enabled.

The Cloud Shell deployment script enables wallet enforcement and reconciliation
together. A manual Terraform deployment keeps both disabled by default; enable
both `billing_enforcement_enabled` and `billing_reconciliation_enabled` together
to use the V4 wallet gate and worker settlement path.

### 5. Deploying ADK Agents (Independent Scripts)
* **`scripts/deploy_cloudrun_streaming_agent.sh`**:
  * Build & deploy any Cloud Run streaming ADK agent with production sizing (2 vCPU, 2 GiB, Concurrency 80):
  ```bash
  bash ./scripts/deploy_cloudrun_streaming_agent.sh <AGENT_NAME> <AGENT_DIRECTORY> <MODEL_NAME>
  ```
* **`scripts/deploy_agentplatform_streaming_agent.py`**:
  * Deploy any local ADK Agent to Vertex AI Agent Platform (Reasoning Engine).

---

## 🚀 Deployment Sequence in Google Cloud Shell

```bash
# 1. Clone your new repo in Cloud Shell
cd ~
git clone <YOUR_NEW_GITHUB_REPO_URL>
cd <REPO_NAME>

# 2. Update to the committed main revision before each deployment
git fetch origin main
git reset --hard origin/main

# 3. Build Middleware Container Images in Artifact Registry
bash ./scripts/cloudshell_build_middleware.sh

# 4. Deploy Middleware Stack via Terraform / Cloud Run
bash ./scripts/cloudshell_deploy_middleware.sh
```

### Stack identity and safe defaults

For a clone named `agentic4`, the scripts automatically use the `agentic4`
image prefix, `stacks/agentic4/middleware` Terraform state prefix, and an
`agentic4` namespace for runtime Firestore collections. Shared billing
collections keep their Terraform-configured names. This isolates new middleware from existing
stacks even when they share a Google Cloud project and Artifact Registry
repository. Override `MIDDLEWARE_STACK_NAME`, `MIDDLEWARE_IMAGE_PREFIX`, or
`FIRESTORE_NAMESPACE` only intentionally.

### Shared Central Billing vs. Stack-Scoped Collections

The Cloud Shell deploy script distinguishes between **shared central billing collections** and **stack-specific agent runtime data**:

1. **Shared Central Billing Collections**:
   - `FIRESTORE_CUSTOMER_WALLETS_COLLECTION`
   - `FIRESTORE_WALLET_TRANSACTIONS_COLLECTION`
   - `FIRESTORE_CUSTOMER_BILLING_PERIODS_COLLECTION`
   With `SHARE_CENTRAL_BILLING_COLLECTIONS=true` (default), the deploy script uses the exact values configured for the corresponding Terraform variables in `terraform.tfvars` or `variables.tf`; the template defaults are the canonical `_v3` names. Thus customized names such as `*_service43` are preserved and must match the central billing service exactly.

2. **Stack-Scoped Collections**:
   - `FIRESTORE_THREADS_COLLECTION` (`agent_threads_<namespace>`)
   - `FIRESTORE_MESSAGES_SUBCOLLECTION` (`messages_<namespace>`)
   - `FIRESTORE_IDEMPOTENCY_COLLECTION` (`processed_events_<namespace>`)
   - `FIRESTORE_BILLING_LEDGER_COLLECTION` (`agent_billing_ledger_<namespace>`)
   - `FIRESTORE_BILLING_RESERVATIONS_COLLECTION` (`billing_reservations_<namespace>`)
   These remain isolated per stack using `${FIRESTORE_NAMESPACE}` so multiple agent stacks in the same GCP project do not collide on thread sessions or turn reservations.

To customize shared collection names, set the corresponding Terraform variables in `terraform.tfvars` or `variables.tf`. The script validates any exported `FIRESTORE_*_COLLECTION` override against the configured Terraform value to catch stale Cloud Shell settings; `ALLOW_CUSTOM_SHARED_COLLECTIONS=true` is required to intentionally override it. With `SHARE_CENTRAL_BILLING_COLLECTIONS=false`, the wallet, transaction, and billing-period collections are stack-scoped by default; inherited environment overrides fail closed, and an explicit custom override requires the same opt-in. The other runtime collections remain stack-scoped as usual. Do not change `FIRESTORE_NAMESPACE` just to share selected billing records.

`IMPORT_EXISTING_RESOURCES=true` is required for recovery-only Terraform
imports. The deploy script rejects plans that delete or replace resources
unless `ALLOW_TERRAFORM_DELETES=true` is explicitly set after review.

### V4 Wallet Gate, Batched Settlement, and ADK Usage

* **$0.05 minimum-balance gate, no monetary hold**: Before routing a prompt, the gateway reads the
  server-owned wallet and requires `available_credit_nanos >= billing_minimum_balance_nanos`
  (default `50,000,000` nanos / `$0.05`). This single check does not mutate the wallet or reserve
  funds. Pending or unsettled turns do not block subsequent requests; only an insufficient current
  balance does. Each turn may have a zero-value lifecycle marker, which is not a wallet hold.
* **Batched wallet settlement**: The worker persists per-turn usage/price records and queues priced
  turns. It closes a wallet settlement batch after the configured maximum age (default 1 hour),
  session idle period (5 minutes), turn count (20), or exposure threshold (`$1`). The scheduled
  reconciler defaults to hourly UTC. These estimates are provisional until settlement. Batch close
  debits available wallet credit once; any confirmed unpaid remainder is added to the shared
  `usage_debt_nanos` debt ledger. Stable turn/batch transaction IDs make event retries idempotent.
* **Debt repayment**: The central billing service must process top-ups through its shared atomic
  credit operation: repay known `usage_debt_nanos` first, then add only the remainder to available
  credit. Credit retries must be idempotent. Refunds and chargebacks remain separate flows; do not
  treat a top-up reversal as a usage-debt repayment.
* **Delayed or missing usage is not free usage**: For Vertex AI Agent Runtime turns, the worker
  preserves resource, session, and invocation identifiers and polls session events using the
  [Agent Runtime session events API](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1beta1/projects.locations.reasoningEngines.sessions.events/list).
  Retries use bounded exponential backoff (5-minute initial delay, 6-hour cap) for up to 7 days.
  When complete, recovered usage is priced, recorded, and queued for settlement; retrying the same
  event cannot charge twice. If usage is still missing, ambiguous, or unpriced after retention, the
  marker goes to manual billing review without blocking new requests. Missing usage is never
  guessed or treated as free. The generic Cloud Run ADK backend has no session-events recovery
  adapter here; missing usage on that backend is therefore flagged for manual review. Recovered
  usage is corrected before its first settlement. A provider correction that arrives after a batch
  has already settled is not automatically applied as a delta by this template; handle that through
  an explicit adjustment workflow rather than rewriting an immutable wallet transaction.
* **Usage-cost scope**: Token-rate estimates reflect the configured model catalog and complete
  provider usage metadata. Tool calls, hosted containers, and other non-model costs are included
  only if the upstream provider reports them in per-turn usage. Google Cloud infrastructure costs
  are not exactly attributable to a user from these turn records; reconcile those separately using
  Cloud Billing export and an explicit allocation policy.
* **ADK token limits are backend-owned**: The stock ADK `/run_sse` API does not define request-level
  `generation_config` or `max_total_tokens` fields. Configure model output limits, context/history
  pruning, and tool/LLM-call limits in the ADK agent container. The `$0.05` check is an admission
  threshold, not a spending cap; actual usage may exceed available credit and create debt.
* **Upstream timeouts**: `upstream_read_timeout_seconds` (default `240s`) limits idle time between
  chunks; `upstream_total_timeout_seconds` (default `270s`) is the absolute turn deadline from
  gateway request start. Keep the total limit below `GATEWAY_TIMEOUT`. Post-acceptance timeouts
  publish partial usage for worker reconciliation; they do not trigger a wallet hold or debit.
* **Rate changes**: The catalog switches to the configured January 1, 2027 rates automatically
  using UTC. Verify the model rate card against the provider's current pricing before production;
  the minimum-balance gate is independent of model prices.


## 🚀 Production Go-Live Checklist

Follow this checklist before approving and deploying a production middleware stack:

1. **GCP Project Isolation**:
   * Set your active production Google Cloud Project:
     ```bash
     gcloud config set project <PRODUCTION_PROJECT_ID>
     export PROJECT_ID="<PRODUCTION_PROJECT_ID>"
     ```
   * Confirm that the deploy script banner prints `<PRODUCTION_PROJECT_ID>` and not `ceo-dev123`. Do not use `ALLOW_DEV_PROJECT_IN_PROD=true`.

2. **Browser Origins (CORS)**:
   * Export your exact production domain(s) as a plain string without markdown formatting:
     ```bash
     export ALLOWED_ORIGINS="https://app.yourdomain.com"
     ```

3. **Production Cloud Monitoring Alerts**:
   * Production deployments strictly require at least one Cloud Monitoring notification channel:
     ```bash
     export ALERT_NOTIFICATION_CHANNELS_JSON='["projects/<PRODUCTION_PROJECT_ID>/notificationChannels/<CHANNEL_ID>"]'
     ```

4. **ADK Agent Backend Target**:
   * In `config/agents.prod.yaml`, replace the development endpoint (`maximus84-281577273798.us-central1.run.app`) with your deployed production Cloud Run ADK agent service URL.
   * Do not set `PRODUCTION_AGENT_TARGETS_REVIEWED=true` to bypass this check without updating the endpoint.

5. **Manual Terraform Review**:
   * Ensure `TERRAFORM_AUTO_APPROVE` is unset or `false`.
   * Manually review the output plan in Cloud Shell, verify resource names and IAM roles, then type `yes` when prompted.

6. **Model Pricing and Wallet Gate**:
   * Confirm production agents use exactly `gemini-3.8-flash`; unlisted aliases and versions intentionally fail closed.
   * Confirm the production rate schedule and the `$0.05` minimum-balance gate. Verify the central billing service repays recorded usage debt before crediting top-ups.

---

## 🧪 Running Tests Locally

```bash
python -m pytest
```
All unit and integration tests should pass with 100% success.
