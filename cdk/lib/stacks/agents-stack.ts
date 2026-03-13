/**
 * NovaGuard Agents Stack
 *
 * Owns the entire compute layer:
 * - 6 specialized Lambda agent functions (Intake, Triage, Dispatch, Comms, Post-Incident, Prediction)
 * - 1 ECS Fargate cluster + task definition for Nova Act CAD browser automation
 * - 4 WebSocket Lambda functions (connect, disconnect, message, authorizer)
 * - 1 REST API Lambda (caller-facing endpoints)
 * - Step Functions Express/Standard state machine
 * - Lambda Layers (Strands Agents SDK, shared utilities)
 * - EventBridge rules: Kinesis-to-StepFunctions pipe, Post-Incident trigger, Prediction schedule
 *
 * Lambda runtime: Python 3.12
 * Tracing: AWS X-Ray active tracing on all functions
 * Log groups: Encrypted with CMK, 90-day retention
 *
 * IMPORTANT — Nova Act architecture:
 *   Nova Act requires a real browser (Playwright/Chromium). Lambda has no browser runtime.
 *   Dispatch Agent calls ecs.run_task() to launch the nova-act-worker Fargate container,
 *   which runs Nova Act, automates the CAD web UI, and writes the result back to DynamoDB.
 *   Dispatch Agent polls DynamoDB for up to 8 seconds, then continues non-fatally.
 */

import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import * as logs from "aws-cdk-lib/aws-logs";
import * as events from "aws-cdk-lib/aws-events";
import * as eventsTargets from "aws-cdk-lib/aws-events-targets";
import * as kinesis from "aws-cdk-lib/aws-kinesis";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as sns from "aws-cdk-lib/aws-sns";
import * as kms from "aws-cdk-lib/aws-kms";
import * as iam from "aws-cdk-lib/aws-iam";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecrAssets from "aws-cdk-lib/aws-ecr-assets";
import * as path from "path";
import { Construct } from "constructs";
import { NOVAGUARD_CONFIG } from "../config/constants";
import { LambdaExecutionRoles } from "./core-stack";

export interface AgentsStackProps extends cdk.StackProps {
  // Storage
  emergenciesTable: dynamodb.Table;
  connectionsTable: dynamodb.Table;
  resourcesTable: dynamodb.Table;
  callerProfilesTable: dynamodb.Table;
  auditLogTable: dynamodb.Table;
  mediaBucket: s3.Bucket;
  protocolsBucket: s3.Bucket;
  // Streaming
  emergencyStream: kinesis.Stream;
  dispatcherAlertsTopic: sns.Topic;
  escalationTopic: sns.Topic;
  agentMessageQueue: sqs.Queue;
  // Security
  encryptionKey: kms.Key;
  lambdaExecutionRoles: LambdaExecutionRoles;
}

export class AgentsStack extends cdk.Stack {
  // Agent Lambdas
  public readonly intakeAgentFn: lambda.Function;
  public readonly triageAgentFn: lambda.Function;
  public readonly dispatchAgentFn: lambda.Function;
  public readonly commsAgentFn: lambda.Function;
  public readonly postIncidentFn: lambda.Function;
  public readonly predictionAgentFn: lambda.Function;

  // ECS Nova Act
  public readonly novaActCluster: ecs.Cluster;
  public readonly novaActTaskDefinition: ecs.FargateTaskDefinition;

  // WebSocket Lambdas
  public readonly connectWebSocketHandler: lambda.Function;
  public readonly disconnectWebSocketHandler: lambda.Function;
  public readonly messageWebSocketHandler: lambda.Function;

  // REST API Lambda
  public readonly callerApiHandler: lambda.Function;

  // Step Functions
  public readonly emergencyWorkflowStateMachineArn: string;

  // All functions for observability stack reference
  public readonly allAgentFunctions: lambda.Function[];

  constructor(scope: Construct, id: string, props: AgentsStackProps) {
    super(scope, id, props);

    const { lambdaExecutionRoles: roles, encryptionKey } = props;

    // =================================================================
    // SECTION 1: LAMBDA LAYERS
    // =================================================================

    // Layer 1: Strands Agents SDK + Boto3 + AWS SDK utilities
    // This layer is built separately via CodeBuild/Makefile and uploaded to S3
    // Here we reference it by version ARN after upload
    const strandsAgentsLayer = new lambda.LayerVersion(this, "StrandsAgentsLayer", {
      layerVersionName: "novaguard-strands-agents",
      description: "Strands Agents SDK, boto3, aws-xray-sdk, opensearch-py, strands-tools",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/layers/strands-agents")),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      compatibleArchitectures: [lambda.Architecture.ARM_64],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Layer 2: Shared utilities (models, observability, bedrock client)  
    const sharedUtilsLayer = new lambda.LayerVersion(this, "SharedUtilsLayer", {
      layerVersionName: "novaguard-shared-utils",
      description: "NovaGuard shared models, bedrock client, observability utilities",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/layers/shared-utils")),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      compatibleArchitectures: [lambda.Architecture.ARM_64],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const commonLayers = [strandsAgentsLayer, sharedUtilsLayer];

    // =================================================================
    // SECTION 2: COMMON ENVIRONMENT VARIABLES
    // =================================================================

    const commonEnv = {
      REGION: this.region,
      ACCOUNT_ID: this.account,
      EMERGENCIES_TABLE: props.emergenciesTable.tableName,
      CONNECTIONS_TABLE: props.connectionsTable.tableName,
      RESOURCES_TABLE: props.resourcesTable.tableName,
      CALLER_PROFILES_TABLE: props.callerProfilesTable.tableName,
      AUDIT_LOG_TABLE: props.auditLogTable.tableName,
      MEDIA_BUCKET: props.mediaBucket.bucketName,
      PROTOCOLS_BUCKET: props.protocolsBucket.bucketName,
      AGENT_MESSAGE_QUEUE_URL: props.agentMessageQueue.queueUrl,
      DISPATCHER_ALERTS_TOPIC_ARN: props.dispatcherAlertsTopic.topicArn,
      ESCALATION_TOPIC_ARN: props.escalationTopic.topicArn,
      NOVA_LITE_MODEL_ID: NOVAGUARD_CONFIG.MODELS.NOVA_LITE,
      NOVA_SONIC_MODEL_ID: NOVAGUARD_CONFIG.MODELS.NOVA_SONIC,
      NOVA_EMBEDDING_MODEL_ID: NOVAGUARD_CONFIG.MODELS.NOVA_MULTIMODAL_EMBEDDING,
      TRIAGE_CONFIDENCE_THRESHOLD: String(NOVAGUARD_CONFIG.AI_GOVERNANCE.TRIAGE_CONFIDENCE_THRESHOLD),
      MAX_AUTO_DISPATCH_SEVERITY: String(NOVAGUARD_CONFIG.AI_GOVERNANCE.MAX_AUTO_DISPATCH_SEVERITY),
      ROUTE_CALCULATOR_NAME: NOVAGUARD_CONFIG.LOCATION_SERVICE.ROUTE_CALCULATOR,
      PLACE_INDEX_NAME: NOVAGUARD_CONFIG.LOCATION_SERVICE.PLACE_INDEX,
      OPENSEARCH_COLLECTION_NAME: NOVAGUARD_CONFIG.OPENSEARCH.COLLECTION_NAME,
      OPENSEARCH_PROTOCOL_INDEX: NOVAGUARD_CONFIG.OPENSEARCH.PROTOCOL_INDEX,
      POWERTOOLS_SERVICE_NAME: "novaguard",
      POWERTOOLS_LOG_LEVEL: "INFO",
      POWERTOOLS_TRACER_CAPTURE_RESPONSE: "true",
      POWERTOOLS_TRACER_CAPTURE_ERROR: "true",
    };

    // =================================================================
    // SECTION 3: HELPER FOR LOG GROUP CREATION
    // =================================================================

    const createLogGroup = (logicalId: string, logGroupName: string): logs.LogGroup =>
      new logs.LogGroup(this, logicalId, {
        logGroupName,
        retention: logs.RetentionDays.THREE_MONTHS, // 90 days for operational logs
        encryptionKey,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });

    // =================================================================
    // SECTION 4: AGENT LAMBDA FUNCTIONS
    // =================================================================

    // -----------------------------------------------------------------
    // Intake Agent
    // Trigger: Kinesis event source (emergency-events stream)
    // Purpose: Parse incoming emergency messages, extract structured data,
    //          run multimodal embedding on attached images, initiate
    //          Step Functions workflow execution.
    // -----------------------------------------------------------------
    const intakeLogGroup = createLogGroup("IntakeAgentLogs", "/aws/lambda/novaguard-intake-agent");

    this.intakeAgentFn = new lambda.Function(this, "IntakeAgentFn", {
      functionName: "novaguard-intake-agent",
      description: "NovaGuard Intake Agent: Emergency intake, multimodal embedding, workflow trigger",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/intake_agent")),
      layers: commonLayers,
      role: roles.intakeAgent,
      memorySize: NOVAGUARD_CONFIG.LAMBDA.MEMORY_MB.INTAKE,
      timeout: cdk.Duration.seconds(NOVAGUARD_CONFIG.LAMBDA.TIMEOUT_SECONDS.INTAKE),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: intakeLogGroup,
      environment: {
        ...commonEnv,
        AGENT_NAME: "intake",
        STEP_FUNCTIONS_ARN_PARAM: "/novaguard/emergency-workflow-arn",
      },
      reservedConcurrentExecutions: 500, // Cap to prevent runaway Bedrock costs
    });

    // Kinesis trigger: batch of 10 records, bisect-on-error for fault isolation
    this.intakeAgentFn.addEventSource(new lambdaEventSources.KinesisEventSource(props.emergencyStream, {
      startingPosition: lambda.StartingPosition.LATEST,
      batchSize: 10,
      bisectBatchOnError: true, // Bisect batch to isolate failed records
      retryAttempts: 3,
      parallelizationFactor: 5, // Process 5 shard batches in parallel
      reportBatchItemFailures: true, // Partial batch success for fine-grained retries
    }));

    // Provisioned concurrency to eliminate cold starts on critical path
    const intakeAlias = new lambda.Alias(this, "IntakeAgentAlias", {
      aliasName: "live",
      version: this.intakeAgentFn.currentVersion,
    });
    intakeAlias.addAutoScaling({
      minCapacity: NOVAGUARD_CONFIG.LAMBDA.PROVISIONED_CONCURRENCY.INTAKE,
      maxCapacity: 100,
    });

    // -----------------------------------------------------------------
    // Triage Agent
    // Trigger: SQS (agent-message-queue, filtered for triage-request events)
    // Purpose: Classify emergency severity (0-100), estimate resource needs,
    //          retrieve relevant protocols from OpenSearch, return structured
    //          triage result to Step Functions.
    // -----------------------------------------------------------------
    const triageLogGroup = createLogGroup("TriageAgentLogs", "/aws/lambda/novaguard-triage-agent");

    this.triageAgentFn = new lambda.Function(this, "TriageAgentFn", {
      functionName: "novaguard-triage-agent",
      description: "NovaGuard Triage Agent: Severity scoring, resource estimation, protocol retrieval",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/triage_agent")),
      layers: commonLayers,
      role: roles.triageAgent,
      memorySize: NOVAGUARD_CONFIG.LAMBDA.MEMORY_MB.TRIAGE,
      timeout: cdk.Duration.seconds(NOVAGUARD_CONFIG.LAMBDA.TIMEOUT_SECONDS.TRIAGE),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: triageLogGroup,
      environment: {
        ...commonEnv,
        AGENT_NAME: "triage",
      },
      reservedConcurrentExecutions: 500,
    });

    const triageAlias = new lambda.Alias(this, "TriageAgentAlias", {
      aliasName: "live",
      version: this.triageAgentFn.currentVersion,
    });
    triageAlias.addAutoScaling({
      minCapacity: NOVAGUARD_CONFIG.LAMBDA.PROVISIONED_CONCURRENCY.TRIAGE,
      maxCapacity: 100,
    });

    // -----------------------------------------------------------------
    // Dispatch Agent
    // Trigger: Invoked by Step Functions (synchronous task)
    // Purpose: Find nearest available resources via Location Service,
    //          use Nova Act to check dashboard capacity, generate dispatch
    //          recommendation object with resource assignments and ETAs.
    // -----------------------------------------------------------------
    const dispatchLogGroup = createLogGroup("DispatchAgentLogs", "/aws/lambda/novaguard-dispatch-agent");

    this.dispatchAgentFn = new lambda.Function(this, "DispatchAgentFn", {
      functionName: "novaguard-dispatch-agent",
      description: "NovaGuard Dispatch Agent: Resource location, Nova Act dashboard check, dispatch optimization",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/dispatch_agent")),
      layers: commonLayers,
      role: roles.dispatchAgent,
      memorySize: NOVAGUARD_CONFIG.LAMBDA.MEMORY_MB.DISPATCH,
      timeout: cdk.Duration.seconds(NOVAGUARD_CONFIG.LAMBDA.TIMEOUT_SECONDS.DISPATCH),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: dispatchLogGroup,
      environment: {
        ...commonEnv,
        AGENT_NAME: "dispatch",
        NOVA_ACT_HEADLESS: "true",
        HOSPITAL_DASHBOARD_URL: "https://dispatch-sim.novaguard.local/hospital-capacity",
        DISPATCH_DASHBOARD_URL: "https://dispatch-sim.novaguard.local/cad",
        // ECS Nova Act worker env vars — resolved after ECS resources are created below
        // These are patched in via addEnvironment() after the ECS section.
      },
      reservedConcurrentExecutions: 300,
    });

    // -----------------------------------------------------------------
    // Communications Agent
    // Trigger: Step Functions + direct WebSocket message forwarding
    // Purpose: Sonic TTS/STT voice bridge, send caller updates,
    //          translation, push dispatcher audio to WebSocket clients.
    // -----------------------------------------------------------------
    const commsLogGroup = createLogGroup("CommsAgentLogs", "/aws/lambda/novaguard-comms-agent");

    this.commsAgentFn = new lambda.Function(this, "CommsAgentFn", {
      functionName: "novaguard-comms-agent",
      description: "NovaGuard Communications Agent: Sonic voice bridge, WebSocket push, Translate, caller updates",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/comms_agent")),
      layers: commonLayers,
      role: roles.commsAgent,
      memorySize: NOVAGUARD_CONFIG.LAMBDA.MEMORY_MB.COMMS,
      timeout: cdk.Duration.seconds(NOVAGUARD_CONFIG.LAMBDA.TIMEOUT_SECONDS.COMMS),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: commsLogGroup,
      environment: {
        ...commonEnv,
        AGENT_NAME: "comms",
        // WebSocket API endpoint injected after ApiStack deploys
        // Set via: aws ssm put-parameter --name /novaguard/ws-endpoint ...
        WS_ENDPOINT_PARAM: "/novaguard/ws-endpoint",
      },
      reservedConcurrentExecutions: 500,
    });

    const commsAlias = new lambda.Alias(this, "CommsAgentAlias", {
      aliasName: "live",
      version: this.commsAgentFn.currentVersion,
    });
    commsAlias.addAutoScaling({
      minCapacity: NOVAGUARD_CONFIG.LAMBDA.PROVISIONED_CONCURRENCY.COMMS,
      maxCapacity: 100,
    });

    // =================================================================
    // SECTION 4b: ECS FARGATE — NOVA ACT CAD BROWSER AUTOMATION WORKER
    // Nova Act requires a real browser (Playwright + Chromium).
    // Lambda cannot host a browser runtime. This Fargate task is launched
    // by Dispatch Agent via ecs.run_task(), writes result to DynamoDB.
    // =================================================================

    // Use the default VPC for simplicity; in production, pass in a real VPC.
    const vpc = ec2.Vpc.fromLookup(this, "DefaultVpc", { isDefault: true });

    this.novaActCluster = new ecs.Cluster(this, "NovaActCluster", {
      clusterName: "novaguard-nova-act",
      vpc,
      containerInsights: true,
    });

    // Build the container image from the nova_act_worker/ directory
    const novaActImage = new ecrAssets.DockerImageAsset(this, "NovaActWorkerImage", {
      directory: path.join(__dirname, "../../../lambdas/nova_act_worker"),
      assetName: "novaguard-nova-act-worker",
    });

    this.novaActTaskDefinition = new ecs.FargateTaskDefinition(this, "NovaActTaskDef", {
      family: "novaguard-nova-act-worker",
      cpu: 1024,    // 1 vCPU — sufficient for single Chromium instance
      memoryLimitMiB: 2048, // 2 GB — Chromium + Nova Act SDK
    });

    this.novaActTaskDefinition.addContainer("nova-act-worker", {
      image: ecs.ContainerImage.fromDockerImageAsset(novaActImage),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "novaguard-nova-act",
        logGroup: createLogGroup("NovaActWorkerLogs", "/ecs/novaguard-nova-act-worker"),
      }),
      environment: {
        REGION: this.region,
        EMERGENCIES_TABLE: props.emergenciesTable.tableName,
        NOVA_ACT_HEADLESS: "true",
        DISPATCH_DASHBOARD_URL: "https://dispatch-sim.novaguard.local/cad",
      },
      readonlyRootFilesystem: false, // Playwright writes temp files
    });

    // Grant Fargate task DynamoDB write access for result write-back
    props.emergenciesTable.grantWriteData(this.novaActTaskDefinition.taskRole);

    // Patch ECS ARNs into Dispatch Agent environment now that resources exist
    const defaultSubnetIds = vpc.publicSubnets.map((s: ec2.ISubnet) => s.subnetId).join(",");
    this.dispatchAgentFn.addEnvironment("ECS_NOVA_ACT_CLUSTER_ARN", this.novaActCluster.clusterArn);
    this.dispatchAgentFn.addEnvironment("ECS_NOVA_ACT_TASK_DEF_ARN", this.novaActTaskDefinition.taskDefinitionArn);
    this.dispatchAgentFn.addEnvironment("ECS_NOVA_ACT_SUBNET_IDS", defaultSubnetIds);
    this.dispatchAgentFn.addEnvironment("ECS_NOVA_ACT_POLL_TIMEOUT", "8");
    this.dispatchAgentFn.addEnvironment("ECS_NOVA_ACT_POLL_INTERVAL", "0.5");

    // Grant Dispatch Agent permission to run the ECS task
    this.dispatchAgentFn.addToRolePolicy(new iam.PolicyStatement({
      sid: "RunNovaActEcsTask",
      actions: ["ecs:RunTask", "iam:PassRole"],
      resources: [
        this.novaActTaskDefinition.taskDefinitionArn,
        this.novaActTaskDefinition.taskRole.roleArn,
        this.novaActTaskDefinition.executionRole?.roleArn ?? "*",
      ],
    }));

    // =================================================================
    // SECTION 4c: POST-INCIDENT AGENT (Agent 5)
    // Triggered asynchronously via EventBridge after every completed emergency.
    // Off the critical path — generates after-action report, PII redaction,
    // outcome embedding → OpenSearch learning loop.
    // =================================================================

    const postIncidentLogGroup = createLogGroup("PostIncidentLogs", "/aws/lambda/novaguard-post-incident");

    this.postIncidentFn = new lambda.Function(this, "PostIncidentFn", {
      functionName: "novaguard-post-incident",
      description: "NovaGuard Post-Incident Agent: After-action report, PII redaction, learning loop embedding",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/post_incident")),
      layers: commonLayers,
      role: roles.dispatchAgent, // Reuse dispatch role (has Bedrock + DDB + OpenSearch access)
      memorySize: 1024,
      timeout: cdk.Duration.seconds(300), // 5 min — report generation is non-critical path
      tracing: lambda.Tracing.ACTIVE,
      logGroup: postIncidentLogGroup,
      environment: {
        ...commonEnv,
        AGENT_NAME: "post_incident",
      },
      reservedConcurrentExecutions: 50,
    });

    // EventBridge rule: trigger Post-Incident Agent for every completed emergency
    const postIncidentRule = new events.Rule(this, "PostIncidentTriggerRule", {
      ruleName: "novaguard-post-incident-trigger",
      description: "Triggers Post-Incident Agent after every NovaGuard emergency completes",
      eventPattern: {
        source: ["novaguard.dispatch"],
        detailType: ["EmergencyCompleted"],
      },
    });
    postIncidentRule.addTarget(new eventsTargets.LambdaFunction(this.postIncidentFn, {
      retryAttempts: 2,
    }));

    // =================================================================
    // SECTION 4d: PREDICTION AGENT (Agent 6)
    // Scheduled every 4 hours via EventBridge. Uses Nova 2 Lite 1M-token
    // context window to analyze 30-day incident history and generate
    // resource pre-positioning recommendations for dispatchers.
    // =================================================================

    const predictionLogGroup = createLogGroup("PredictionAgentLogs", "/aws/lambda/novaguard-prediction-agent");

    this.predictionAgentFn = new lambda.Function(this, "PredictionAgentFn", {
      functionName: "novaguard-prediction-agent",
      description: "NovaGuard Prediction Agent: 1M-context resource pre-positioning via Nova 2 Lite",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/prediction_agent")),
      layers: commonLayers,
      role: roles.dispatchAgent, // Has Bedrock + DDB + SNS access
      memorySize: 512,
      timeout: cdk.Duration.seconds(600), // 10 min — processes 30-day corpus
      tracing: lambda.Tracing.ACTIVE,
      logGroup: predictionLogGroup,
      environment: {
        ...commonEnv,
        AGENT_NAME: "prediction",
        PREDICTION_TABLE: props.emergenciesTable.tableName, // Reads from main emergencies table
        DISPATCHER_ALERTS_TOPIC_ARN: props.dispatcherAlertsTopic.topicArn,
        PREDICTION_CONFIDENCE_THRESHOLD: "0.85",
        PREDICTION_HISTORY_DAYS: "30",
      },
      reservedConcurrentExecutions: 5, // Low concurrency — scheduled, not real-time
    });

    // Scheduled EventBridge rule: every 4 hours
    const predictionScheduleRule = new events.Rule(this, "PredictionScheduleRule", {
      ruleName: "novaguard-prediction-schedule",
      description: "Triggers Prediction Agent every 4 hours for resource pre-positioning analysis",
      schedule: events.Schedule.rate(cdk.Duration.hours(4)),
    });
    predictionScheduleRule.addTarget(new eventsTargets.LambdaFunction(this.predictionAgentFn, {
      retryAttempts: 1,
    }));

    // =================================================================
    // SECTION 5: WEBSOCKET LAMBDA FUNCTIONS
    // =================================================================

    const wsLogGroup = createLogGroup("WebSocketLogs", "/aws/lambda/novaguard-websocket");

    this.connectWebSocketHandler = new lambda.Function(this, "WsConnectFn", {
      functionName: "novaguard-ws-connect",
      description: "NovaGuard WebSocket $connect handler — registers connection in DynamoDB",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "connect.lambda_handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/websocket")),
      layers: commonLayers,
      role: roles.webSocketConnect,
      memorySize: NOVAGUARD_CONFIG.LAMBDA.MEMORY_MB.COMMS,
      timeout: cdk.Duration.seconds(NOVAGUARD_CONFIG.LAMBDA.TIMEOUT_SECONDS.WEBSOCKET),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: wsLogGroup,
      environment: { ...commonEnv },
    });

    this.disconnectWebSocketHandler = new lambda.Function(this, "WsDisconnectFn", {
      functionName: "novaguard-ws-disconnect",
      description: "NovaGuard WebSocket $disconnect handler — cleans up connection record",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "disconnect.lambda_handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/websocket")),
      layers: commonLayers,
      role: roles.webSocketDisconnect,
      memorySize: NOVAGUARD_CONFIG.LAMBDA.MEMORY_MB.COMMS,
      timeout: cdk.Duration.seconds(NOVAGUARD_CONFIG.LAMBDA.TIMEOUT_SECONDS.WEBSOCKET),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: wsLogGroup,
      environment: { ...commonEnv },
    });

    this.messageWebSocketHandler = new lambda.Function(this, "WsMessageFn", {
      functionName: "novaguard-ws-message",
      description: "NovaGuard WebSocket $default message handler — routes messages to Kinesis",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "message.lambda_handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/websocket")),
      layers: commonLayers,
      role: roles.webSocketMessage,
      memorySize: 512,
      timeout: cdk.Duration.seconds(NOVAGUARD_CONFIG.LAMBDA.TIMEOUT_SECONDS.WEBSOCKET),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: wsLogGroup,
      environment: {
        ...commonEnv,
        EMERGENCY_STREAM_NAME: props.emergencyStream.streamName,
      },
    });

    // =================================================================
    // SECTION 6: REST API LAMBDA (Caller-facing endpoints)
    // =================================================================

    const apiLogGroup = createLogGroup("CallerApiLogs", "/aws/lambda/novaguard-caller-api");

    this.callerApiHandler = new lambda.Function(this, "CallerApiFn", {
      functionName: "novaguard-caller-api",
      description: "NovaGuard REST API handler — emergency status queries, presigned URLs, profile management",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../../lambdas/caller_api")),
      layers: commonLayers,
      role: roles.callerApi,
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: apiLogGroup,
      environment: {
        ...commonEnv,
        MEDIA_BUCKET_URL_EXPIRY_SECONDS: "300",
      },
    });

    // =================================================================
    // SECTION 7: STEP FUNCTIONS STATE MACHINE
    // =================================================================

    const stateMachineLogGroup = createLogGroup("StateMachineLogs", "/aws/states/novaguard-emergency-workflow");

    // ---- Task definitions (Lambda invocations) ----------------------

    // Intake task — already done via Kinesis trigger, passed in event
    // Step Functions receives the pre-processed intake result from Intake Agent

    // Triage task — synchronous Lambda invoke with retry and catch
    const triageTask = new tasks.LambdaInvoke(this, "TriageTask", {
      lambdaFunction: this.triageAgentFn,
      comment: "Classify emergency severity (0-100) using Nova 2 Lite reasoning",
      payloadResponseOnly: true,
      retryOnServiceExceptions: true,
      taskTimeout: sfn.Timeout.duration(cdk.Duration.seconds(90)),
    }).addRetry({
      errors: ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException"],
      interval: cdk.Duration.seconds(2),
      maxAttempts: 3,
      backoffRate: 2,
    }).addCatch(new sfn.Pass(this, "TriageFallback", {
      parameters: {
        "severity_score": 75, // Conservative fallback: treat as high severity
        "confidence": 0,
        "triage_fallback": true,
        "fallback_reason.$": "$.Cause",
      },
      resultPath: "$.triage_result",
    }), {
      errors: ["States.ALL"],
      resultPath: "$.triage_error",
    });

    // Human approval gate — for high-severity emergencies, require dispatcher confirmation
    // Uses Step Functions waitForTaskToken pattern with 5-minute timeout
    const humanApprovalTask = new tasks.SqsSendMessage(this, "HumanApprovalTask", {
      queue: props.agentMessageQueue,
      messageBody: sfn.TaskInput.fromObject({
        type: "DISPATCHER_APPROVAL_REQUEST",
        "emergency_id.$": "$.emergency_id",
        "triage_result.$": "$.triage_result",
        "task_token.$": sfn.JsonPath.taskToken,
        "expires_at.$": sfn.JsonPath.stringAt("$$.Execution.StartTime"),
      }),
      integrationPattern: sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
      taskTimeout: sfn.Timeout.duration(cdk.Duration.minutes(5)), // Dispatcher has 5 min to approve
      comment: "Wait for dispatcher approval for high-severity emergencies",
    }).addRetry({
      errors: ["States.TaskFailed"],
      interval: cdk.Duration.seconds(5),
      maxAttempts: 0, // No retry — expired approval defaults to auto-dispatch
    }).addCatch(new sfn.Pass(this, "ApprovalTimeout", {
      parameters: {
        "auto_approved": true,
        "approval_reason": "TIMEOUT_AUTO_APPROVE",
      },
      resultPath: "$.approval_result",
    }), {
      errors: ["States.HeartbeatTimeout", "States.Timeout"],
    });

    // Dispatch task
    const dispatchTask = new tasks.LambdaInvoke(this, "DispatchTask", {
      lambdaFunction: this.dispatchAgentFn,
      comment: "Find nearest resources, check capacities via Nova Act, generate dispatch plan",
      payloadResponseOnly: true,
      retryOnServiceExceptions: true,
      taskTimeout: sfn.Timeout.duration(cdk.Duration.seconds(120)),
    }).addRetry({
      errors: ["Lambda.ServiceException", "Lambda.AWSLambdaException"],
      interval: cdk.Duration.seconds(3),
      maxAttempts: 2,
      backoffRate: 2,
    }).addCatch(new sfn.Pass(this, "DispatchFallback", {
      parameters: {
        "dispatch_fallback": true,
        "message": "Dispatch optimization failed — manual dispatch required",
        "fallback_reason.$": "$.Cause",
      },
      resultPath: "$.dispatch_result",
    }), {
      errors: ["States.ALL"],
      resultPath: "$.dispatch_error",
    });

    // Comms task — async fan-out via SNS + WebSocket push
    const commsTask = new tasks.LambdaInvoke(this, "CommsTask", {
      lambdaFunction: this.commsAgentFn,
      comment: "Sonic voice bridge, caller update, dispatcher notification",
      payloadResponseOnly: true,
      retryOnServiceExceptions: true,
      taskTimeout: sfn.Timeout.duration(cdk.Duration.seconds(45)),
    }).addRetry({
      errors: ["Lambda.ServiceException"],
      interval: cdk.Duration.seconds(1),
      maxAttempts: 3,
      backoffRate: 1.5,
    });

    // Monitor task — periodic status check using a Lambda polling loop
    const monitorTask = new tasks.LambdaInvoke(this, "MonitorTask", {
      lambdaFunction: this.commsAgentFn,
      comment: "Monitor emergency resolution and send periodic updates",
      payloadResponseOnly: true,
      retryOnServiceExceptions: true,
      taskTimeout: sfn.Timeout.duration(cdk.Duration.seconds(30)),
    });

    // ---- Choice states -----------------------------------------------

    // Is triage confidence above threshold?
    const confidenceCheck = new sfn.Choice(this, "ConfidenceCheck", {
      comment: "Route to human queue if AI triage confidence < 70%",
    });

    // Is severity above auto-dispatch threshold?
    const severityCheck = new sfn.Choice(this, "SeverityCheck", {
      comment: "Require dispatcher approval for severity > 50",
    });

    // Is emergency resolved?
    const resolutionCheck = new sfn.Choice(this, "ResolutionCheck", {
      comment: "Continue monitoring or close emergency",
    });

    // ---- Wait state for resolution polling ---------------------------

    const waitForResolution = new sfn.Wait(this, "WaitForResolution", {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(60)),
      comment: "Poll every 60 seconds for emergency resolution",
    });

    // ---- Terminal states ---------------------------------------------

    const emergencyResolved = new sfn.Succeed(this, "EmergencyResolved", {
      comment: "Emergency successfully resolved and closed",
    });

    const escalateToHuman = new sfn.Pass(this, "EscalateToHuman", {
      parameters: {
        "escalation_reason": "AI_CONFIDENCE_BELOW_THRESHOLD",
        "escalated_at.$": "$$.State.EnteredTime",
      },
      resultPath: "$.escalation",
      comment: "Low-confidence triage — route to human operator queue",
    });

    const escalateAndNotify = new tasks.SnsPublish(this, "SendEscalationAlert", {
      topic: props.escalationTopic,
      message: sfn.TaskInput.fromObject({
        "default.$": sfn.JsonPath.format(
          "LOW CONFIDENCE TRIAGE — Emergency {} requires human review",
          sfn.JsonPath.stringAt("$.emergency_id")
        ),
        "emergency_id.$": "$.emergency_id",
        "confidence.$": "$.triage_result.confidence",
        "escalation_reason.$": "$.escalation.escalation_reason",
      }),
      subject: "NovaGuard: Low Confidence Emergency Escalation",
    });

    // ---- Workflow definition -----------------------------------------

    const workflowDefinition = triageTask
      .next(
        confidenceCheck
          .when(
            sfn.Condition.numberLessThan("$.triage_result.confidence", NOVAGUARD_CONFIG.AI_GOVERNANCE.TRIAGE_CONFIDENCE_THRESHOLD),
            escalateToHuman.next(escalateAndNotify)
          )
          .otherwise(
            severityCheck
              .when(
                sfn.Condition.numberGreaterThan("$.triage_result.severity_score", NOVAGUARD_CONFIG.AI_GOVERNANCE.MAX_AUTO_DISPATCH_SEVERITY),
                humanApprovalTask.next(dispatchTask)
              )
              .otherwise(dispatchTask)
              .afterwards()
              .next(
                commsTask.next(
                  waitForResolution.next(
                    monitorTask.next(
                      resolutionCheck
                        .when(sfn.Condition.stringEquals("$.resolution_status", "RESOLVED"), emergencyResolved)
                        .otherwise(waitForResolution)
                    )
                  )
                )
              )
          )
      );

    // ---- State machine -----------------------------------------------

    const stateMachine = new sfn.StateMachine(this, "EmergencyWorkflow", {
      stateMachineName: "novaguard-emergency-workflow",
      stateMachineType: sfn.StateMachineType.EXPRESS,
      definitionBody: sfn.DefinitionBody.fromChainable(workflowDefinition),
      timeout: cdk.Duration.seconds(NOVAGUARD_CONFIG.STEP_FUNCTIONS.EXPRESS_EXECUTION_TIMEOUT_SECONDS),
      tracingEnabled: true,
      logs: {
        destination: stateMachineLogGroup,
        level: sfn.LogLevel.ALL,
        includeExecutionData: true,
      },
      role: props.lambdaExecutionRoles.stepFunctionsExecution,
    });

    this.emergencyWorkflowStateMachineArn = stateMachine.stateMachineArn;

    // Store state machine ARN in SSM so Intake Agent can start executions
    const ssm = require("aws-cdk-lib/aws-ssm");
    new ssm.StringParameter(this, "StateMachineArnParam", {
      parameterName: "/novaguard/emergency-workflow-arn",
      stringValue: stateMachine.stateMachineArn,
      description: "NovaGuard emergency workflow state machine ARN",
    });

    // =================================================================
    // SECTION 8: ALL FUNCTIONS EXPORT
    // =================================================================

    this.allAgentFunctions = [
      this.intakeAgentFn,
      this.triageAgentFn,
      this.dispatchAgentFn,
      this.commsAgentFn,
      this.postIncidentFn,
      this.predictionAgentFn,
      this.connectWebSocketHandler,
      this.disconnectWebSocketHandler,
      this.messageWebSocketHandler,
      this.callerApiHandler,
    ];

    // =================================================================
    // STACK OUTPUTS
    // =================================================================

    new cdk.CfnOutput(this, "StateMachineArn", {
      exportName: "NovaGuard-StateMachineArn",
      value: stateMachine.stateMachineArn,
    });

    new cdk.CfnOutput(this, "IntakeAgentArn", {
      exportName: "NovaGuard-IntakeAgentArn",
      value: this.intakeAgentFn.functionArn,
    });

    new cdk.CfnOutput(this, "TriageAgentArn", {
      exportName: "NovaGuard-TriageAgentArn",
      value: this.triageAgentFn.functionArn,
    });

    new cdk.CfnOutput(this, "DispatchAgentArn", {
      exportName: "NovaGuard-DispatchAgentArn",
      value: this.dispatchAgentFn.functionArn,
    });

    new cdk.CfnOutput(this, "CommsAgentArn", {
      exportName: "NovaGuard-CommsAgentArn",
      value: this.commsAgentFn.functionArn,
    });

    new cdk.CfnOutput(this, "PostIncidentAgentArn", {
      exportName: "NovaGuard-PostIncidentAgentArn",
      value: this.postIncidentFn.functionArn,
    });

    new cdk.CfnOutput(this, "PredictionAgentArn", {
      exportName: "NovaGuard-PredictionAgentArn",
      value: this.predictionAgentFn.functionArn,
    });

    new cdk.CfnOutput(this, "NovaActClusterArn", {
      exportName: "NovaGuard-NovaActClusterArn",
      value: this.novaActCluster.clusterArn,
    });

    new cdk.CfnOutput(this, "NovaActTaskDefArn", {
      exportName: "NovaGuard-NovaActTaskDefArn",
      value: this.novaActTaskDefinition.taskDefinitionArn,
    });
  }
}
