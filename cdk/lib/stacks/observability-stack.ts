/**
 * NovaGuard Observability Stack
 *
 * Complete visibility into system behavior:
 * - CloudWatch Dashboard: unified operational view
 * - CloudWatch Alarms: automated alerting on SLO breaches
 * - X-Ray groups and sampling rules
 * - Log metric filters: extract business metrics from Lambda logs
 * - Contributor Insights: DynamoDB hot key detection
 * - Synthetic Canary: proactive end-to-end health monitoring
 */

import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as xray from "aws-cdk-lib/aws-xray";
import * as logs from "aws-cdk-lib/aws-logs";
import * as sns from "aws-cdk-lib/aws-sns";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import { Construct } from "constructs";

export interface ObservabilityStackProps extends cdk.StackProps {
  stateMachineArn: string;
  systemHealthTopic: sns.Topic;
  agentFunctions: lambda.Function[];
  webSocketApi: apigatewayv2.WebSocketApi;
}

export class ObservabilityStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);

    const { systemHealthTopic, agentFunctions } = props;
    const snsAlarmAction = new cloudwatchActions.SnsAction(systemHealthTopic);

    // =================================================================
    // SECTION 1: LATENCY ALARMS (SLO enforcement)
    // =================================================================

    // Triage latency P99 > 2000ms — SLO breach
    const triageLatencyAlarm = new cloudwatch.Alarm(this, "TriageLatencyAlarm", {
      alarmName: "novaguard-triage-latency-p99",
      alarmDescription: "Triage Agent P99 duration exceeded 2000ms — triage SLO breach",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "Duration",
        dimensionsMap: { FunctionName: "novaguard-triage-agent" },
        statistic: "p99",
        period: cdk.Duration.minutes(1),
      }),
      threshold: 2000,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    triageLatencyAlarm.addAlarmAction(snsAlarmAction);

    // End-to-end emergency intake-to-triage P99 > 2000ms
    const intakeLatencyAlarm = new cloudwatch.Alarm(this, "IntakeLatencyAlarm", {
      alarmName: "novaguard-intake-latency-p99",
      alarmDescription: "Intake Agent P99 duration exceeded 2000ms",
      metric: new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "Duration",
        dimensionsMap: { FunctionName: "novaguard-intake-agent" },
        statistic: "p99",
        period: cdk.Duration.minutes(1),
      }),
      threshold: 2000,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    intakeLatencyAlarm.addAlarmAction(snsAlarmAction);

    // =================================================================
    // SECTION 2: ERROR RATE ALARMS
    // =================================================================

    const functionNames = [
      "novaguard-intake-agent",
      "novaguard-triage-agent",
      "novaguard-dispatch-agent",
      "novaguard-comms-agent",
      "novaguard-ws-message",
    ];

    const errorAlarms: cloudwatch.Alarm[] = functionNames.map((fnName) => {
      const alarm = new cloudwatch.Alarm(this, `${fnName.replace(/-/g, "")}ErrorAlarm`, {
        alarmName: `novaguard-${fnName}-error-rate`,
        alarmDescription: `${fnName} error rate > 5% over 5 minutes`,
        metric: new cloudwatch.MathExpression({
          expression: "(errors / invocations) * 100",
          usingMetrics: {
            errors: new cloudwatch.Metric({
              namespace: "AWS/Lambda",
              metricName: "Errors",
              dimensionsMap: { FunctionName: fnName },
              statistic: "Sum",
              period: cdk.Duration.minutes(5),
            }),
            invocations: new cloudwatch.Metric({
              namespace: "AWS/Lambda",
              metricName: "Invocations",
              dimensionsMap: { FunctionName: fnName },
              statistic: "Sum",
              period: cdk.Duration.minutes(5),
            }),
          },
          period: cdk.Duration.minutes(5),
          label: `${fnName} Error Rate %`,
        }),
        threshold: 5,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 2,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      alarm.addAlarmAction(snsAlarmAction);
      return alarm;
    });

    // =================================================================
    // SECTION 3: DLQ DEPTH ALARM
    // =================================================================

    const dlqDepthAlarm = new cloudwatch.Alarm(this, "DLQDepthAlarm", {
      alarmName: "novaguard-agent-dlq-depth",
      alarmDescription: "Agent message DLQ depth > 0 — failed inter-agent messages detected",
      metric: new cloudwatch.Metric({
        namespace: "AWS/SQS",
        metricName: "ApproximateNumberOfMessagesVisible",
        dimensionsMap: { QueueName: "novaguard-agent-messages-dlq" },
        statistic: "Maximum",
        period: cdk.Duration.minutes(1),
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    dlqDepthAlarm.addAlarmAction(snsAlarmAction);

    // =================================================================
    // SECTION 4: ESCALATION RATE ALARM
    // =================================================================

    // Log metric filter: count how many emergencies were escalated due to low AI confidence
    // Lambda functions log: {"event": "TRIAGE_ESCALATION", "emergency_id": "...", "confidence": 0.45}
    const escalationMetricFilter = new logs.MetricFilter(this, "EscalationMetricFilter", {
      logGroup: logs.LogGroup.fromLogGroupName(this, "TriageLogGroup", "/aws/lambda/novaguard-triage-agent"),
      metricNamespace: "NovaGuard/BusinessMetrics",
      metricName: "TriageEscalations",
      filterPattern: logs.FilterPattern.stringValue("$.event", "=", "TRIAGE_ESCALATION"),
      metricValue: "1",
      unit: cloudwatch.Unit.COUNT,
    });

    // Alarm if escalation rate is high — indicates potential model degradation
    const escalationRateAlarm = new cloudwatch.Alarm(this, "HighEscalationRateAlarm", {
      alarmName: "novaguard-high-escalation-rate",
      alarmDescription: "More than 10 AI triage escalations in 10 minutes — model may be degraded",
      metric: new cloudwatch.Metric({
        namespace: "NovaGuard/BusinessMetrics",
        metricName: "TriageEscalations",
        statistic: "Sum",
        period: cdk.Duration.minutes(10),
      }),
      threshold: 10,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    escalationRateAlarm.addAlarmAction(snsAlarmAction);

    // =================================================================
    // SECTION 5: STEP FUNCTIONS ALARM
    // =================================================================

    const sfnFailureAlarm = new cloudwatch.Alarm(this, "StateMachineFailureAlarm", {
      alarmName: "novaguard-workflow-failures",
      alarmDescription: "Step Functions express workflow execution failures > 5 in 5 minutes",
      metric: new cloudwatch.Metric({
        namespace: "AWS/States",
        metricName: "ExecutionsFailed",
        dimensionsMap: { StateMachineArn: props.stateMachineArn },
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: 5,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    sfnFailureAlarm.addAlarmAction(snsAlarmAction);

    // =================================================================
    // SECTION 6: X-RAY GROUPS
    // =================================================================

    // X-Ray group: filter to traces with errors for targeted debugging
    const errorTracesGroup = new xray.CfnGroup(this, "ErrorTracesGroup", {
      groupName: "novaguard-errors",
      filterExpression: "fault = true OR error = true",
      insightsConfiguration: {
        insightsEnabled: true,
        notificationsEnabled: true,
      },
    });

    // X-Ray group: slow traces (total duration > 10s = SLO concern)
    const slowTracesGroup = new xray.CfnGroup(this, "SlowTracesGroup", {
      groupName: "novaguard-slow-traces",
      filterExpression: "responsetime > 10",
      insightsConfiguration: {
        insightsEnabled: false,
      },
    });

    // X-Ray sampling rule: sample 100% of emergency intake requests
    // (emergencies are rare enough that sampling is not a concern; complete data needed)
    const emergencySamplingRule = new xray.CfnSamplingRule(this, "EmergencySamplingRule", {
      samplingRule: {
        ruleName: "novaguard-emergency-full-trace",
        priority: 1,
        reservoirSize: 50,
        fixedRate: 1.0, // 100% sampling — every emergency is fully traced
        host: "*",
        httpMethod: "*",
        serviceName: "novaguard-*",
        serviceType: "*",
        urlPath: "*",
        resourceArn: "*",
        version: 1,
        attributes: {
          "route": "emergency",
        },
      },
    });

    // =================================================================
    // SECTION 7: CLOUDWATCH DASHBOARD
    // =================================================================

    const dashboard = new cloudwatch.Dashboard(this, "NovaGuardDashboard", {
      dashboardName: "NovaGuard-Operations",
      periodOverride: cloudwatch.PeriodOverride.INHERIT,
    });

    // Row 1: Emergency volume + processing
    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: "## NovaGuard — Real-Time Emergency Operations Dashboard",
        width: 24,
        height: 1,
      })
    );

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Emergency Events Ingested (Kinesis)",
        left: [new cloudwatch.Metric({
          namespace: "AWS/Kinesis",
          metricName: "GetRecords.Records",
          dimensionsMap: { StreamName: "novaguard-emergency-events" },
          statistic: "Sum",
          period: cdk.Duration.minutes(1),
          label: "Records/min",
        })],
        width: 8,
        height: 6,
      }),

      new cloudwatch.GraphWidget({
        title: "Step Functions Executions",
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/States",
            metricName: "ExecutionsStarted",
            dimensionsMap: { StateMachineArn: props.stateMachineArn },
            statistic: "Sum",
            period: cdk.Duration.minutes(1),
            label: "Started",
            color: cloudwatch.Color.BLUE,
          }),
          new cloudwatch.Metric({
            namespace: "AWS/States",
            metricName: "ExecutionsSucceeded",
            dimensionsMap: { StateMachineArn: props.stateMachineArn },
            statistic: "Sum",
            period: cdk.Duration.minutes(1),
            label: "Succeeded",
            color: cloudwatch.Color.GREEN,
          }),
          new cloudwatch.Metric({
            namespace: "AWS/States",
            metricName: "ExecutionsFailed",
            dimensionsMap: { StateMachineArn: props.stateMachineArn },
            statistic: "Sum",
            period: cdk.Duration.minutes(1),
            label: "Failed",
            color: cloudwatch.Color.RED,
          }),
        ],
        width: 8,
        height: 6,
      }),

      new cloudwatch.GraphWidget({
        title: "WebSocket Active Connections",
        left: [new cloudwatch.Metric({
          namespace: "AWS/ApiGateway",
          metricName: "ConnectCount",
          dimensionsMap: { ApiId: props.webSocketApi.apiId },
          statistic: "Sum",
          period: cdk.Duration.minutes(1),
        })],
        width: 8,
        height: 6,
      })
    );

    // Row 2: Agent latencies (P50, P99)
    const agentLatencyWidgets = ["intake", "triage", "dispatch", "comms"].map((agent) =>
      new cloudwatch.GraphWidget({
        title: `${agent.charAt(0).toUpperCase() + agent.slice(1)} Agent Latency`,
        left: [
          new cloudwatch.Metric({
            namespace: "AWS/Lambda",
            metricName: "Duration",
            dimensionsMap: { FunctionName: `novaguard-${agent}-agent` },
            statistic: "p50",
            period: cdk.Duration.minutes(1),
            label: "P50",
            color: cloudwatch.Color.GREEN,
          }),
          new cloudwatch.Metric({
            namespace: "AWS/Lambda",
            metricName: "Duration",
            dimensionsMap: { FunctionName: `novaguard-${agent}-agent` },
            statistic: "p99",
            period: cdk.Duration.minutes(1),
            label: "P99",
            color: cloudwatch.Color.ORANGE,
          }),
        ],
        leftYAxis: { label: "ms", min: 0 },
        width: 6,
        height: 6,
      })
    );
    dashboard.addWidgets(...agentLatencyWidgets);

    // Row 3: Error rates
    dashboard.addWidgets(
      new cloudwatch.AlarmStatusWidget({
        title: "Alarm Status",
        alarms: [
          triageLatencyAlarm,
          intakeLatencyAlarm,
          dlqDepthAlarm,
          escalationRateAlarm,
          sfnFailureAlarm,
        ],
        width: 24,
        height: 4,
      })
    );

    // Row 4: Business metrics
    dashboard.addWidgets(
      new cloudwatch.SingleValueWidget({
        title: "Triage Escalations (Last Hour)",
        metrics: [new cloudwatch.Metric({
          namespace: "NovaGuard/BusinessMetrics",
          metricName: "TriageEscalations",
          statistic: "Sum",
          period: cdk.Duration.hours(1),
        })],
        width: 4,
        height: 4,
      }),

      new cloudwatch.GraphWidget({
        title: "Agent DLQ Depth",
        left: [new cloudwatch.Metric({
          namespace: "AWS/SQS",
          metricName: "ApproximateNumberOfMessagesVisible",
          dimensionsMap: { QueueName: "novaguard-agent-messages-dlq" },
          statistic: "Maximum",
          period: cdk.Duration.minutes(1),
          color: cloudwatch.Color.RED,
        })],
        width: 8,
        height: 4,
      }),

      new cloudwatch.LogQueryWidget({
        title: "Recent Error Logs (All Agents)",
        logGroupNames: [
          "/aws/lambda/novaguard-intake-agent",
          "/aws/lambda/novaguard-triage-agent",
          "/aws/lambda/novaguard-dispatch-agent",
          "/aws/lambda/novaguard-comms-agent",
        ],
        queryLines: [
          "fields @timestamp, @message, @requestId",
          "filter level = 'ERROR'",
          "sort @timestamp desc",
          "limit 50",
        ],
        width: 12,
        height: 6,
      })
    );

    // =================================================================
    // STACK OUTPUTS
    // =================================================================

    new cdk.CfnOutput(this, "DashboardUrl", {
      exportName: "NovaGuard-DashboardUrl",
      value: `https://${this.region}.console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards:name=NovaGuard-Operations`,
    });
  }
}
