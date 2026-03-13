/**
 * NovaGuard Core Stack
 *
 * Owns all security primitives:
 * - KMS Customer Managed Key (CMK) for data-at-rest encryption
 * - IAM execution roles for each Lambda agent (least-privilege separation)
 * - Bedrock Guardrails configuration
 * - SSM Parameter Store entries for cross-stack references
 *
 * Every resource in this stack is a prerequisite for all other stacks.
 */

import * as cdk from "aws-cdk-lib";
import * as kms from "aws-cdk-lib/aws-kms";
import * as iam from "aws-cdk-lib/aws-iam";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";
import { NOVAGUARD_CONFIG } from "../config/constants";

export interface CoreStackProps extends cdk.StackProps {
  // No inbound dependencies — this is the root stack
}

export interface LambdaExecutionRoles {
  intakeAgent: iam.Role;
  triageAgent: iam.Role;
  dispatchAgent: iam.Role;
  commsAgent: iam.Role;
  webSocketConnect: iam.Role;
  webSocketDisconnect: iam.Role;
  webSocketMessage: iam.Role;
  callerApi: iam.Role;
  stepFunctionsExecution: iam.Role;
  eventBridgePipe: iam.Role;
}

export class CoreStack extends cdk.Stack {
  public readonly dataEncryptionKey: kms.Key;
  public readonly lambdaExecutionRoles: LambdaExecutionRoles;

  constructor(scope: Construct, id: string, props: CoreStackProps) {
    super(scope, id, props);

    // -----------------------------------------------------------------
    // KMS Customer Managed Key
    // Used for: DynamoDB, S3, Kinesis, SQS, SNS, CloudWatch Logs
    // Rotation: annual (automated)
    // -----------------------------------------------------------------
    this.dataEncryptionKey = new kms.Key(this, "NovaGuardCMK", {
      alias: "alias/novaguard-data",
      description: "NovaGuard primary CMK for data-at-rest encryption across all services. HIPAA-eligible.",
      enableKeyRotation: true,
      rotationPeriod: cdk.Duration.days(365),
      keySpec: kms.KeySpec.SYMMETRIC_DEFAULT,
      keyUsage: kms.KeyUsage.ENCRYPT_DECRYPT,
      removalPolicy: cdk.RemovalPolicy.RETAIN, // Keys must be retained; data becomes unrecoverable
    });

    // Allow CloudWatch Logs to use this key (required for encrypted log groups)
    this.dataEncryptionKey.addToResourcePolicy(new iam.PolicyStatement({
      principals: [new iam.ServicePrincipal(`logs.${this.region}.amazonaws.com`)],
      actions: ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
      conditions: {
        ArnLike: { "kms:EncryptionContext:aws:logs:arn": `arn:aws:logs:${this.region}:${this.account}:*` },
      },
    }));

    // -----------------------------------------------------------------
    // Bedrock execution policy — shared across all agent roles
    // Scoped to specific Nova model ARNs to enforce model governance
    // -----------------------------------------------------------------
    const bedrockPolicy = new iam.ManagedPolicy(this, "BedrockInvokePolicy", {
      managedPolicyName: "novaguard-bedrock-invoke",
      description: "Least-privilege Bedrock access for NovaGuard agents",
      statements: [
        new iam.PolicyStatement({
          sid: "InvokeNovaModels",
          effect: iam.Effect.ALLOW,
          actions: [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
          ],
          resources: [
            `arn:aws:bedrock:${this.region}::foundation-model/amazon.nova-lite-v1:0`,
            `arn:aws:bedrock:${this.region}::foundation-model/amazon.nova-pro-v1:0`,
            `arn:aws:bedrock:${this.region}::foundation-model/amazon.nova-sonic-v1:0`,
            // Nova Multimodal Embeddings — correct model ID confirmed 2026-03
            `arn:aws:bedrock:${this.region}::foundation-model/amazon.nova-2-multimodal-embeddings-v1:0`,
            // Cross-region inference profile ARNs
            `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "ApplyGuardrails",
          effect: iam.Effect.ALLOW,
          actions: ["bedrock:ApplyGuardrail"],
          resources: [`arn:aws:bedrock:${this.region}:${this.account}:guardrail/*`],
        }),
        new iam.PolicyStatement({
          sid: "XRayBedrockTracing",
          effect: iam.Effect.ALLOW,
          actions: [
            "xray:PutTraceSegments",
            "xray:PutTelemetryRecords",
          ],
          resources: ["*"],
        }),
      ],
    });

    // -----------------------------------------------------------------
    // Intake Agent Role
    // Permissions: Bedrock (Nova Lite + MM Embedding), DynamoDB (emergencies write),
    //              S3 (media read), Kinesis (events put), SQS (A2A send),
    //              Translate (language detection), CloudWatch
    // -----------------------------------------------------------------
    this.lambdaExecutionRoles = {
      intakeAgent: this.createAgentRole("IntakeAgent", bedrockPolicy, [
        new iam.PolicyStatement({
          sid: "DynamoDBEmergencyWrite",
          actions: ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"],
          resources: [
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.EMERGENCIES}`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.CALLER_PROFILES}`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "S3MediaRead",
          actions: ["s3:GetObject", "s3:GetObjectVersion"],
          resources: [`arn:aws:s3:::${NOVAGUARD_CONFIG.BUCKETS.MEDIA}-${this.account}/*`],
        }),
        new iam.PolicyStatement({
          sid: "KinesisEmergencyPut",
          actions: ["kinesis:PutRecord", "kinesis:PutRecords"],
          resources: [`arn:aws:kinesis:${this.region}:${this.account}:stream/${NOVAGUARD_CONFIG.KINESIS.EMERGENCY_STREAM}`],
        }),
        new iam.PolicyStatement({
          sid: "SQSAgentMessage",
          actions: ["sqs:SendMessage", "sqs:GetQueueUrl"],
          resources: [`arn:aws:sqs:${this.region}:${this.account}:novaguard-agent-messages`],
        }),
        new iam.PolicyStatement({
          sid: "TranslateDetect",
          actions: ["translate:DetectDominantLanguage", "translate:TranslateText"],
          resources: ["*"],
        }),
      ]),

      // -----------------------------------------------------------------
      // Triage Agent Role
      // Additional: OpenSearch (vector search), S3 protocols read
      // -----------------------------------------------------------------
      triageAgent: this.createAgentRole("TriageAgent", bedrockPolicy, [
        new iam.PolicyStatement({
          sid: "DynamoDBEmergencyUpdate",
          actions: ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query"],
          resources: [
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.EMERGENCIES}`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.EMERGENCIES}/index/*`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "OpenSearchVectorQuery",
          actions: ["aoss:APIAccessAll"],
          resources: [`arn:aws:aoss:${this.region}:${this.account}:collection/*`],
        }),
        new iam.PolicyStatement({
          sid: "S3ProtocolsRead",
          actions: ["s3:GetObject", "s3:ListBucket"],
          resources: [
            `arn:aws:s3:::${NOVAGUARD_CONFIG.BUCKETS.PROTOCOLS}-${this.account}`,
            `arn:aws:s3:::${NOVAGUARD_CONFIG.BUCKETS.PROTOCOLS}-${this.account}/*`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "SQSAgentMessage",
          actions: ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueUrl"],
          resources: [`arn:aws:sqs:${this.region}:${this.account}:novaguard-agent-messages`],
        }),
      ]),

      // -----------------------------------------------------------------
      // Dispatch Agent Role
      // Additional: Location Service, SSM (read-only for resource configs)
      // -----------------------------------------------------------------
      dispatchAgent: this.createAgentRole("DispatchAgent", bedrockPolicy, [
        new iam.PolicyStatement({
          sid: "DynamoDBResourcesRead",
          actions: ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:UpdateItem"],
          resources: [
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.EMERGENCIES}`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.RESOURCES}`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.RESOURCES}/index/*`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "LocationServiceQuery",
          actions: [
            "geo:SearchPlaceIndexForText",
            "geo:SearchPlaceIndexForPosition",
            "geo:CalculateRoute",
            "geo:GetDevicePosition",
            "geo:BatchGetDevicePosition",
          ],
          resources: [
            `arn:aws:geo:${this.region}:${this.account}:route-calculator/${NOVAGUARD_CONFIG.LOCATION_SERVICE.ROUTE_CALCULATOR}`,
            `arn:aws:geo:${this.region}:${this.account}:place-index/${NOVAGUARD_CONFIG.LOCATION_SERVICE.PLACE_INDEX}`,
            `arn:aws:geo:${this.region}:${this.account}:tracker/${NOVAGUARD_CONFIG.LOCATION_SERVICE.TRACKER}`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "SQSAgentMessage",
          actions: ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueUrl"],
          resources: [`arn:aws:sqs:${this.region}:${this.account}:novaguard-agent-messages`],
        }),
        new iam.PolicyStatement({
          sid: "SNSDispatchAlert",
          actions: ["sns:Publish"],
          resources: [
            `arn:aws:sns:${this.region}:${this.account}:${NOVAGUARD_CONFIG.SNS.DISPATCHER_ALERTS}`,
            `arn:aws:sns:${this.region}:${this.account}:${NOVAGUARD_CONFIG.SNS.ESCALATION}`,
          ],
        }),
      ]),

      // -----------------------------------------------------------------
      // Communications Agent Role
      // Additional: API Gateway Management (WebSocket push), Translate
      // -----------------------------------------------------------------
      commsAgent: this.createAgentRole("CommsAgent", bedrockPolicy, [
        new iam.PolicyStatement({
          sid: "DynamoDBConnectionsRead",
          actions: ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"],
          resources: [
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.WS_CONNECTIONS}`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.EMERGENCIES}`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "APIGatewayManageConnections",
          actions: ["execute-api:ManageConnections"],
          resources: [`arn:aws:execute-api:${this.region}:${this.account}:*/*/@connections/*`],
        }),
        new iam.PolicyStatement({
          sid: "TranslateText",
          actions: ["translate:TranslateText"],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          sid: "SNSPublish",
          actions: ["sns:Publish"],
          resources: [
            `arn:aws:sns:${this.region}:${this.account}:${NOVAGUARD_CONFIG.SNS.DISPATCHER_ALERTS}`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "SQSAgentMessage",
          actions: ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueUrl"],
          resources: [`arn:aws:sqs:${this.region}:${this.account}:novaguard-agent-messages`],
        }),
      ]),

      // -----------------------------------------------------------------
      // WebSocket Lambda Roles — minimal permissions
      // -----------------------------------------------------------------
      webSocketConnect: this.createAgentRole("WebSocketConnect", bedrockPolicy, [
        new iam.PolicyStatement({
          sid: "DynamoDBConnectionWrite",
          actions: ["dynamodb:PutItem", "dynamodb:DeleteItem"],
          resources: [`arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.WS_CONNECTIONS}`],
        }),
      ]),

      webSocketDisconnect: this.createAgentRole("WebSocketDisconnect", bedrockPolicy, [
        new iam.PolicyStatement({
          sid: "DynamoDBConnectionDelete",
          actions: ["dynamodb:DeleteItem", "dynamodb:GetItem"],
          resources: [`arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.WS_CONNECTIONS}`],
        }),
      ]),

      webSocketMessage: this.createAgentRole("WebSocketMessage", bedrockPolicy, [
        new iam.PolicyStatement({
          sid: "DynamoDBConnectionRead",
          actions: ["dynamodb:GetItem", "dynamodb:UpdateItem"],
          resources: [`arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.WS_CONNECTIONS}`],
        }),
        new iam.PolicyStatement({
          sid: "KinesisCallerEventPut",
          actions: ["kinesis:PutRecord"],
          resources: [`arn:aws:kinesis:${this.region}:${this.account}:stream/${NOVAGUARD_CONFIG.KINESIS.EMERGENCY_STREAM}`],
        }),
        new iam.PolicyStatement({
          sid: "S3MediaWrite",
          actions: ["s3:PutObject", "s3:GetObject"],
          resources: [`arn:aws:s3:::${NOVAGUARD_CONFIG.BUCKETS.MEDIA}-${this.account}/*`],
        }),
      ]),

      callerApi: this.createAgentRole("CallerApi", bedrockPolicy, [
        new iam.PolicyStatement({
          sid: "DynamoDBEmergencyRead",
          actions: ["dynamodb:GetItem", "dynamodb:Query"],
          resources: [
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.EMERGENCIES}`,
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${NOVAGUARD_CONFIG.TABLES.CALLER_PROFILES}`,
          ],
        }),
        new iam.PolicyStatement({
          sid: "S3PresignedUrlGenerate",
          actions: ["s3:PutObject", "s3:GetObject", "s3:GeneratePresignedUrl"],
          resources: [`arn:aws:s3:::${NOVAGUARD_CONFIG.BUCKETS.MEDIA}-${this.account}/*`],
        }),
      ]),

      // -----------------------------------------------------------------
      // Step Functions execution role
      // -----------------------------------------------------------------
      stepFunctionsExecution: new iam.Role(this, "StepFunctionsExecutionRole", {
        roleName: "novaguard-stepfunctions-execution",
        assumedBy: new iam.ServicePrincipal("states.amazonaws.com"),
        description: "Execution role for NovaGuard Step Functions state machine",
        inlinePolicies: {
          InvokeLambdas: new iam.PolicyDocument({
            statements: [
              new iam.PolicyStatement({
                actions: ["lambda:InvokeFunction"],
                resources: [`arn:aws:lambda:${this.region}:${this.account}:function:novaguard-*`],
              }),
              new iam.PolicyStatement({
                actions: [
                  "xray:PutTraceSegments",
                  "xray:PutTelemetryRecords",
                  "xray:GetSamplingRules",
                  "xray:GetSamplingTargets",
                ],
                resources: ["*"],
              }),
              new iam.PolicyStatement({
                actions: ["logs:CreateLogDelivery", "logs:PutLogEvents", "logs:CreateLogGroup"],
                resources: ["*"],
              }),
              new iam.PolicyStatement({
                actions: ["cloudwatch:PutMetricData"],
                resources: ["*"],
              }),
            ],
          }),
        },
      }),

      // EventBridge Pipes execution role
      eventBridgePipe: new iam.Role(this, "EventBridgePipeRole", {
        roleName: "novaguard-eventbridge-pipe",
        assumedBy: new iam.ServicePrincipal("pipes.amazonaws.com"),
        description: "Execution role for EventBridge Pipes connecting Kinesis to Step Functions",
        inlinePolicies: {
          KinesisRead: new iam.PolicyDocument({
            statements: [
              new iam.PolicyStatement({
                actions: [
                  "kinesis:GetRecords",
                  "kinesis:GetShardIterator",
                  "kinesis:DescribeStream",
                  "kinesis:ListStreams",
                  "kinesis:ListShards",
                ],
                resources: [`arn:aws:kinesis:${this.region}:${this.account}:stream/${NOVAGUARD_CONFIG.KINESIS.EMERGENCY_STREAM}`],
              }),
              new iam.PolicyStatement({
                actions: ["states:StartExecution"],
                resources: [`arn:aws:states:${this.region}:${this.account}:stateMachine:novaguard-*`],
              }),
            ],
          }),
        },
      }),
    };

    // -----------------------------------------------------------------
    // SSM Parameters — expose CMK ARN for cross-stack reference without
    // creating implicit CDK cross-stack token dependencies
    // -----------------------------------------------------------------
    new ssm.StringParameter(this, "CMKArnParam", {
      parameterName: "/novaguard/cmk-arn",
      stringValue: this.dataEncryptionKey.keyArn,
      description: "NovaGuard CMK ARN for cross-stack encryption configuration",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "AccountIdParam", {
      parameterName: "/novaguard/account-id",
      stringValue: this.account,
      description: "AWS Account ID — used by agents for constructing ARNs",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "RegionParam", {
      parameterName: "/novaguard/region",
      stringValue: this.region,
      description: "AWS Region — used by agents for constructing service endpoints",
      tier: ssm.ParameterTier.STANDARD,
    });

    // -----------------------------------------------------------------
    // Stack outputs for cross-account deployment and CI/CD
    // -----------------------------------------------------------------
    new cdk.CfnOutput(this, "CMKArn", {
      exportName: "NovaGuard-CMKArn",
      value: this.dataEncryptionKey.keyArn,
      description: "Customer Managed Key ARN for all NovaGuard data-at-rest encryption",
    });

    new cdk.CfnOutput(this, "CMKAlias", {
      exportName: "NovaGuard-CMKAlias",
      value: "alias/novaguard-data",
      description: "KMS key alias",
    });
  }

  /**
   * Factory method for agent Lambda execution roles.
   * Builds a role with a consistent naming convention and applies shared
   * policies (Bedrock, CloudWatch, X-Ray) plus agent-specific statements.
   */
  private createAgentRole(
    agentName: string,
    bedrockPolicy: iam.ManagedPolicy,
    agentSpecificStatements: iam.PolicyStatement[]
  ): iam.Role {
    const role = new iam.Role(this, `${agentName}ExecutionRole`, {
      roleName: `novaguard-${agentName.toLowerCase().replace(/([A-Z])/g, "-$1").replace(/^-/, "")}-exec`,
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: `Execution role for NovaGuard ${agentName} Lambda`,
      managedPolicies: [
        bedrockPolicy,
        iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole"),
      ],
    });

    // KMS decrypt access for all agents (needed for encrypted DynamoDB/SQS reads)
    role.addToPolicy(new iam.PolicyStatement({
      sid: "KMSDecrypt",
      actions: ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
      resources: [this.dataEncryptionKey.keyArn],
    }));

    // X-Ray active tracing
    role.addToPolicy(new iam.PolicyStatement({
      sid: "XRayWrite",
      actions: ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"],
      resources: ["*"],
    }));

    // SSM read for runtime configuration
    role.addToPolicy(new iam.PolicyStatement({
      sid: "SSMReadConfig",
      actions: ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
      resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter/novaguard/*`],
    }));

    // Agent-specific permissions
    for (const statement of agentSpecificStatements) {
      role.addToPolicy(statement);
    }

    return role;
  }
}
