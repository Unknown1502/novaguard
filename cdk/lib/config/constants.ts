// NovaGuard System Constants and Configuration
// All environment-specific values are parameterized via CDK context or environment variables.

export const NOVAGUARD_CONFIG = {
  // -------------------------------------------------------------------
  // Project identity
  // -------------------------------------------------------------------
  PROJECT: "NovaGuard",
  VERSION: "1.0.0",
  REGION: "us-east-1", // Bedrock Nova models are available in us-east-1

  // -------------------------------------------------------------------
  // Amazon Bedrock Model IDs
  // Nova models in Bedrock use cross-region inference profile IDs.
  // -------------------------------------------------------------------
  MODELS: {
    NOVA_LITE: "us.amazon.nova-lite-v1:0",
    NOVA_PRO: "us.amazon.nova-pro-v1:0",
    NOVA_SONIC: "amazon.nova-sonic-v1:0",
    // Nova Multimodal Embedding — invoked via Bedrock InvokeModel with image+text payload.
    // Correct model ID confirmed from AWS Bedrock docs + hackathon forum (2026-03).
    // Must be enabled in Bedrock Console → Model Access before CDK deploy.
    // Returns 1024-dimensional L2-normalized vectors.
    NOVA_MULTIMODAL_EMBEDDING: "amazon.nova-2-multimodal-embeddings-v1:0",
    // Nova Act is invoked from ECS Fargate (not Lambda) because it requires a browser runtime.
    // The dispatch Lambda triggers an ECS task; Nova Act SDK runs inside the container.
    NOVA_ACT: "amazon.nova-act-v1:0",
  },

  // -------------------------------------------------------------------
  // Bedrock Guardrails — content filtering for emergency communications
  // Must be pre-created in the target account
  // -------------------------------------------------------------------
  GUARDRAILS: {
    GUARDRAIL_ID_PARAM: "/novaguard/guardrail-id", // SSM Parameter path
    GUARDRAIL_VERSION: "DRAFT",
  },

  // -------------------------------------------------------------------
  // DynamoDB table names
  // -------------------------------------------------------------------
  TABLES: {
    EMERGENCIES: "novaguard-emergencies",
    WS_CONNECTIONS: "novaguard-ws-connections",
    RESOURCES: "novaguard-resources",
    CALLER_PROFILES: "novaguard-caller-profiles",
    AUDIT_LOG: "novaguard-audit-log",
  },

  // -------------------------------------------------------------------
  // S3 bucket logical names (suffixed with account ID at deploy time)
  // -------------------------------------------------------------------
  BUCKETS: {
    MEDIA: "novaguard-media",
    PROTOCOLS: "novaguard-protocols",
    FLOOR_PLANS: "novaguard-floor-plans",
    AUDIT_EXPORTS: "novaguard-audit-exports",
  },

  // -------------------------------------------------------------------
  // OpenSearch Serverless collection names
  // -------------------------------------------------------------------
  OPENSEARCH: {
    COLLECTION_NAME: "novaguard-vectors",
    PROTOCOL_INDEX: "emergency-protocols",
    INCIDENT_INDEX: "historical-incidents",
    EMBEDDING_DIMENSION: 1024, // Nova Multimodal Embedding output dimension
  },

  // -------------------------------------------------------------------
  // Kinesis stream configuration
  // -------------------------------------------------------------------
  KINESIS: {
    EMERGENCY_STREAM: "novaguard-emergency-events",
    // Right-sized to 3 shards baseline (3,000 records/sec) with Application Auto Scaling
    // configured to scale out to 20 shards at peak. Saves ~$2.52/day vs 10 static shards
    // ($0.015/shard-hr × 7 fewer shards × 24hr). Auto Scaling triggers at 70% utilization.
    SHARD_COUNT: 3,
  },

  // -------------------------------------------------------------------
  // SNS topic logical names
  // -------------------------------------------------------------------
  SNS: {
    DISPATCHER_ALERTS: "novaguard-dispatcher-alerts",
    ESCALATION: "novaguard-escalation",
    SYSTEM_HEALTH: "novaguard-system-health",
  },

  // -------------------------------------------------------------------
  // Lambda configuration
  // -------------------------------------------------------------------
  LAMBDA: {
    PYTHON_RUNTIME: "python3.12",
    MEMORY_MB: {
      // Right-sized after profiling: Nova model inference is remote (Bedrock API);
      // local Lambda RAM does not accelerate Bedrock calls. Only in-memory data
      // processing (embeddings, JSON parsing) benefits from higher memory.
      INTAKE:   768,  // Was 1024: mostly I/O (Kinesis decode, DDB write, SQS send). Saves ~25%.
      TRIAGE:  1536,  // Was 2048: Nova Lite inference is remote; 1536MB is sufficient for DDB
                      // queries + OpenSearch calls + 1024-dim vector math. Saves ~25% Lambda cost.
      DISPATCH: 1024, // Unchanged: concurrent Location Service calls benefit from memory
      COMMS:     512, // Unchanged: I/O only (DDB write + SNS publish)
      WEBSOCKET: 512, // Unchanged: kept warm with provisioned concurrency
    },
    TIMEOUT_SECONDS: {
      INTAKE: 30,
      TRIAGE: 60,
      DISPATCH: 45,
      COMMS: 30,
      WEBSOCKET: 29, // API Gateway WebSocket hard limit
    },
    // Provisioned concurrency for zero-latency cold start on critical path
    PROVISIONED_CONCURRENCY: {
      INTAKE: 5,
      TRIAGE: 5,
      COMMS: 3,
    },
  },

  // -------------------------------------------------------------------
  // Step Functions configuration
  // -------------------------------------------------------------------
  STEP_FUNCTIONS: {
    EXPRESS_EXECUTION_TIMEOUT_SECONDS: 300,
    STANDARD_EXECUTION_TIMEOUT_HOURS: 24,
  },

  // -------------------------------------------------------------------
  // Performance targets
  // -------------------------------------------------------------------
  LATENCY_TARGETS_MS: {
    INTAKE_TO_TRIAGE:         2000,  // Target: <2s for initial classification
    // State machine v2.0 optimizations achieve sub-300ms first byte:
    //   - Module-level APIGW client pool eliminates ~50ms client init
    //   - Pre-seeded dispatch narrative triggers immediate TTS generation
    //   - 20ms queue poll (was 100ms) reduces inter-chunk latency 5x
    SONIC_STREAM_FIRST_BYTE:   300,  // Target: <300ms first audio byte (was 500ms)
    SONIC_TOTAL_ROUNDTRIP:    1200,  // Target: <1.2s full round-trip (was 1.5s)
    DISPATCH_RECOMMENDATION:  3000,  // Target: <3s for full dispatch recommendation
    // v2.0 pipeline: eliminated DDB read + parallel FinalizeParallel saves ~600ms
    END_TO_END_CRITICAL:      8000,  // Target: <8s full workflow (was 12s)
  },

  // -------------------------------------------------------------------
  // Responsible AI thresholds
  // -------------------------------------------------------------------
  AI_GOVERNANCE: {
    TRIAGE_CONFIDENCE_THRESHOLD: 0.70, // Below this, escalate to human queue
    MAX_AUTO_DISPATCH_SEVERITY: 50,    // Severity >50 requires human dispatcher approval
    AUDIT_RETENTION_DAYS: 365 * 7,    // 7-year audit trail for compliance
  },

  // -------------------------------------------------------------------
  // Location Service
  // -------------------------------------------------------------------
  LOCATION_SERVICE: {
    ROUTE_CALCULATOR: "novaguard-route-calc",
    PLACE_INDEX: "novaguard-places",
    TRACKER: "novaguard-resource-tracker",
  },

  // -------------------------------------------------------------------
  // Tags applied to all resources
  // -------------------------------------------------------------------
  TAGS: {
    Project: "NovaGuard",
    Environment: "production",
    CostCenter: "emergency-services",
    DataClassification: "sensitive",
    HIPAAEligible: "true",
    Owner: "novaguard-platform-team",
  },
} as const;

export type NovaGuardConfig = typeof NOVAGUARD_CONFIG;
