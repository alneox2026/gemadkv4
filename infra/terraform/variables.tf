variable "project_id" {
  description = "Google Cloud project ID."
  type        = string
  default     = "ceo-dev123"
}

variable "region" {
  description = "Primary deployment region."
  type        = string
  default     = "us-central1"
}

variable "gateway_service_name" {
  description = "Cloud Run service name for the public agent gateway."
  type        = string
  default     = "gemadkv4-gateway"
}

variable "worker_service_name" {
  description = "Cloud Run service name for the persistence worker."
  type        = string
  default     = "gemadkv4-worker"
}

variable "gateway_image" {
  description = "Container image URI for the gateway service."
  type        = string
}

variable "worker_image" {
  description = "Container image URI for the persistence worker."
  type        = string
}

variable "gateway_service_account_name" {
  description = "Service account name for the gateway service."
  type        = string
  default     = "gemadkv4-gateway-sa"
}

variable "worker_service_account_name" {
  description = "Service account name for the worker service."
  type        = string
  default     = "gemadkv4-worker-sa"
}

variable "eventarc_service_account_name" {
  description = "Service account name for the Eventarc trigger."
  type        = string
  default     = "gemadkv4-eventarc-sa"
}

variable "billing_reconciler_service_account_name" {
  description = "Service account name for the Cloud Scheduler billing reconciler. Must be between 6 and 30 characters."
  type        = string
  default     = "gemadkv4-reconciler-sa"
}


variable "default_model_name" {
  description = "Default Gemini model for usage pricing if not specified by agent registry or event payload."
  type        = string
  default     = "gemini-3.8-flash"
}

variable "pubsub_topic_name" {
  description = "Topic for completed turn events."
  type        = string
  default     = "agent-turn-events-gemadkv4"
}

variable "pubsub_publish_timeout_seconds" {
  description = "Pub/Sub publish timeout for the gateway."
  type        = number
  default     = 30
}

variable "require_firebase_auth" {
  description = "Whether the gateway requires Firebase bearer tokens."
  type        = bool
  default     = true
}

variable "allowed_origins" {
  description = "Allowed CORS origins for the public gateway."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for origin in var.allowed_origins : trimspace(origin) != "*"
    ])
    error_message = "Wildcard CORS origins are not allowed. Provide explicit web origins instead."
  }
}

variable "agent_registry_path" {
  description = "Absolute path inside the container to the production agent registry."
  type        = string
  default     = "/app/config/agents.prod.yaml"
}

variable "gateway_log_level" {
  description = "Gateway application log level."
  type        = string
  default     = "INFO"
}

variable "gateway_stream_debug" {
  description = "Whether to emit one-shot upstream stream diagnostics from the gateway."
  type        = bool
  default     = false
}

variable "worker_log_level" {
  description = "Worker application log level."
  type        = string
  default     = "INFO"
}

variable "runtime_delete_timeout_seconds" {
  description = "Worker timeout for Agent Runtime session delete operations."
  type        = number
  default     = 30
}

variable "upstream_connect_timeout_seconds" {
  description = "Gateway connect timeout to Agent Runtime."
  type        = number
  default     = 10
}

variable "upstream_read_timeout_seconds" {
  description = "Maximum idle time between upstream stream chunks. This is not a total-turn deadline; use upstream_total_timeout_seconds for the absolute wall-clock limit."
  type        = number
  default     = 240
}

variable "upstream_total_timeout_seconds" {
  description = "Absolute wall-clock limit for an upstream agent turn, measured from gateway request start. The 270s default is 30s below the 300s Cloud Run request deadline; post-acceptance timeouts publish partial usage for worker reconciliation."
  type        = number
  default     = 270
}

# The Cloud Shell deployment script namespaces agent-runtime collections by
# FIRESTORE_NAMESPACE. Shared billing collections below keep their configured
# values so every middleware can point at the Central Billing records.
variable "firestore_threads_collection" {
  description = "Top-level Firestore collection for chat threads. Cloud Shell honors this custom value; template *_v3 defaults become stack-scoped."
  type        = string
  default     = "agent_threads_gemadkv4"
}

variable "firestore_messages_subcollection" {
  description = "Subcollection name for messages under each thread document."
  type        = string
  default     = "messages_gemadkv4"
}


variable "firestore_idempotency_collection" {
  description = "Top-level Firestore collection for processed event ids."
  type        = string
  default     = "processed_events_gemadkv4"
}

variable "firestore_billing_ledger_collection" {
  description = "Top-level immutable Firestore collection for completed-turn billing ledgers."
  type        = string
  default     = "agent_billing_ledger_gemadkv4"
}

variable "firestore_customer_wallets_collection" {
  description = "Top-level Firestore collection for server-owned prepaid customer wallets."
  type        = string
  default     = "customer_wallets_billingv4"
}

variable "firestore_billing_reservations_collection" {
  description = "Top-level Firestore lifecycle collection for zero-value turn markers and billing compatibility records."
  type        = string
  default     = "billing_reservations_gemadkv4"
}

variable "firestore_wallet_transactions_collection" {
  description = "Top-level immutable Firestore collection for customer wallet transactions."
  type        = string
  default     = "wallet_transactions_billingv4"
}

variable "firestore_customer_billing_periods_collection" {
  description = "Top-level Firestore collection for customer monthly billing aggregates."
  type        = string
  default     = "customer_billing_periods_billingv4"
}

variable "billing_enforcement_enabled" {
  description = "Whether the gateway must verify the wallet meets the minimum balance before each agent request. The request path does not hold or debit funds."
  type        = bool
  default     = false
}

variable "billing_minimum_balance_nanos" {
  description = "Minimum available USD wallet balance required to start a turn; 50000000 is USD 0.05. This is a read-only gateway gate, not a reservation or spending cap."
  type        = number
  default     = 50000000

  validation {
    condition     = var.billing_minimum_balance_nanos > 0
    error_message = "billing_minimum_balance_nanos must be positive."
  }
}

variable "monthly_service_fee_nanos" {
  description = "Monthly service fee recorded for each customer billing period; 5000000000 is USD 5.00."
  type        = number
  default     = 5000000000
}

variable "billing_reconciliation_batch_size" {
  description = "Maximum billing lifecycle records processed by one scheduled reconciliation run."
  type        = number
  default     = 100
}

variable "billing_batch_settlement_enabled" {
  description = "Whether the worker batches priced turns before applying wallet debits."
  type        = bool
  default     = true
}

variable "billing_batch_max_age_seconds" {
  description = "Maximum age of a pending wallet settlement batch before it closes."
  type        = number
  default     = 3600

  validation {
    condition     = var.billing_batch_max_age_seconds > 0
    error_message = "billing_batch_max_age_seconds must be positive."
  }
}

variable "billing_batch_session_idle_seconds" {
  description = "Idle time after the newest turn before a session batch may close."
  type        = number
  default     = 300

  validation {
    condition     = var.billing_batch_session_idle_seconds > 0
    error_message = "billing_batch_session_idle_seconds must be positive."
  }
}

variable "billing_batch_max_turns" {
  description = "Maximum priced turns grouped in one wallet settlement batch."
  type        = number
  default     = 20

  validation {
    condition     = var.billing_batch_max_turns > 0
    error_message = "billing_batch_max_turns must be positive."
  }
}

variable "billing_batch_max_exposure_nanos" {
  description = "Estimated batch-cost threshold in USD nanos that triggers earlier settlement."
  type        = number
  default     = 1000000000

  validation {
    condition     = var.billing_batch_max_exposure_nanos > 0
    error_message = "billing_batch_max_exposure_nanos must be positive."
  }
}

variable "billing_reconciliation_schedule" {
  description = "Five-field UTC cron schedule for billing settlement, late-usage recovery, and expired reservation review."
  type        = string
  default     = "0 * * * *"

  validation {
    condition = can(regex(
      "^[-A-Za-z0-9*/?,]+([[:space:]]+[-A-Za-z0-9*/?,]+){4}$",
      trimspace(var.billing_reconciliation_schedule)
    ))
    error_message = "billing_reconciliation_schedule must be a five-field Cloud Scheduler cron expression, for example \"0 * * * *\"."
  }
}

variable "billing_reconciliation_enabled" {
  description = "Whether Terraform should schedule billing and late-usage reconciliation on the worker service. Required when prepaid enforcement is enabled."
  type        = bool
  default     = false
}

variable "billing_usage_reconciliation_initial_backoff_seconds" {
  description = "Initial delay before retrying a missing Agent Runtime usage lookup."
  type        = number
  default     = 300

  validation {
    condition     = var.billing_usage_reconciliation_initial_backoff_seconds > 0
    error_message = "billing_usage_reconciliation_initial_backoff_seconds must be positive."
  }
}

variable "billing_usage_reconciliation_max_backoff_seconds" {
  description = "Maximum exponential-backoff delay between late-usage lookups."
  type        = number
  default     = 21600

  validation {
    condition     = var.billing_usage_reconciliation_max_backoff_seconds >= var.billing_usage_reconciliation_initial_backoff_seconds
    error_message = "billing_usage_reconciliation_max_backoff_seconds must be at least the initial backoff."
  }
}

variable "billing_usage_reconciliation_retention_seconds" {
  description = "How long the worker retries late usage before requiring manual billing review."
  type        = number
  default     = 604800

  validation {
    condition     = var.billing_usage_reconciliation_retention_seconds > 0
    error_message = "billing_usage_reconciliation_retention_seconds must be positive."
  }
}

variable "gateway_min_instances" {
  description = "Minimum number of gateway instances."
  type        = number
  default     = 0
}


variable "gateway_max_instances" {
  description = "Maximum number of gateway instances."
  type        = number
  default     = 20
}

variable "gateway_concurrency" {
  description = "Maximum concurrent requests per gateway instance."
  type        = number
  default     = 16
}

variable "gateway_cpu" {
  description = "Gateway CPU limit."
  type        = string
  default     = "1"
}

variable "gateway_memory" {
  description = "Gateway memory limit."
  type        = string
  default     = "1Gi"
}

variable "gateway_timeout" {
  description = "Gateway request timeout."
  type        = string
  default     = "300s"
}

variable "gemini_api_key_secret_id" {
  description = "Existing Secret Manager secret ID containing the Google Gemini API key."
  type        = string
  default     = "gemini-api-key"
}

variable "gemini_api_key_secret_version" {
  description = "Pinned numeric version of the Gemini API key Secret Manager secret."
  type        = string
  default     = "1"
}

variable "gemini_api_key" {
  description = "Direct Gemini API key (optional fallback when Secret Manager is not used)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cloud_run_execution_environment" {
  description = "Pinned Cloud Run execution environment for both services."
  type        = string
  default     = "EXECUTION_ENVIRONMENT_GEN2"
}

variable "cloud_run_request_based_billing" {
  description = "Whether Cloud Run CPU should be allocated only during requests."
  type        = bool
  default     = true
}

variable "worker_min_instances" {
  description = "Minimum number of worker instances."
  type        = number
  default     = 0
}

variable "worker_max_instances" {
  description = "Maximum number of worker instances."
  type        = number
  default     = 20
}

variable "worker_concurrency" {
  description = "Maximum concurrent requests per worker instance."
  type        = number
  default     = 8
}

variable "worker_cpu" {
  description = "Worker CPU limit."
  type        = string
  default     = "1"
}

variable "worker_memory" {
  description = "Worker memory limit."
  type        = string
  default     = "512Mi"
}

variable "worker_timeout" {
  description = "Worker request timeout."
  type        = string
  default     = "120s"
}

variable "deployment_environment" {
  description = "Deployment stage used for production-only safety checks in the Cloud Shell deploy script."
  type        = string
  default     = "development"

  validation {
    condition     = contains(["development", "staging", "production"], var.deployment_environment)
    error_message = "deployment_environment must be development, staging, or production."
  }
}

variable "worker_require_eventarc_auth" {
  description = "Whether the worker verifies Eventarc OIDC tokens in application code."
  type        = bool
  default     = false
}

variable "worker_eventarc_allowed_service_account" {
  description = "Expected Eventarc service account email for worker push requests. Defaults to the Terraform-managed Eventarc service account when empty."
  type        = string
  default     = ""
}

variable "worker_eventarc_audience" {
  description = "Expected OIDC audience for worker push requests when application-level Eventarc auth is enabled."
  type        = string
  default     = ""
}

variable "alert_notification_channels" {
  description = "Cloud Monitoring notification channel resource names for alert policies."
  type        = list(string)
  default     = []
}

variable "gateway_p95_latency_threshold_ms" {
  description = "Gateway p95 latency threshold in milliseconds for launch alerting."
  type        = number
  default     = 8000
}
