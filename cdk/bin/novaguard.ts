#!/usr/bin/env node
/**
 * NovaGuard CDK Application Entry Point
 *
 * Deployment order matters — stacks with cross-stack references
 * must be declared in dependency order.
 *
 * Stack dependency chain:
 *   CoreStack → StorageStack → AgentsStack → ApiStack → ObservabilityStack
 */

import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { CoreStack } from "../lib/stacks/core-stack";
import { StorageStack } from "../lib/stacks/storage-stack";
import { AgentsStack } from "../lib/stacks/agents-stack";
import { ApiStack } from "../lib/stacks/api-stack";
import { StreamingStack } from "../lib/stacks/streaming-stack";
import { ObservabilityStack } from "../lib/stacks/observability-stack";
import { NOVAGUARD_CONFIG } from "../lib/config/constants";

const app = new cdk.App();

// Explicit environment binding — never use process.env.CDK_DEFAULT_ACCOUNT
// for production-grade deployments. Account and region must be explicit.
const env: cdk.Environment = {
  account: app.node.tryGetContext("account") || process.env.CDK_DEPLOY_ACCOUNT,
  region: app.node.tryGetContext("region") || NOVAGUARD_CONFIG.REGION,
};

if (!env.account) {
  throw new Error(
    "AWS account ID must be provided via CDK context (--context account=<id>) " +
    "or the CDK_DEPLOY_ACCOUNT environment variable. " +
    "Account-agnostic deployments are not supported for production."
  );
}

// -----------------------------------------------------------------------
// Stack 1: Core — IAM, KMS, Guardrails, shared parameters
// -----------------------------------------------------------------------
const coreStack = new CoreStack(app, "NovaGuard-CoreStack", {
  env,
  description: "NovaGuard: Core security primitives — KMS keys, IAM roles, Bedrock Guardrails",
  terminationProtection: true,
  tags: NOVAGUARD_CONFIG.TAGS,
});

// -----------------------------------------------------------------------
// Stack 2: Storage — DynamoDB, S3, OpenSearch Serverless
// -----------------------------------------------------------------------
const storageStack = new StorageStack(app, "NovaGuard-StorageStack", {
  env,
  description: "NovaGuard: Persistent storage — DynamoDB, S3, OpenSearch Serverless vector store",
  terminationProtection: true,
  tags: NOVAGUARD_CONFIG.TAGS,
  encryptionKey: coreStack.dataEncryptionKey,
});
storageStack.addDependency(coreStack);

// -----------------------------------------------------------------------
// Stack 3: Streaming — Kinesis, EventBridge, SNS, SQS
// -----------------------------------------------------------------------
const streamingStack = new StreamingStack(app, "NovaGuard-StreamingStack", {
  env,
  description: "NovaGuard: Event streaming — Kinesis, EventBridge bus, SNS topics, SQS queues",
  terminationProtection: true,
  tags: NOVAGUARD_CONFIG.TAGS,
  encryptionKey: coreStack.dataEncryptionKey,
});
streamingStack.addDependency(coreStack);

// -----------------------------------------------------------------------
// Stack 4: Agents — Lambda functions, Step Functions State Machine
// -----------------------------------------------------------------------
const agentsStack = new AgentsStack(app, "NovaGuard-AgentsStack", {
  env,
  description: "NovaGuard: Multi-agent orchestration — Lambda agents, Step Functions workflow",
  terminationProtection: true,
  tags: NOVAGUARD_CONFIG.TAGS,
  // Pass cross-stack references
  emergenciesTable: storageStack.emergenciesTable,
  connectionsTable: storageStack.connectionsTable,
  resourcesTable: storageStack.resourcesTable,
  callerProfilesTable: storageStack.callerProfilesTable,
  auditLogTable: storageStack.auditLogTable,
  mediaBucket: storageStack.mediaBucket,
  protocolsBucket: storageStack.protocolsBucket,
  emergencyStream: streamingStack.emergencyStream,
  dispatcherAlertsTopic: streamingStack.dispatcherAlertsTopic,
  escalationTopic: streamingStack.escalationTopic,
  agentMessageQueue: streamingStack.agentMessageQueue,
  encryptionKey: coreStack.dataEncryptionKey,
  lambdaExecutionRoles: coreStack.lambdaExecutionRoles,
});
agentsStack.addDependency(storageStack);
agentsStack.addDependency(streamingStack);

// -----------------------------------------------------------------------
// Stack 5: API — API Gateway REST, WebSocket, CloudFront, WAF
// -----------------------------------------------------------------------
const apiStack = new ApiStack(app, "NovaGuard-ApiStack", {
  env,
  description: "NovaGuard: API layer — API Gateway REST/WebSocket, CloudFront CDN, WAF",
  terminationProtection: true,
  tags: NOVAGUARD_CONFIG.TAGS,
  connectWebSocketHandler: agentsStack.connectWebSocketHandler,
  disconnectWebSocketHandler: agentsStack.disconnectWebSocketHandler,
  messageWebSocketHandler: agentsStack.messageWebSocketHandler,
  callerApiHandler: agentsStack.callerApiHandler,
  connectionsTable: storageStack.connectionsTable,
});
apiStack.addDependency(agentsStack);

// -----------------------------------------------------------------------
// Stack 6: Observability — CloudWatch dashboards, X-Ray groups, alarms
// -----------------------------------------------------------------------
const observabilityStack = new ObservabilityStack(app, "NovaGuard-ObservabilityStack", {
  env,
  description: "NovaGuard: Observability — CloudWatch dashboards, X-Ray, alarms, log retention",
  terminationProtection: false, // Observability stack can be re-deployed without impact
  tags: NOVAGUARD_CONFIG.TAGS,
  stateMachineArn: agentsStack.emergencyWorkflowStateMachineArn,
  systemHealthTopic: streamingStack.systemHealthTopic,
  agentFunctions: agentsStack.allAgentFunctions,
  webSocketApi: apiStack.webSocketApi,
});
observabilityStack.addDependency(agentsStack);
observabilityStack.addDependency(apiStack);

// CDK Metadata
cdk.Tags.of(app).add("ManagedBy", "CDK");
cdk.Tags.of(app).add("CdkVersion", "2.130.0");

app.synth();
