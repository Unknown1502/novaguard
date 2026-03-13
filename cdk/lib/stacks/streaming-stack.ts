/**
 * NovaGuard Streaming Stack
 *
 * Owns all asynchronous messaging infrastructure:
 * - Kinesis Data Streams: emergency event ingestion pipeline
 * - EventBridge custom bus: agent-to-agent (A2A) event routing
 * - SQS queues: agent message queues (with DLQ pattern)
 * - SNS topics: dispatcher alerts, escalation notifications, system health
 *
 * Architecture decisions:
 * - Kinesis over SQS for emergency intake: Kinesis preserves event ordering
 *   within a shard (by emergency_id), supports multi-consumer fan-out,
 *   and provides 24-hour+ replay capability for re-processing.
 * - SQS over EventBridge for A2A: SQS provides at-least-once delivery with
 *   DLQ for agent-level error isolation. EventBridge would add 100ms+ latency.
 * - SNS for dispatcher alerts: fan-out to multiple endpoints (email, SMS,
 *   push notifications) in a single publish.
 */

import * as cdk from "aws-cdk-lib";
import * as kinesis from "aws-cdk-lib/aws-kinesis";
import * as events from "aws-cdk-lib/aws-events";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as sns from "aws-cdk-lib/aws-sns";
import * as kms from "aws-cdk-lib/aws-kms";
import * as appscaling from "aws-cdk-lib/aws-applicationautoscaling";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import { Construct } from "constructs";
import { NOVAGUARD_CONFIG } from "../config/constants";

export interface StreamingStackProps extends cdk.StackProps {
  encryptionKey: kms.Key;
}

export class StreamingStack extends cdk.Stack {
  // Kinesis
  public readonly emergencyStream: kinesis.Stream;

  // EventBridge
  public readonly novaguardEventBus: events.EventBus;

  // SQS
  public readonly agentMessageQueue: sqs.Queue;
  public readonly agentMessageDLQ: sqs.Queue;
  public readonly sonicBridgeQueue: sqs.Queue;
  public readonly dispatchCommandQueue: sqs.Queue;

  // SNS
  public readonly dispatcherAlertsTopic: sns.Topic;
  public readonly escalationTopic: sns.Topic;
  public readonly systemHealthTopic: sns.Topic;

  constructor(scope: Construct, id: string, props: StreamingStackProps) {
    super(scope, id, props);

    const { encryptionKey } = props;

    // =================================================================
    // SECTION 1: KINESIS DATA STREAMS
    // =================================================================

    // Emergency event stream
    // 3 shards baseline (3,000 records/sec write) — Application Auto Scaling
    // grows the stream to 20 shards when IncomingBytes exceeds 70% of capacity.
    // Baseline vs. 10-static saves ~$2.52/day ($0.015/shard-hr × 7 shards × 24hr).
    this.emergencyStream = new kinesis.Stream(this, "EmergencyStream", {
      streamName: NOVAGUARD_CONFIG.KINESIS.EMERGENCY_STREAM,
      shardCount: NOVAGUARD_CONFIG.KINESIS.SHARD_COUNT,  // 3 — matches right-sized baseline
      retentionPeriod: cdk.Duration.hours(48), // 48-hour replay window
      encryption: kinesis.StreamEncryption.KMS,
      encryptionKey,
      streamMode: kinesis.StreamMode.PROVISIONED, // Provisioned for predictable latency
    });

    // ── Application Auto Scaling for the emergency Kinesis stream ──────────
    // Registers the stream as a scalable target with ServiceNamespace=kinesis.
    // ScalableDimension: kinesis:stream:WriteCapacityUnits (maps to shard count).
    // Min: 3 shards  |  Max: 20 shards  |  Scale-out threshold: 70% write utilization
    //
    // Scale-out trigger:
    //   Metric: IncomingBytes (sum over 1-min window) > 70% of shard write capacity
    //   Each shard supports 1 MB/s write → 3 shards = 3 MB/s = 3,145,728 B per 1-min window
    //   70% of that: 2,202,009 bytes — triggers +1 shard
    //   Cooldown: 300 seconds (5 min) to avoid thrashing during sustained surge
    //
    // Scale-in trigger:
    //   IncomingBytes < 25% of current capacity — scale back toward 3-shard baseline
    //   Cooldown: 600 seconds (10 min) — conservative to prevent oscillation
    const kinesisScalableTarget = new appscaling.CfnScalableTarget(
      this,
      "KinesisStreamScalableTarget",
      {
        maxCapacity:        20,
        minCapacity:        NOVAGUARD_CONFIG.KINESIS.SHARD_COUNT, // 3
        resourceId:         `stream/${this.emergencyStream.streamName}`,
        scalableDimension:  "kinesis:stream:WriteCapacityUnits",
        serviceNamespace:   "kinesis",
        // IAM role is created automatically by Application Auto Scaling service
      }
    );
    kinesisScalableTarget.addDependency(
      this.emergencyStream.node.defaultChild as cdk.CfnResource
    );

    // Step scaling policy — scale OUT when write utilization exceeds 70%
    const scaleOutPolicy = new appscaling.CfnScalingPolicy(
      this,
      "KinesisScaleOutPolicy",
      {
        policyName:           "novaguard-kinesis-scale-out",
        policyType:           "StepScaling",
        resourceId:           kinesisScalableTarget.resourceId,
        scalableDimension:    kinesisScalableTarget.scalableDimension,
        serviceNamespace:     kinesisScalableTarget.serviceNamespace,
        stepScalingPolicyConfiguration: {
          adjustmentType:          "ChangeInCapacity",
          cooldown:                300,  // 5-minute cooldown between scale-out events
          metricAggregationType:   "Sum",
          stepAdjustments: [
            // 70–85% utilization: add 2 shards
            { metricIntervalLowerBound: 0, metricIntervalUpperBound: 15, scalingAdjustment: 2 },
            // 85–100% utilization: add 5 shards (burst surge)
            { metricIntervalLowerBound: 15, scalingAdjustment: 5 },
          ],
        },
      }
    );
    scaleOutPolicy.addDependency(kinesisScalableTarget);

    // Step scaling policy — scale IN when throughput drops under 25%
    const scaleInPolicy = new appscaling.CfnScalingPolicy(
      this,
      "KinesisScaleInPolicy",
      {
        policyName:           "novaguard-kinesis-scale-in",
        policyType:           "StepScaling",
        resourceId:           kinesisScalableTarget.resourceId,
        scalableDimension:    kinesisScalableTarget.scalableDimension,
        serviceNamespace:     kinesisScalableTarget.serviceNamespace,
        stepScalingPolicyConfiguration: {
          adjustmentType:          "ChangeInCapacity",
          cooldown:                600,  // 10-minute cooldown on scale-in (conservative)
          metricAggregationType:   "Sum",
          stepAdjustments: [
            // Below 0 means utilization dropped; remove 1 shard at a time toward minimum
            { metricIntervalUpperBound: 0, scalingAdjustment: -1 },
          ],
        },
      }
    );
    scaleInPolicy.addDependency(kinesisScalableTarget);

    // CloudWatch alarm: fires scale-out policy when IncomingBytes/min > 70% capacity
    // At 3 shards: 70% of 3 MB/s × 60s = ~2,097,152 bytes/min trigger point
    const highWriteAlarm = new cloudwatch.Alarm(this, "KinesisHighWriteAlarm", {
      alarmName:          "novaguard-kinesis-high-write-utilization",
      alarmDescription:   "Kinesis stream write utilization exceeds 70% — scale out triggered",
      metric: new cloudwatch.Metric({
        namespace:   "AWS/Kinesis",
        metricName:  "IncomingBytes",
        dimensionsMap: { StreamName: this.emergencyStream.streamName },
        period:      cdk.Duration.minutes(1),
        statistic:   "Sum",
      }),
      threshold:          2097152,           // 70% of 3-shard capacity (3 MB/s × 60s × 0.70)
      evaluationPeriods:  2,                 // 2 consecutive minutes before alarm (avoids spikes)
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData:   cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Wire alarm → scale-out policy.
    // CfnScalingPolicy.ref returns the policy ARN in CloudFormation, which is
    // the correct format expected by CW AlarmActions for Application Auto Scaling.
    (highWriteAlarm.node.defaultChild as cloudwatch.CfnAlarm).alarmActions = [
      scaleOutPolicy.ref,
    ];

    // =================================================================
    // SECTION 2: EVENTBRIDGE CUSTOM BUS
    // =================================================================

    // Custom event bus for A2A routing between agents
    // Event schema: see docs/event-schema.md
    this.novaguardEventBus = new events.EventBus(this, "NovaGuardEventBus", {
      eventBusName: "novaguard-agent-events",
      description: "NovaGuard agent-to-agent coordination event bus",
    });

    // Archive for replay/debug — catch-all pattern archives every event on this bus
    this.novaguardEventBus.archive("AgentEventsArchive", {
      archiveName: "novaguard-agent-events-archive",
      retention: cdk.Duration.days(30),
      description: "30-day archive of all inter-agent events for debugging and replay",
      eventPattern: { account: [cdk.Aws.ACCOUNT_ID] }, // catch-all: archive everything
    });

    // =================================================================
    // SECTION 3: SQS QUEUES (A2A Communication)
    // =================================================================

    // Dead-letter queue for all agent message queues
    // Messages that fail 3 times land here for investigation
    this.agentMessageDLQ = new sqs.Queue(this, "AgentMessageDLQ", {
      queueName: "novaguard-agent-messages-dlq",
      retentionPeriod: cdk.Duration.days(14), // 14-day DLQ retention for post-mortem
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: encryptionKey,
    });

    // Main inter-agent message queue
    // Used by: Intake → Triage, Triage → Dispatch, Dispatch → Comms
    // Visibility timeout must exceed agent processing time (60s is max agent timeout)
    this.agentMessageQueue = new sqs.Queue(this, "AgentMessageQueue", {
      queueName: "novaguard-agent-messages",
      visibilityTimeout: cdk.Duration.seconds(90), // 1.5x max processing time
      receiveMessageWaitTime: cdk.Duration.seconds(20), // Long polling — reduces empty receives
      retentionPeriod: cdk.Duration.hours(4), // Emergency messages stale after 4 hours
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: encryptionKey,
      deadLetterQueue: {
        queue: this.agentMessageDLQ,
        maxReceiveCount: 3,
      },
    });

    // Dedicated queue for Sonic bridge messages
    // Lower visibility timeout because Sonic must respond in <1.5s
    this.sonicBridgeQueue = new sqs.Queue(this, "SonicBridgeQueue", {
      queueName: "novaguard-sonic-bridge",
      visibilityTimeout: cdk.Duration.seconds(10),
      receiveMessageWaitTime: cdk.Duration.seconds(5),
      retentionPeriod: cdk.Duration.hours(1),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: encryptionKey,
      deadLetterQueue: {
        queue: this.agentMessageDLQ,
        maxReceiveCount: 2, // Voice fails fast — don't retry stale audio
      },
    });

    // Dedicated queue for dispatch command execution
    this.dispatchCommandQueue = new sqs.Queue(this, "DispatchCommandQueue", {
      queueName: "novaguard-dispatch-commands",
      visibilityTimeout: cdk.Duration.seconds(120), // Nova Act can take 60-90 seconds
      receiveMessageWaitTime: cdk.Duration.seconds(20),
      retentionPeriod: cdk.Duration.hours(2),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: encryptionKey,
      deadLetterQueue: {
        queue: this.agentMessageDLQ,
        maxReceiveCount: 3,
      },
    });

    // =================================================================
    // SECTION 4: SNS TOPICS
    // =================================================================

    // Topic for dispatcher alert notifications
    // Subscribers: email endpoints, PagerDuty webhook, SMS via SNS
    this.dispatcherAlertsTopic = new sns.Topic(this, "DispatcherAlertsTopic", {
      topicName: NOVAGUARD_CONFIG.SNS.DISPATCHER_ALERTS,
      displayName: "NovaGuard Dispatcher Alerts",
      masterKey: encryptionKey,
    });

    // Topic for emergency escalations (response time exceeded, AI confidence low)
    this.escalationTopic = new sns.Topic(this, "EscalationTopic", {
      topicName: NOVAGUARD_CONFIG.SNS.ESCALATION,
      displayName: "NovaGuard Emergency Escalation",
      masterKey: encryptionKey,
    });

    // Topic for system health alerts (Lambda errors, DLQ depth, Bedrock throttling)
    this.systemHealthTopic = new sns.Topic(this, "SystemHealthTopic", {
      topicName: NOVAGUARD_CONFIG.SNS.SYSTEM_HEALTH,
      displayName: "NovaGuard System Health",
      masterKey: encryptionKey,
    });

    // =================================================================
    // STACK OUTPUTS
    // =================================================================

    new cdk.CfnOutput(this, "EmergencyStreamArn", {
      exportName: "NovaGuard-EmergencyStreamArn",
      value: this.emergencyStream.streamArn,
    });

    new cdk.CfnOutput(this, "EventBusArn", {
      exportName: "NovaGuard-EventBusArn",
      value: this.novaguardEventBus.eventBusArn,
    });

    new cdk.CfnOutput(this, "AgentMessageQueueUrl", {
      exportName: "NovaGuard-AgentMessageQueueUrl",
      value: this.agentMessageQueue.queueUrl,
    });

    new cdk.CfnOutput(this, "DispatcherAlertsTopicArn", {
      exportName: "NovaGuard-DispatcherAlertsTopicArn",
      value: this.dispatcherAlertsTopic.topicArn,
    });
  }
}
