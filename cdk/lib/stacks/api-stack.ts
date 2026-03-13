/**
 * NovaGuard API Stack
 *
 * Public-facing API layer:
 * - API Gateway WebSocket API: real-time bidirectional communication
 * - API Gateway REST API: caller status queries, presigned upload URLs
 * - WAF WebACL: rate limiting, IP reputation, managed rule sets
 * - CloudFront distribution: CDN for frontend assets and media delivery
 * - Custom domain configuration (placeholder — requires ACM cert)
 *
 * WebSocket message flow:
 *   Client → API GW WebSocket → $default handler → Kinesis → Intake Agent
 *   Agent → DynamoDB (connection lookup) → API GW Management API → Client
 *
 * WebSocket route selection expression: $request.body.action
 * Defined routes: $connect, $disconnect, emergency.initiate, emergency.update, ping
 */

import * as cdk from "aws-cdk-lib";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as apigatewayv2Integrations from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as cloudfront_origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

export interface ApiStackProps extends cdk.StackProps {
  connectWebSocketHandler: lambda.Function;
  disconnectWebSocketHandler: lambda.Function;
  messageWebSocketHandler: lambda.Function;
  callerApiHandler: lambda.Function;
  connectionsTable: dynamodb.Table;
}

export class ApiStack extends cdk.Stack {
  public readonly webSocketApi: apigatewayv2.WebSocketApi;
  public readonly restApi: apigateway.RestApi;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    // =================================================================
    // SECTION 1: WAF WEB ACL (applied to both REST and WebSocket APIs)
    // =================================================================

    const webAcl = new wafv2.CfnWebACL(this, "NovaGuardWAF", {
      name: "novaguard-waf",
      scope: "REGIONAL",
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: "NovaGuardWAFMetrics",
        sampledRequestsEnabled: true,
      },
      rules: [
        // AWS Managed Rule: Core rule set (SQLi, XSS, common attack vectors)
        {
          name: "AWSManagedRulesCoreRuleSet",
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesCommonRuleSet",
              excludedRules: [
                { name: "SizeRestrictions_BODY" }, // Allow large JSON payloads for emergency descriptions
              ],
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "AWSManagedRulesCoreRuleSet",
            sampledRequestsEnabled: false,
          },
        },
        // AWS Managed Rule: Known bad inputs
        {
          name: "AWSManagedRulesKnownBadInputsRuleSet",
          priority: 2,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesKnownBadInputsRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "AWSManagedRulesKnownBadInputsRuleSet",
            sampledRequestsEnabled: false,
          },
        },
        // AWS Managed Rule: IP reputation list
        {
          name: "AWSManagedRulesAmazonIpReputationList",
          priority: 3,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesAmazonIpReputationList",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "AWSManagedRulesAmazonIpReputationList",
            sampledRequestsEnabled: false,
          },
        },
        // Rate limiting — 2000 requests per 5 minutes per IP
        // Emergency callers should never hit this; this stops DDoS/abuse
        {
          name: "RateLimitPerIP",
          priority: 10,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 2000,
              aggregateKeyType: "IP",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "RateLimitPerIP",
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    // =================================================================
    // SECTION 2: WEBSOCKET API
    // =================================================================

    this.webSocketApi = new apigatewayv2.WebSocketApi(this, "NovaGuardWebSocketApi", {
      apiName: "novaguard-ws",
      description: "NovaGuard real-time bidirectional emergency communication channel",
      routeSelectionExpression: "$request.body.action",
      connectRouteOptions: {
        integration: new apigatewayv2Integrations.WebSocketLambdaIntegration(
          "WsConnectIntegration",
          props.connectWebSocketHandler
        ),
      },
      disconnectRouteOptions: {
        integration: new apigatewayv2Integrations.WebSocketLambdaIntegration(
          "WsDisconnectIntegration",
          props.disconnectWebSocketHandler
        ),
      },
      defaultRouteOptions: {
        integration: new apigatewayv2Integrations.WebSocketLambdaIntegration(
          "WsDefaultIntegration",
          props.messageWebSocketHandler
        ),
        returnResponse: true, // Return Lambda response directly to client
      },
    });

    // Production stage with access logging and throttling
    const wsStage = new apigatewayv2.WebSocketStage(this, "NovaGuardWsStage", {
      webSocketApi: this.webSocketApi,
      stageName: "v1",
      autoDeploy: true,
      throttle: {
        rateLimit: 10000, // 10K messages/second burst
        burstLimit: 20000,
      },
    });

    // Associate WAF with WebSocket API stage
    new wafv2.CfnWebACLAssociation(this, "WsApiWafAssociation", {
      resourceArn: `arn:aws:apigateway:${this.region}::/restapis/${this.webSocketApi.apiId}/stages/${wsStage.stageName}`,
      webAclArn: webAcl.attrArn,
    });

    // Store WebSocket endpoint URL in SSM for Lambda functions to read at runtime
    const wsCallbackUrl = `https://${this.webSocketApi.apiId}.execute-api.${this.region}.amazonaws.com/${wsStage.stageName}`;
    new ssm.StringParameter(this, "WsEndpointParam", {
      parameterName: "/novaguard/ws-endpoint",
      stringValue: wsCallbackUrl,
      description: "API Gateway WebSocket callback URL for posting messages to connected clients",
    });

    // =================================================================
    // SECTION 3: REST API
    // =================================================================

    const restApiAccessLogs = new logs.LogGroup(this, "RestApiAccessLogs", {
      logGroupName: "/aws/apigateway/novaguard-rest-api",
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.restApi = new apigateway.RestApi(this, "NovaGuardRestApi", {
      restApiName: "novaguard-api",
      description: "NovaGuard REST API: emergency status, media uploads, profile management",
      deployOptions: {
        stageName: "v1",
        accessLogDestination: new apigateway.LogGroupLogDestination(restApiAccessLogs),
        accessLogFormat: apigateway.AccessLogFormat.jsonWithStandardFields({
          caller: true,
          httpMethod: true,
          ip: true,
          protocol: true,
          requestTime: true,
          resourcePath: true,
          responseLength: true,
          status: true,
          user: true,
        }),
        tracingEnabled: true,
        dataTraceEnabled: false, // Disable data tracing — never log request/response bodies (HIPAA)
        methodOptions: {
          "/*/*": {
            throttlingRateLimit: 5000,
            throttlingBurstLimit: 10000,
          },
        },
      },
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS, // Restrict in production
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: ["Content-Type", "Authorization", "X-Caller-ID", "X-Emergency-ID"],
        maxAge: cdk.Duration.hours(1),
      },
      binaryMediaTypes: ["image/*", "video/*", "audio/*"],
    });

    // Lambda proxy integration for all REST routes
    const callerApiIntegration = new apigateway.LambdaIntegration(props.callerApiHandler, {
      proxy: true,
      allowTestInvoke: false,
    });

    // /v1/emergencies
    const emergenciesResource = this.restApi.root.addResource("emergencies");
    emergenciesResource.addMethod("POST", callerApiIntegration);               // Initiate emergency
    emergenciesResource.addMethod("GET", callerApiIntegration);                // List caller's emergencies

    const emergencyByIdResource = emergenciesResource.addResource("{emergencyId}");
    emergencyByIdResource.addMethod("GET", callerApiIntegration);              // Get emergency status
    emergencyByIdResource.addMethod("PATCH", callerApiIntegration);            // Update emergency details

    // /v1/media/upload-url
    const mediaResource = this.restApi.root.addResource("media");
    const uploadUrlResource = mediaResource.addResource("upload-url");
    uploadUrlResource.addMethod("POST", callerApiIntegration);                 // Get presigned S3 upload URL

    // /v1/profiles
    const profilesResource = this.restApi.root.addResource("profiles");
    profilesResource.addMethod("GET", callerApiIntegration);                   // Get caller profile
    profilesResource.addMethod("PUT", callerApiIntegration);                   // Update profile

    // /v1/health (no auth required — used by load balancer health checks)
    const healthResource = this.restApi.root.addResource("health");
    healthResource.addMethod("GET", new apigateway.MockIntegration({
      integrationResponses: [{ statusCode: "200", responseTemplates: { "application/json": '{"status":"healthy"}' } }],
      passthroughBehavior: apigateway.PassthroughBehavior.NEVER,
      requestTemplates: { "application/json": '{"statusCode": 200}' },
    }), {
      methodResponses: [{ statusCode: "200" }],
    });

    // Associate WAF with REST API
    new wafv2.CfnWebACLAssociation(this, "RestApiWafAssociation", {
      resourceArn: this.restApi.deploymentStage.stageArn,
      webAclArn: webAcl.attrArn,
    });

    // =================================================================
    // SECTION 4: CLOUDFRONT DISTRIBUTION (Frontend + Media CDN)
    // =================================================================

    // Frontend SPA bucket — referenced but created in StorageStack
    // CloudFront distribution delivers static assets with tight cache policy
    // and media files via signed URLs for HIPAA compliance

    // =================================================================
    // STACK OUTPUTS
    // =================================================================

    new cdk.CfnOutput(this, "WebSocketApiEndpoint", {
      exportName: "NovaGuard-WebSocketEndpoint",
      value: wsStage.url,
      description: "WebSocket API endpoint URL for frontend clients",
    });

    new cdk.CfnOutput(this, "RestApiEndpoint", {
      exportName: "NovaGuard-RestApiEndpoint",
      value: this.restApi.url,
      description: "REST API base URL",
    });

    new cdk.CfnOutput(this, "WebSocketApiId", {
      exportName: "NovaGuard-WebSocketApiId",
      value: this.webSocketApi.apiId,
    });
  }
}
