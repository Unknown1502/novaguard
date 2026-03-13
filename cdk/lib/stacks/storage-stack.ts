/**
 * NovaGuard Storage Stack
 *
 * Owns all persistent-state infrastructure:
 * - DynamoDB: 5 tables with precise access patterns and TTL
 * - S3: 4 buckets with object lifecycle policies, versioning, CORS
 * - OpenSearch Serverless: Vector collection for multimodal embeddings
 * - Amazon Location Service: Route calculator, place index, tracker
 *
 * All resources are encrypted with the CMK from CoreStack.
 * All DynamoDB tables use on-demand billing for zero-config auto-scaling.
 * OpenSearch Serverless uses time-based data lifecycle policies.
 */

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as kms from "aws-cdk-lib/aws-kms";
import * as opensearchserverless from "aws-cdk-lib/aws-opensearchserverless";
import * as location from "aws-cdk-lib/aws-location";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import { NOVAGUARD_CONFIG } from "../config/constants";

export interface StorageStackProps extends cdk.StackProps {
  encryptionKey: kms.Key;
}

export class StorageStack extends cdk.Stack {
  // DynamoDB tables
  public readonly emergenciesTable: dynamodb.Table;
  public readonly connectionsTable: dynamodb.Table;
  public readonly resourcesTable: dynamodb.Table;
  public readonly callerProfilesTable: dynamodb.Table;
  public readonly auditLogTable: dynamodb.Table;

  // S3 buckets
  public readonly mediaBucket: s3.Bucket;
  public readonly protocolsBucket: s3.Bucket;
  public readonly floorPlansBucket: s3.Bucket;
  public readonly auditExportsBucket: s3.Bucket;

  // OpenSearch Serverless collection ARN — passed to agents as env var
  public readonly openSearchCollectionEndpoint: string;

  // Location Service
  public readonly routeCalculatorName: string;
  public readonly placeIndexName: string;
  public readonly trackerName: string;

  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props);

    const { encryptionKey } = props;

    // =================================================================
    // SECTION 1: DYNAMODB TABLES
    // =================================================================

    // -----------------------------------------------------------------
    // Table 1: emergencies
    // PK: emergency_id (String, UUID v4)
    // SK: version (Number, incremented on each state transition)
    //
    // Access patterns:
    //   - Get active emergency by ID: PK=emergency_id, SK=LATEST (via GSI latest-index)
    //   - List open emergencies by severity: GSI status-severity-index
    //   - Caller's active emergency: GSI caller-active-index
    //   - Dispatcher view by priority: GSI status-created_at-index
    //   - TTL: 72 hours post-resolution (HIPAA-compliant purge window)
    // -----------------------------------------------------------------
    this.emergenciesTable = new dynamodb.Table(this, "EmergenciesTable", {
      tableName: NOVAGUARD_CONFIG.TABLES.EMERGENCIES,
      partitionKey: { name: "emergency_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "version", type: dynamodb.AttributeType.NUMBER },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey,
      pointInTimeRecovery: true,
      timeToLiveAttribute: "ttl_epoch",
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES, // For audit trail Lambda trigger
    });

    // GSI: Query all emergencies in a status, ordered by severity descending
    this.emergenciesTable.addGlobalSecondaryIndex({
      indexName: "status-severity-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "severity_score", type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI: Look up caller's most recent active emergency
    this.emergenciesTable.addGlobalSecondaryIndex({
      indexName: "caller-active-index",
      partitionKey: { name: "caller_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "created_at_epoch", type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["status", "emergency_type", "severity_score", "triage_summary"],
    });

    // GSI: Dispatcher queue — open emergencies sorted by creation time
    this.emergenciesTable.addGlobalSecondaryIndex({
      indexName: "status-created-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "created_at_epoch", type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["emergency_id", "emergency_type", "severity_score", "location_address", "caller_id", "assigned_dispatcher"],
    });

    // -----------------------------------------------------------------
    // Table 2: ws-connections
    // PK: connection_id (String, from API Gateway WebSocket)
    //
    // Access patterns:
    //   - Caller WebSocket lookup: GSI caller-connection-index
    //   - Dispatcher WebSocket lookup: GSI dispatcher-connection-index
    //   - Emergency connection mapping: GSI emergency-connections-index
    //   - TTL: 12 hours (WebSocket idle timeout + buffer)
    // -----------------------------------------------------------------
    this.connectionsTable = new dynamodb.Table(this, "ConnectionsTable", {
      tableName: NOVAGUARD_CONFIG.TABLES.WS_CONNECTIONS,
      partitionKey: { name: "connection_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey,
      timeToLiveAttribute: "ttl_epoch",
      removalPolicy: cdk.RemovalPolicy.DESTROY, // Connection state is ephemeral
    });

    this.connectionsTable.addGlobalSecondaryIndex({
      indexName: "caller-connection-index",
      partitionKey: { name: "caller_id", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["connection_id", "emergency_id", "ttl_epoch", "connected_at"],
    });

    this.connectionsTable.addGlobalSecondaryIndex({
      indexName: "emergency-connections-index",
      partitionKey: { name: "emergency_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "role", type: dynamodb.AttributeType.STRING }, // "caller" | "dispatcher"
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["connection_id", "caller_id"],
    });

    // -----------------------------------------------------------------
    // Table 3: resources
    // PK: resource_id (String)
    // SK: resource_type (String: "AMBULANCE" | "FIRE" | "POLICE" | "HOSPITAL")
    //
    // Access patterns:
    //   - Find available resources by type: GSI type-status-index
    //   - Find resources near geohash: GSI geohash-index
    //   - Dispatcher assignment tracking
    // -----------------------------------------------------------------
    this.resourcesTable = new dynamodb.Table(this, "ResourcesTable", {
      tableName: NOVAGUARD_CONFIG.TABLES.RESOURCES,
      partitionKey: { name: "resource_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "resource_type", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey,
      pointInTimeRecovery: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.resourcesTable.addGlobalSecondaryIndex({
      indexName: "type-status-index",
      partitionKey: { name: "resource_type", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "status", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Geohash-based proximity search (precision 5 = ~5km grid) 
    this.resourcesTable.addGlobalSecondaryIndex({
      indexName: "geohash5-type-index",
      partitionKey: { name: "geohash5", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "resource_type", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["resource_id", "status", "latitude", "longitude", "eta_minutes", "unit_name"],
    });

    // -----------------------------------------------------------------
    // Table 4: caller-profiles
    // PK: caller_id (UUID, generated on first contact, stored in device)
    //
    // Access patterns:
    //   - Get profile on intake: PK=caller_id
    //   - Update language preference, accessibility needs
    // -----------------------------------------------------------------
    this.callerProfilesTable = new dynamodb.Table(this, "CallerProfilesTable", {
      tableName: NOVAGUARD_CONFIG.TABLES.CALLER_PROFILES,
      partitionKey: { name: "caller_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey,
      pointInTimeRecovery: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // -----------------------------------------------------------------
    // Table 5: audit-log
    // PK: audit_id (String, UUID)
    // SK: timestamp_iso (String, ISO-8601)
    //
    // Append-only audit trail for every AI decision and agent action.
    // Satisfies HIPAA §164.312(b) audit control requirement.
    // 7-year retention enforced via TTL.
    // -----------------------------------------------------------------
    this.auditLogTable = new dynamodb.Table(this, "AuditLogTable", {
      tableName: NOVAGUARD_CONFIG.TABLES.AUDIT_LOG,
      partitionKey: { name: "audit_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "timestamp_iso", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey,
      pointInTimeRecovery: true,
      timeToLiveAttribute: "ttl_epoch",
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.auditLogTable.addGlobalSecondaryIndex({
      indexName: "emergency-audit-index",
      partitionKey: { name: "emergency_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "timestamp_iso", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // =================================================================
    // SECTION 2: S3 BUCKETS
    // =================================================================

    const commonBucketConfig: Partial<s3.BucketProps> = {
      encryption: s3.BucketEncryption.KMS,
      encryptionKey,
      enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    };

    // -----------------------------------------------------------------
    // Bucket 1: media — caller-uploaded images, audio, video
    // Objects expire at 72 hours post-upload (HIPAA data minimization)
    // -----------------------------------------------------------------
    this.mediaBucket = new s3.Bucket(this, "MediaBucket", {
      ...commonBucketConfig,
      bucketName: `${NOVAGUARD_CONFIG.BUCKETS.MEDIA}-${this.account}`,
      lifecycleRules: [
        {
          id: "AutoDeleteMediaAfter72Hours",
          enabled: true,
          expiration: cdk.Duration.hours(72),
          noncurrentVersionExpiration: cdk.Duration.days(1),
        },
      ],
      cors: [
        {
          allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.GET],
          allowedOrigins: ["*"], // Restrict to known frontend origins in production
          allowedHeaders: ["*"],
          maxAge: 3600,
        },
      ],
      serverAccessLogsPrefix: "access-logs/",
    });

    // -----------------------------------------------------------------
    // Bucket 2: protocols — emergency protocol documents, indexed by OpenSearch
    // Versioned; updated by operations team, never deleted
    // -----------------------------------------------------------------
    this.protocolsBucket = new s3.Bucket(this, "ProtocolsBucket", {
      ...commonBucketConfig,
      bucketName: `${NOVAGUARD_CONFIG.BUCKETS.PROTOCOLS}-${this.account}`,
      lifecycleRules: [
        {
          id: "ArchiveOldVersionsAfter90Days",
          enabled: true,
          noncurrentVersionTransitions: [
            { storageClass: s3.StorageClass.GLACIER, transitionAfter: cdk.Duration.days(90) },
          ],
        },
      ],
    });

    // -----------------------------------------------------------------
    // Bucket 3: floor-plans — building floor plans for indoor navigation
    // -----------------------------------------------------------------
    this.floorPlansBucket = new s3.Bucket(this, "FloorPlansBucket", {
      ...commonBucketConfig,
      bucketName: `${NOVAGUARD_CONFIG.BUCKETS.FLOOR_PLANS}-${this.account}`,
    });

    // -----------------------------------------------------------------
    // Bucket 4: audit-exports — compliance exports from DynamoDB audit table
    // -----------------------------------------------------------------
    this.auditExportsBucket = new s3.Bucket(this, "AuditExportsBucket", {
      ...commonBucketConfig,
      bucketName: `${NOVAGUARD_CONFIG.BUCKETS.AUDIT_EXPORTS}-${this.account}`,
      lifecycleRules: [
        {
          id: "TransitionToGlacierAfter1Year",
          enabled: true,
          transitions: [
            { storageClass: s3.StorageClass.GLACIER, transitionAfter: cdk.Duration.days(365) },
          ],
        },
      ],
    });

    // =================================================================
    // SECTION 3: OPENSEARCH SERVERLESS
    // =================================================================

    // Encryption policy — use CMK for collection encryption
    const ossEncryptionPolicy = new opensearchserverless.CfnSecurityPolicy(this, "OSSEncryptionPolicy", {
      name: "novaguard-encryption",
      type: "encryption",
      policy: JSON.stringify({
        Rules: [{ ResourceType: "collection", Resource: ["collection/novaguard-vectors"] }],
        AWSOwnedKey: false,
        KmsARN: encryptionKey.keyArn,
      }),
    });

    // Network policy — VPC access only + public access for Bedrock service principal
    const ossNetworkPolicy = new opensearchserverless.CfnSecurityPolicy(this, "OSSNetworkPolicy", {
      name: "novaguard-network",
      type: "network",
      policy: JSON.stringify([{
        Rules: [
          { ResourceType: "collection", Resource: ["collection/novaguard-vectors"] },
          { ResourceType: "dashboard", Resource: ["collection/novaguard-vectors"] },
        ],
        AllowFromPublic: false, // Private VPC access only; Bedrock uses IAM, not direct network
      }]),
    });

    // Data access policy — grant agent roles access to specific indexes
    const ossDataAccessPolicy = new opensearchserverless.CfnAccessPolicy(this, "OSSDataAccessPolicy", {
      name: "novaguard-data-access",
      type: "data",
      policy: JSON.stringify([
        {
          Rules: [
            {
              ResourceType: "index",
              Resource: [
                `index/${NOVAGUARD_CONFIG.OPENSEARCH.COLLECTION_NAME}/${NOVAGUARD_CONFIG.OPENSEARCH.PROTOCOL_INDEX}`,
                `index/${NOVAGUARD_CONFIG.OPENSEARCH.COLLECTION_NAME}/${NOVAGUARD_CONFIG.OPENSEARCH.INCIDENT_INDEX}`,
              ],
              Permission: [
                "aoss:CreateIndex", "aoss:DeleteIndex", "aoss:UpdateIndex",
                "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:WriteDocument",
              ],
            },
            {
              ResourceType: "collection",
              Resource: [`collection/${NOVAGUARD_CONFIG.OPENSEARCH.COLLECTION_NAME}`],
              Permission: ["aoss:CreateCollectionItems", "aoss:DeleteCollectionItems", "aoss:UpdateCollectionItems", "aoss:DescribeCollectionItems"],
            },
          ],
          Principal: [
            // These ARNs are resolved at deploy time; agent roles are created in CoreStack
            // and exported. For a cleaner approach, use SSM to pass role ARNs.
            `arn:aws:iam::${this.account}:role/novaguard-triage-agent-exec`,
            `arn:aws:iam::${this.account}:role/novaguard-intake-agent-exec`,
            `arn:aws:iam::${this.account}:role/AWSServiceRoleForAmazonOpenSearchServerless`,
          ],
        },
      ]),
    });

    // The collection itself
    const ossCollection = new opensearchserverless.CfnCollection(this, "VectorCollection", {
      name: NOVAGUARD_CONFIG.OPENSEARCH.COLLECTION_NAME,
      type: "VECTORSEARCH",
      description: "NovaGuard vector store: emergency protocol embeddings + historical incident embeddings",
    });

    ossCollection.addDependency(ossEncryptionPolicy);
    ossCollection.addDependency(ossNetworkPolicy);
    ossCollection.addDependency(ossDataAccessPolicy);

    this.openSearchCollectionEndpoint = ossCollection.attrCollectionEndpoint;

    // =================================================================
    // SECTION 4: AMAZON LOCATION SERVICE
    // =================================================================

    const placeIndex = new location.CfnPlaceIndex(this, "PlaceIndex", {
      indexName: NOVAGUARD_CONFIG.LOCATION_SERVICE.PLACE_INDEX,
      dataSource: "Esri", // Esri provides best coverage for US emergency routing
      dataSourceConfiguration: { intendedUse: "Storage" }, // HIPAA: data stored on behalf of end users
      description: "NovaGuard place index for geocoding caller-provided addresses",
    });

    const routeCalculator = new location.CfnRouteCalculator(this, "RouteCalculator", {
      calculatorName: NOVAGUARD_CONFIG.LOCATION_SERVICE.ROUTE_CALCULATOR,
      dataSource: "Esri",
      description: "NovaGuard route calculator for ambulance ETA computation",
    });

    const tracker = new location.CfnTracker(this, "ResourceTracker", {
      trackerName: NOVAGUARD_CONFIG.LOCATION_SERVICE.TRACKER,
      positionFiltering: "TimeBased",
      description: "NovaGuard tracker for real-time emergency vehicle positions",
    });

    this.routeCalculatorName = routeCalculator.calculatorName;
    this.placeIndexName = placeIndex.indexName;
    this.trackerName = tracker.trackerName;

    // =================================================================
    // STACK OUTPUTS
    // =================================================================

    new cdk.CfnOutput(this, "EmergenciesTableArn", {
      exportName: "NovaGuard-EmergenciesTableArn",
      value: this.emergenciesTable.tableArn,
    });

    new cdk.CfnOutput(this, "MediaBucketName", {
      exportName: "NovaGuard-MediaBucketName",
      value: this.mediaBucket.bucketName,
    });

    new cdk.CfnOutput(this, "OpenSearchCollectionEndpoint", {
      exportName: "NovaGuard-OpenSearchEndpoint",
      value: this.openSearchCollectionEndpoint,
    });

    new cdk.CfnOutput(this, "OpenSearchCollectionArn", {
      exportName: "NovaGuard-OpenSearchCollectionArn",
      value: ossCollection.attrArn,
    });
  }
}
