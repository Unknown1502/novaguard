/**
 * NovaGuard Multi-Agent Pipeline Stack
 *
 * Full 3-agent pipeline: Voice/Text → Triage → Dispatch → Communications
 *
 *   [Caller audio] → Nova Sonic → transcript
 *   [description]  → Triage Agent (Nova 2 Lite) → severity + type
 *                 → Dispatch Agent (Nova 2 Lite) → units + ETA
 *                 → Comms Agent (Nova 2 Lite) → caller instructions + responder briefing
 *
 * API endpoints:
 *   POST /triage     — single triage agent
 *   POST /transcribe — Nova Sonic voice → transcript
 *   POST /dispatch   — dispatch agent
 *   POST /pipeline   — full 3-agent pipeline (the judge demo endpoint)
 *   GET  /health     — liveness
 *
 * Deploy: cdk deploy (from novaguard/mvp/)
 */

import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import * as sns from "aws-cdk-lib/aws-sns";
import * as snsSubscriptions from "aws-cdk-lib/aws-sns-subscriptions";
import * as location from "aws-cdk-lib/aws-location";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { Construct } from "constructs";
import * as path from "path";

export class NovaGuardMvpStack extends cdk.Stack {
  public readonly apiUrl: string;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ================================================================
    // 1. DynamoDB — Emergency records (shared across all agents)
    // ================================================================
    const table = new dynamodb.Table(this, "EmergenciesTable", {
      tableName: "novaguard-emergencies",
      partitionKey: { name: "emergency_id", type: dynamodb.AttributeType.STRING },
      sortKey:      { name: "version",       type: dynamodb.AttributeType.NUMBER },
      billingMode:  dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: "ttl_epoch",
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: false },
    });

    table.addGlobalSecondaryIndex({
      indexName: "status-severity-index",
      partitionKey: { name: "status",         type: dynamodb.AttributeType.STRING },
      sortKey:      { name: "severity_score", type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ================================================================
    // 2. Shared IAM Role — all Lambdas use a single role (simpler demo)
    // ================================================================
    const role = new iam.Role(this, "NovaGuardLambdaRole", {
      roleName: "novaguard-lambda-role",
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole"),
        iam.ManagedPolicy.fromAwsManagedPolicyName("AWSXRayDaemonWriteAccess"),
      ],
    });

    // Bedrock — all Nova models (Lite cross-region + Sonic bidirectional)
    role.addToPolicy(new iam.PolicyStatement({
      sid: "InvokeNovaModels",
      effect: iam.Effect.ALLOW,
      actions: [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:InvokeModelWithBidirectionalStream",
      ],
      resources: [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:*:inference-profile/*",
      ],
    }));

    // DynamoDB
    role.addToPolicy(new iam.PolicyStatement({
      sid: "DynamoAccess",
      effect: iam.Effect.ALLOW,
      actions: ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem",
                "dynamodb:Query", "dynamodb:Scan"],
      resources: [table.tableArn, `${table.tableArn}/index/*`],
    }));

    // Lambda:InvokeFunction — pipeline handler calls sibling Lambdas
    role.addToPolicy(new iam.PolicyStatement({
      sid: "InvokeSiblingLambdas",
      effect: iam.Effect.ALLOW,
      actions: ["lambda:InvokeFunction"],
      resources: [`arn:aws:lambda:${this.region}:${this.account}:function:novaguard-*`],
    }));

    // Step Functions — start + describe executions
    role.addToPolicy(new iam.PolicyStatement({
      sid: "StepFunctions",
      effect: iam.Effect.ALLOW,
      actions: ["states:StartExecution", "states:DescribeExecution", "states:StopExecution", "states:GetExecutionHistory"],
      resources: [
        `arn:aws:states:${this.region}:${this.account}:stateMachine:novaguard-*`,
        `arn:aws:states:${this.region}:${this.account}:execution:novaguard-*:*`,
      ],
    }));

    // Bedrock AgentCore — invoke Nova Act workflows deployed via `act workflow deploy`
    role.addToPolicy(new iam.PolicyStatement({
      sid: "AgentCoreInvoke",
      effect: iam.Effect.ALLOW,
      actions: ["bedrock-agentcore:InvokeAgentRuntime"],
      resources: ["*"],
    }));

    // CloudWatch metrics
    role.addToPolicy(new iam.PolicyStatement({
      sid: "CloudWatchMetrics",
      effect: iam.Effect.ALLOW,
      actions: ["cloudwatch:PutMetricData"],
      resources: ["*"],
    }));

    // Amazon Translate — auto-detect language and translate emergency text to English
    role.addToPolicy(new iam.PolicyStatement({
      sid: "TranslateAccess",
      effect: iam.Effect.ALLOW,
      actions: ["translate:TranslateText", "translate:DetectDominantLanguage"],
      resources: ["*"],
    }));

    // Amazon SNS — publish dispatcher alerts when comms agent completes
    role.addToPolicy(new iam.PolicyStatement({
      sid: "SnsPublish",
      effect: iam.Effect.ALLOW,
      actions: ["sns:Publish", "sns:ListTopics"],
      resources: ["*"],
    }));

    // Amazon Location Service — route ETA calculation
    role.addToPolicy(new iam.PolicyStatement({
      sid: "LocationRouting",
      effect: iam.Effect.ALLOW,
      actions: [
        "geo:CalculateRoute",
        "geo:GetPlace",
        "geo:SearchPlaceIndexForText",
      ],
      resources: ["*"],
    }));

    // Amazon ECS — run Nova Act Fargate tasks
    // NOTE: iam:PassRole for nova-act roles added AFTER those roles are declared below.
    role.addToPolicy(new iam.PolicyStatement({
      sid: "EcsRunNovaAct",
      effect: iam.Effect.ALLOW,
      actions: ["ecs:RunTask", "ecs:DescribeTasks", "ecs:StopTask"],
      resources: ["*"],
    }));

    // ================================================================
    // 3a. SNS Topic — dispatcher alerts (SMS/email/push per emergency)
    // ================================================================
    // NOTE on VPC: ECS Fargate requires a VPC. We look up the default VPC here.
    // If you need to create a dedicated VPC instead, replace fromLookup with:
    //   new ec2.Vpc(this, "NovaActVpc", { maxAzs: 2, natGateways: 0 })
    const vpc = ec2.Vpc.fromLookup(this, "DefaultVpc", { isDefault: true });

    const dispatchTopic = new sns.Topic(this, "DispatchAlertsTopic", {
      topicName:   "novaguard-dispatch-alerts",
      displayName: "NovaGuard Emergency Dispatcher Alerts",
    });

    // Optional: add a demo email subscription if ALERT_EMAIL env var is set
    const alertEmail = process.env.NOVAGUARD_ALERT_EMAIL;
    if (alertEmail) {
      dispatchTopic.addSubscription(new snsSubscriptions.EmailSubscription(alertEmail));
    }

    // ================================================================
    // 3b-i. Amazon ECR — nova-act-worker Docker image repository
    // ================================================================
    // Build + push the image with:
    //   cd novaguard/lambdas/nova_act_worker
    //   aws ecr get-login-password --region us-east-1 | docker login --username AWS \
    //       --password-stdin <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com
    //   docker build -t nova-act-worker .
    //   docker tag nova-act-worker:latest <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/novaguard-nova-act-worker:latest
    //   docker push <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/novaguard-nova-act-worker:latest
    const novaActRepo = new ecr.Repository(this, "NovaActRepo", {
      repositoryName: "novaguard-nova-act-worker",
      removalPolicy:  cdk.RemovalPolicy.DESTROY,
      emptyOnDelete:  true,
      imageScanOnPush: true,
    });

    // ================================================================
    // 3b-ii. Amazon ECS Fargate — Nova Act browser automation cluster
    // ================================================================
    const novaActCluster = new ecs.Cluster(this, "NovaActCluster", {
      clusterName:       "novaguard-nova-act",
      vpc,
      containerInsights: true,
    });

    // Task execution role (ECR pull + CloudWatch logs)
    const novaActExecRole = new iam.Role(this, "NovaActExecRole", {
      roleName:  "novaguard-nova-act-exec-role",
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AmazonECSTaskExecutionRolePolicy"
        ),
      ],
    });

    // Task role (what the running container can call)
    const novaActTaskRole = new iam.Role(this, "NovaActTaskRole", {
      roleName:  "novaguard-nova-act-task-role",
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
    });
    novaActTaskRole.addToPolicy(new iam.PolicyStatement({
      effect:  iam.Effect.ALLOW,
      actions: ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
      resources: ["*"],   // scoped to novaguard table at runtime via env var
    }));
    novaActTaskRole.addToPolicy(new iam.PolicyStatement({
      effect:  iam.Effect.ALLOW,
      actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
      resources: ["*"],
    }));
    // Nova Act SDK needs Bedrock access to call the Nova Act model
    novaActTaskRole.addToPolicy(new iam.PolicyStatement({
      effect:  iam.Effect.ALLOW,
      actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      resources: ["arn:aws:bedrock:*::foundation-model/*",
                  "arn:aws:bedrock:*:*:inference-profile/*"],
    }));

    // CloudWatch log group for the ECS task
    const novaActLogGroup = new logs.LogGroup(this, "NovaActWorkerLogs", {
      logGroupName:  "/ecs/novaguard-nova-act-worker",
      retention:     logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Fargate task definition
    // IMPORTANT: Before the real nova-act-worker image is pushed to ECR,
    // this uses a harmless placeholder. Once pushed, update the image URI
    // and redeploy. The /nova-act endpoint gracefully handles missing ECS task.
    const novaActTaskDef = new ecs.FargateTaskDefinition(this, "NovaActTaskDef", {
      family:         "novaguard-nova-act-worker",
      memoryLimitMiB: 4096,    // Playwright/Chromium needs 2-4 GB RAM
      cpu:            2048,    // 2 vCPU for fast browser rendering
      executionRole:  novaActExecRole,
      taskRole:       novaActTaskRole,
    });

    // Real Nova Act worker image from ECR
    novaActTaskDef.addContainer("nova-act-worker", {
      image:   ecs.ContainerImage.fromEcrRepository(novaActRepo, "latest"),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix:   "nova-act",
        logGroup:        novaActLogGroup,
      }),
      environment: {
        REGION:            this.region,
        EMERGENCIES_TABLE: "novaguard-emergencies",
      },
      essential: true,
    });

    // Security group for the ECS task (outbound only — needs internet for Nova Act)
    const novaActSg = new ec2.SecurityGroup(this, "NovaActSg", {
      vpc,
      securityGroupName: "novaguard-nova-act-sg",
      description:       "NovaGuard Nova Act ECS task - outbound to Nova Act API + CloudFront",
      allowAllOutbound:  true,
    });

    // Now that novaActExecRole + novaActTaskRole are declared, grant Lambda PassRole
    role.addToPolicy(new iam.PolicyStatement({
      sid:     "IamPassNovaActRoles",
      effect:  iam.Effect.ALLOW,
      actions: ["iam:PassRole"],
      resources: [novaActExecRole.roleArn, novaActTaskRole.roleArn],
    }));

    // ================================================================
    // 3b. Amazon Location Service — Route Calculator
    // ================================================================
    const routeCalculator = new location.CfnRouteCalculator(this, "RouteCalculator", {
      calculatorName: "novaguard-route-calculator",
      dataSource:     "Esri",
      description:    "NovaGuard emergency unit route ETA calculator",
      pricingPlan:    "RequestBasedUsage",
    });

    // ================================================================
    // 3. Helper — create Lambda + log group with consistent defaults
    // ================================================================
    const commonEnv = {
      REGION:                    this.region,
      NOVA_LITE_MODEL_ID:        "us.amazon.nova-lite-v1:0",
      NOVA_SONIC_MODEL_ID:       "amazon.nova-sonic-v1:0",
      EMERGENCIES_TABLE:         table.tableName,
      DISPATCHER_SNS_TOPIC:      dispatchTopic.topicArn,
      LOCATION_ROUTE_CALCULATOR: routeCalculator.calculatorName,
      POWERTOOLS_LOG_LEVEL:      "INFO",
    };

    // Smithy SDK layer — aws_sdk_bedrock_runtime (the ONLY Python SDK that exposes
    // invoke_model_with_bidirectional_stream for Nova Sonic HTTP/2 bidirectional streaming)
    // Packages: aws_sdk_bedrock_runtime, smithy_aws_core, smithy_core, smithy_http,
    //           smithy_json, smithy_aws_event_stream, aws_sdk_signers, awscrt, ijson
    const sonicSmithyLayer = new lambda.LayerVersion(this, "SonicSmithyLayer", {
      layerVersionName: "novaguard-sonic-smithy-sdk",
      description:      "aws_sdk_bedrock_runtime Smithy SDK — enables Nova Sonic HTTP/2 bidirectional streaming in Python Lambda",
      code:             lambda.Code.fromAsset(path.join(__dirname, "lambda", "sonic_layer")),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
    });

    // Strands Agents SDK layer — strands-agents 1.23.0 + deps
    // Pre-built into lambda/strands_layer/python/ so the @tool decorator
    // resolves at import time → STRANDS_AVAILABLE=True → agentic loop runs
    const strandsLayer = new lambda.LayerVersion(this, "StrandsAgentsLayer", {
      layerVersionName: "novaguard-strands-agents-sdk",
      description:      "strands-agents 1.23.0 — multi-tool agentic loop SDK for StrandsTriageAgent",
      code:             lambda.Code.fromAsset(path.join(__dirname, "lambda", "strands_layer")),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
    });

    const makeLambda = (
      id: string,
      functionName: string,
      handlerFile: string,
      description: string,
      extraEnv: Record<string, string> = {},
      timeoutSec = 60,
      memorySizeMb = 512,
      layers: lambda.ILayerVersion[] = [],
    ): lambda.Function => {
      const lg = new logs.LogGroup(this, `${id}Logs`, {
        logGroupName:  `/aws/lambda/${functionName}`,
        retention:     logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      return new lambda.Function(this, id, {
        functionName,
        runtime:     lambda.Runtime.PYTHON_3_12,
        handler:     `${handlerFile}.handler`,
        code:        lambda.Code.fromAsset(path.join(__dirname, "lambda"), {
          exclude: ["boto3_layer/**", "boto3_layer", "sonic_layer/**", "sonic_layer", "strands_layer/**", "strands_layer"],
        }),
        role,
        logGroup:    lg,
        timeout:     cdk.Duration.seconds(timeoutSec),
        memorySize:  memorySizeMb,
        tracing:     lambda.Tracing.ACTIVE,
        environment: { ...commonEnv, ...extraEnv },
        description,
        layers,
      });
    };

    // ================================================================
    // 4. Lambda functions
    // ================================================================

    // Agent 1 — Triage (Nova 2 Lite)
    const triageLambda = makeLambda(
      "TriageAgent", "novaguard-triage-agent", "handler",
      "NovaGuard Agent 1: Nova 2 Lite emergency triage + severity scoring",
    );

    // Nova Sonic — voice transcription (uses Smithy SDK aws_sdk_bedrock_runtime layer
    // because boto3 does NOT expose invoke_model_with_bidirectional_stream for Nova Sonic)
    const sonicLambda = makeLambda(
      "SonicAgent", "novaguard-sonic-transcription", "sonic_handler",
      "NovaGuard Voice: Nova Sonic bidirectional streaming (Smithy SDK) — audio → transcript",
      {}, 90, 1024, [sonicSmithyLayer],
    );

    // Agent 2 — Dispatch (Nova 2 Lite)
    const dispatchLambda = makeLambda(
      "DispatchAgent", "novaguard-dispatch-agent", "dispatch_handler",
      "NovaGuard Agent 2: Nova 2 Lite emergency unit dispatch decision",
    );

    // Agent 3 — Communications (Nova 2 Lite)
    const commsLambda = makeLambda(
      "CommsAgent", "novaguard-comms-agent", "comms_handler",
      "NovaGuard Agent 3: Nova 2 Lite caller instructions + responder briefing",
    );

    // Streaming Triage — SSE token-by-token output (typewriter proof of live LLM inference)
    const streamLambda = makeLambda(
      "StreamTriageAgent", "novaguard-triage-stream", "triage_stream_handler",
      "NovaGuard Stream: Nova 2 Lite converse_stream → SSE typewriter output for live demo",
      {}, 60, 512,
    );

    // Visual Intake — Nova Multimodal Vision (image/photo → structured emergency report)
    const intakeLambda = makeLambda(
      "IntakeAgent", "novaguard-intake-vision", "intake_handler",
      "NovaGuard Intake: Nova Lite multimodal vision — emergency scene photo → structured 911 report",
      {}, 60, 512,
    );

    // Semantic Embedding — Titan Embed Image v1 + cosine similarity (similar past incidents)
    const embeddingLambda = makeLambda(
      "EmbeddingAgent", "novaguard-embedding-agent", "embedding_handler",
      "NovaGuard Embedding: Titan embed-image-v1 — cosine similarity over past incident vectors",
      {}, 60, 512,
    );

    // Strands Triage — strands-agents multi-tool agentic loop with StrandsAgentsLayer
    // StrandsLayer makes STRANDS_AVAILABLE=True → real Agent() agentic loop runs
    const strandsTriage = makeLambda(
      "StrandsTriageAgent", "novaguard-strands-triage", "strands_triage_handler",
      "NovaGuard Strands: strands-agents 1.23.0 Agent() with 4 @tool functions — real agentic loop",
      {}, 120, 512, [strandsLayer],
    );

    // ================================================================
    // 4a. CentralSquare CAD System — separate S3 + CloudFront
    // Nova Act navigates to THIS URL (different domain from the demo UI
    // so judges can see Nova Act crossing system boundaries)
    // ================================================================
    const cadBucket = new s3.Bucket(this, "CadSystemBucket", {
      bucketName:           `novaguard-cad-system-${this.account}`,
      websiteIndexDocument: "index.html",
      publicReadAccess:     true,
      blockPublicAccess:    s3.BlockPublicAccess.BLOCK_ACLS,
      removalPolicy:        cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects:    true,
    });

    const cadCdn = new cloudfront.Distribution(this, "CadSystemCdn", {
      defaultBehavior: {
        origin:              new origins.S3StaticWebsiteOrigin(cadBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy:          cloudfront.CachePolicy.CACHING_DISABLED,
      },
      defaultRootObject: "index.html",
      comment:           "Riverside County CentralSquare CAD — Nova Act automation target",
    });

    new s3deploy.BucketDeployment(this, "DeployCadSystem", {
      sources:            [s3deploy.Source.asset(path.join(__dirname, "..", "cad_system"))],
      destinationBucket:  cadBucket,
      distribution:       cadCdn,
      distributionPaths:  ["/*"],
    });

    const cadSystemUrl = `https://${cadCdn.distributionDomainName}/index.html`;

    // Nova Act Cloud — API endpoint that queues ECS Fargate Nova Act jobs
    // ECS cluster ARN + task def ARN + subnet IDs passed as env vars so
    // the Lambda can call ECS:RunTask without hard-coding resource IDs.
    // Fn.join resolves subnet IDs at CloudFormation deploy time (not synth time).
    const vpcSubnets   = cdk.Fn.join(",", vpc.publicSubnets.map((s) => s.subnetId));
    const novaActCloud = makeLambda(
      "NovaActCloudAgent", "novaguard-nova-act-cloud", "nova_act_cloud_handler",
      "NovaGuard Nova Act: cloud trigger Lambda → ECS Fargate task → Playwright/Chromium CAD entry",
      {
        ECS_CLUSTER_ARN:       novaActCluster.clusterArn,
        ECS_TASK_DEF_ARN:      novaActTaskDef.taskDefinitionArn,
        ECS_SUBNET_IDS:        vpcSubnets,
        ECS_SG_ID:             novaActSg.securityGroupId,
        CAD_FORM_URL:          cadSystemUrl,   // separate CloudFront (different domain from demo UI)
        AGENTCORE_RUNTIME_ARN: "",
      },
      60,  // 1-min timeout — trigger is async; ECS task runs independently
    );

    // A2A Orchestrator — Google A2A protocol v0.2.2
    // GET  /a2a           → Orchestrator Agent Card (spec-compliant)
    // POST /a2a           → JSON-RPC 2.0 tasks/send router → specialist Lambdas
    // GET  /a2a/agents    → all agent cards
    // GET  /a2a/agents/*  → individual agent cards
    // POST /a2a/agents/*  → direct agent call (non-RPC)
    const a2aLambda = makeLambda(
      "A2AOrchestrator", "novaguard-a2a-orchestrator", "a2a_handler",
      "NovaGuard A2A: Google A2A protocol v0.2.2 — agent cards + tasks/send JSON-RPC 2.0 routing",
      {},   // API_BASE_URL defaults to the known prod URL in the handler
      90, 512,
    );

    // Pipeline Orchestrator — calls all 3 agents in sequence (direct Lambda chain)
    const pipelineLambda = makeLambda(
      "PipelineOrchestrator", "novaguard-pipeline", "pipeline_handler",
      "NovaGuard Full Pipeline: Triage → Dispatch → Communications (3 real Nova 2 Lite calls)",
      {
        TRIAGE_FUNCTION_NAME:   "novaguard-triage-agent",
        DISPATCH_FUNCTION_NAME: "novaguard-dispatch-agent",
        COMMS_FUNCTION_NAME:    "novaguard-comms-agent",
      },
      180,  // 3 consecutive Nova calls — allow 3 minutes
    );

    // ================================================================
    // 5. Step Functions state machine (parallel to pipeline Lambda)
    // ================================================================
    const triageTask = new tasks.LambdaInvoke(this, "SfnTriageTask", {
      lambdaFunction: triageLambda,
      outputPath: "$.Payload",
      comment: "Nova 2 Lite: severity scoring and emergency classification",
    });

    const dispatchTask = new tasks.LambdaInvoke(this, "SfnDispatchTask", {
      lambdaFunction: dispatchLambda,
      outputPath: "$.Payload",
      comment: "Nova 2 Lite: unit dispatch decision",
    });

    const commsTask = new tasks.LambdaInvoke(this, "SfnCommsTask", {
      lambdaFunction: commsLambda,
      outputPath: "$.Payload",
      comment: "Nova 2 Lite: caller instructions + responder briefing",
    });

    const sfnLogGroup = new logs.LogGroup(this, "SfnLogGroup", {
      logGroupName:  "/aws/states/novaguard-pipeline",
      retention:     logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const stateMachine = new sfn.StateMachine(this, "NovaGuardPipeline", {
      stateMachineName: "novaguard-emergency-pipeline",
      definitionBody:   sfn.DefinitionBody.fromChainable(
        triageTask.next(dispatchTask).next(commsTask)
      ),
      timeout:          cdk.Duration.minutes(5),
      tracingEnabled:   true,
      logs: {
        destination: sfnLogGroup,
        level: sfn.LogLevel.ALL,
      },
    });

    triageLambda.grantInvoke(stateMachine.role);
    dispatchLambda.grantInvoke(stateMachine.role);
    commsLambda.grantInvoke(stateMachine.role);

    // Step Functions Pipeline — starts SFN state machine + polls for result
    // Uses the shared role (already has states:StartExecution + DescribeExecution).
    const sfnPipelineLambda = makeLambda(
      "SfnPipelineOrchestrator", "novaguard-sfn-pipeline", "sfn_pipeline_handler",
      "NovaGuard SFN Pipeline: starts novaguard-emergency-pipeline StepFn + returns execution_arn",
      { STATE_MACHINE_ARN: stateMachine.stateMachineArn },
      120,
    );

    // Proof Dashboard — judges can verify all 4 Nova models + AWS services are live
    const proofLambda = makeLambda(
      "ProofDashboard", "novaguard-proof-dashboard", "proof_handler",
      "NovaGuard Proof: live verification dashboard — all 4 Nova models + AWS services",
      { STATE_MACHINE_ARN: stateMachine.stateMachineArn },
      120, 512,
    );

    // ================================================================
    // 6. API Gateway
    // ================================================================
    const api = new apigateway.RestApi(this, "NovaGuardApi", {
      restApiName: "novaguard-api",
      description: "NovaGuard Emergency AI — 3-agent pipeline (Triage + Dispatch + Comms)",
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: ["GET", "POST", "OPTIONS"],
        allowHeaders: ["Content-Type", "Authorization"],
      },
      deployOptions: {
        stageName: "prod",
        loggingLevel: apigateway.MethodLoggingLevel.INFO,
        dataTraceEnabled: true,
        metricsEnabled: true,
      },
    });

    const integ = (fn: lambda.Function) =>
      new apigateway.LambdaIntegration(fn, {
        requestTemplates: { "application/json": '{ "statusCode": "200" }' },
      });

    const addRoute = (
      name: string,
      fn: lambda.Function,
      methods: string[] = ["GET", "POST"],
    ) => {
      const r = api.root.addResource(name);
      for (const m of methods) {
        r.addMethod(m, integ(fn), {
          methodResponses: [{ statusCode: "200" },
                           { statusCode: "400" },
                           { statusCode: "502" }],
        });
      }
      return r;
    };

    addRoute("triage",         triageLambda);    // Agent 1 standalone (+ Amazon Translate)
    addRoute("triage-stream",   streamLambda);    // SSE streaming — typewriter live-LLM proof
    addRoute("intake",          intakeLambda);    // Nova Multimodal Vision — photo → emergency report
    addRoute("transcribe",     sonicLambda);     // Nova Sonic voice STT
    addRoute("dispatch",       dispatchLambda);  // Agent 2 + Location Service ETA
    addRoute("embed",          embeddingLambda); // Titan Embedding + cosine similarity
    addRoute("strands-triage", strandsTriage);   // Strands Agents multi-tool triage
    addRoute("pipeline",       pipelineLambda);  // Full 3-agent demo endpoint (direct Lambda chain)
    addRoute("sfn-pipeline",   sfnPipelineLambda); // Step Functions orchestrated pipeline + execution ARN
    addRoute("proof",         proofLambda, ["GET"]);    // Judge verification — all 4 Nova models live proof
    const novaActRoute = addRoute("nova-act", novaActCloud); // Nova Act CAD browser automation (ECS Fargate)

    // GET/POST /nova-act/{proxy+} — allows /nova-act/latest and /nova-act/status
    const novaActProxy = novaActRoute.addResource("{proxy+}");
    for (const m of ["GET", "POST"]) {
      novaActProxy.addMethod(m, integ(novaActCloud), {
        methodResponses: [{ statusCode: "200" }, { statusCode: "400" }],
      });
    }

    // A2A endpoints: GET /a2a (agent card), POST /a2a (tasks/send),
    //                GET+POST /a2a/{proxy+} (agents list, agent cards, direct calls)
    const a2aRoute = addRoute("a2a", a2aLambda, ["GET", "POST"]);
    const a2aProxy = a2aRoute.addResource("{proxy+}");
    for (const m of ["GET", "POST"]) {
      a2aProxy.addMethod(m, integ(a2aLambda), {
        methodResponses: [
          { statusCode: "200" },
          { statusCode: "400" },
          { statusCode: "404" },
        ],
      });
    }

    // GET /health — mock
    api.root.addResource("health").addMethod("GET",
      new apigateway.MockIntegration({
        integrationResponses: [{
          statusCode: "200",
          responseTemplates: {
            "application/json": JSON.stringify({
              status: "ok",
              agents: 4,
              nova_models: [
                "us.amazon.nova-lite-v1:0  (triage, dispatch, comms, vision-intake, streaming)",
                "amazon.nova-sonic-v1:0    (voice transcription — bidirectional stream)",
                "amazon.nova-act           (CAD automation — ECS Fargate + Playwright)",
              ],
              endpoints: ["/intake", "/triage", "/triage-stream", "/transcribe", "/dispatch", "/pipeline"],
              statemachine: "novaguard-emergency-pipeline",
              nova_multimodal: "POST /intake — image → scene analysis → structured 911 report",
            }),
          },
        }],
        requestTemplates: { "application/json": '{ "statusCode": 200 }' },
      }), { methodResponses: [{ statusCode: "200" }] },
    );

    this.apiUrl = api.url;

    // ================================================================
    // 7. Static Demo UI — S3 + CloudFront
    // ================================================================
    const siteBucket = new s3.Bucket(this, "DemoBucket", {
      bucketName: `novaguard-demo-ui-${this.account}`,
      websiteIndexDocument: "index.html",
      publicReadAccess: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ACLS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const cdn = new cloudfront.Distribution(this, "DemoCdn", {
      defaultBehavior: {
        origin: new origins.S3StaticWebsiteOrigin(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,  // always fresh for demo
      },
      defaultRootObject: "index.html",
      comment: "NovaGuard Hackathon Demo UI",
    });

    new s3deploy.BucketDeployment(this, "DeployDemoUi", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "..", "demo"))],
      destinationBucket: siteBucket,
      distribution: cdn,
      distributionPaths: ["/*"],
    });

    // ================================================================
    // 8. CloudWatch Operations Dashboard
    // ================================================================
    const mkMetric = (fn: string, metricName: string, stat = "Sum") =>
      new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName,
        dimensionsMap: { FunctionName: fn },
        statistic: stat,
        period: cdk.Duration.minutes(5),
      });

    const dashboard = new cloudwatch.Dashboard(this, "NovaGuardDashboard", {
      dashboardName: "NovaGuard-Operations",
    });

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Lambda Invocations — All Nova Agents",
        width: 12,
        left: [
          mkMetric("novaguard-triage-agent",    "Invocations").with({ label: "Triage (Nova 2 Lite)" }),
          mkMetric("novaguard-dispatch-agent",  "Invocations").with({ label: "Dispatch (Nova 2 Lite)" }),
          mkMetric("novaguard-comms-agent",     "Invocations").with({ label: "Comms (Nova 2 Lite)" }),
          mkMetric("novaguard-pipeline",        "Invocations").with({ label: "Pipeline Orchestrator" }),
          mkMetric("novaguard-embedding-agent", "Invocations").with({ label: "Embeddings (Titan)" }),
          mkMetric("novaguard-strands-triage",  "Invocations").with({ label: "Strands Agents" }),
        ],
      }),
      new cloudwatch.GraphWidget({
        title: "Lambda Duration P99 — Response Time",
        width: 12,
        left: [
          mkMetric("novaguard-triage-agent",    "Duration", "p99").with({ label: "Triage p99" }),
          mkMetric("novaguard-dispatch-agent",  "Duration", "p99").with({ label: "Dispatch p99" }),
          mkMetric("novaguard-comms-agent",     "Duration", "p99").with({ label: "Comms p99" }),
          mkMetric("novaguard-pipeline",        "Duration", "p99").with({ label: "Pipeline p99" }),
        ],
      }),
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Lambda Errors — All Agents",
        width: 12,
        left: [
          mkMetric("novaguard-triage-agent",    "Errors").with({ label: "Triage Errors" }),
          mkMetric("novaguard-dispatch-agent",  "Errors").with({ label: "Dispatch Errors" }),
          mkMetric("novaguard-comms-agent",     "Errors").with({ label: "Comms Errors" }),
          mkMetric("novaguard-pipeline",        "Errors").with({ label: "Pipeline Errors" }),
        ],
      }),
      new cloudwatch.GraphWidget({
        title: "Step Functions Executions — Emergency Pipeline",
        width: 12,
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/States",
            metricName: "ExecutionsStarted",
            dimensionsMap: { StateMachineArn: stateMachine.stateMachineArn },
            statistic: "Sum",
            period: cdk.Duration.minutes(5),
            label: "Executions Started",
          }),
          new cloudwatch.Metric({
            namespace: "AWS/States",
            metricName: "ExecutionsFailed",
            dimensionsMap: { StateMachineArn: stateMachine.stateMachineArn },
            statistic: "Sum",
            period: cdk.Duration.minutes(5),
            label: "Executions Failed",
          }),
        ],
      }),
    );

    // CloudWatch Alarms — judges can see these
    new cloudwatch.Alarm(this, "PipelineErrorAlarm", {
      alarmName: "novaguard-pipeline-errors",
      alarmDescription: "NovaGuard pipeline Lambda errors > 1 in 5 min",
      metric: mkMetric("novaguard-pipeline", "Errors"),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ================================================================
    // 9. Outputs
    // ================================================================
    const out = (id: string, value: string, description: string) =>
      new cdk.CfnOutput(this, id, { value, description });

    out("PipelineApiUrl",     `${api.url}pipeline`,        "POST: full 3-agent pipeline — THE JUDGE DEMO ENDPOINT");
    out("StreamTriageApiUrl", `${api.url}triage-stream`,   "POST: SSE streaming triage — token-by-token typewriter proof");
    out("IntakeApiUrl",       `${api.url}intake`,           "POST: Nova Multimodal Vision — image → structured emergency report");
    out("TriageApiUrl",       `${api.url}triage`,           "POST: Agent 1 — triage + Amazon Translate");
    out("TranscribeApiUrl",   `${api.url}transcribe`,       "POST: Nova Sonic voice transcription");
    out("DispatchApiUrl",     `${api.url}dispatch`,         "POST: Agent 2 — dispatch + Location Service ETA");
    out("EmbedApiUrl",        `${api.url}embed`,            "POST: Titan Embedding + cosine similarity past incidents");
    out("StrandsTriageUrl",   `${api.url}strands-triage`,  "POST: Strands Agents multi-tool triage");
    out("DispatchTopicArn",   dispatchTopic.topicArn,       "SNS topic for dispatcher alerts");
    out("RouteCalculatorName", routeCalculator.calculatorName, "Amazon Location Service route calculator");
    out("DemoUiUrl",          `https://${cdn.distributionDomainName}`, "LIVE DEMO UI — CloudFront URL (judges click here)");
    out("CloudWatchDashboard", `https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=NovaGuard-Operations`, "CloudWatch Operations Dashboard");
    out("HealthCheckUrl",     `${api.url}health`,        "GET: liveness");
    out("DynamoTableName",      table.tableName,              "DynamoDB emergencies table");
    out("StateMachineArn",      stateMachine.stateMachineArn, "Step Functions state machine ARN");
    out("NovaActClusterArn",    novaActCluster.clusterArn,    "ECS cluster for Nova Act Fargate tasks");
    out("NovaActTaskDefArn",    novaActTaskDef.taskDefinitionArn, "ECS task definition for nova-act-worker");
    out("NovaActEcrRepo",       novaActRepo.repositoryUri,    "ECR repository for nova-act-worker Docker image");
    out("NovaActApiUrl",        `${api.url}nova-act`,          "POST: trigger Nova Act CAD entry | GET: latest result");
    out("CadSystemUrl",         cadSystemUrl,                  "CentralSquare CAD (separate CloudFront) — Nova Act target URL");
    out("A2AAgentCardUrl",      `${api.url}a2a`,               "GET: NovaGuard Orchestrator Agent Card (Google A2A spec v0.2.2)");
    out("A2AAgentsUrl",         `${api.url}a2a/agents`,        "GET: all A2A agent cards (triage/dispatch/comms/strands/nova-act)");
    out("A2ATasksEndpoint",     `${api.url}a2a`,               "POST: JSON-RPC 2.0 tasks/send — routes to specialist Nova agents");
    out("DockerBuildCommands", [
      "# Build + push nova-act-worker to ECR (requires Docker Desktop):",
      `aws ecr get-login-password --region ${this.region} | docker login --username AWS --password-stdin ${this.account}.dkr.ecr.${this.region}.amazonaws.com`,
      `docker build -t nova-act-worker c:\\Users\\prajw\\OneDrive\\Desktop\\amazon\ nova\\novaguard\\lambdas\\nova_act_worker`,
      `docker tag nova-act-worker:latest ${this.account}.dkr.ecr.${this.region}.amazonaws.com/novaguard-nova-act-worker:latest`,
      `docker push ${this.account}.dkr.ecr.${this.region}.amazonaws.com/novaguard-nova-act-worker:latest`,
    ].join(" && "),
    "Commands to build + push nova-act-worker Docker image to ECR");
    out("DeploymentSummary", [
      "NovaGuard 3-Agent Pipeline LIVE",
      `Pipeline: ${api.url}pipeline`,
      `Transcribe: ${api.url}transcribe`,
      "3 real Nova 2 Lite calls per pipeline run + Nova Sonic voice STT",
      "CloudWatch proof: [NOVAGUARD TRIAGE CALL] [NOVAGUARD DISPATCH CALL] [NOVAGUARD COMMS CALL]",
      "Nova Act: [NOVAGUARD NOVA_ACT TRIGGERED] → ECS Fargate → [NOVA_ACT COMPLETE]",
    ].join(" | "), "Quick reference");
  }
}
