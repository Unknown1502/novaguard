"""
NovaGuard Intake Agent — Lambda Handler

Trigger: Amazon Kinesis Data Streams (emergency-events stream)
Batch size: 10, bisect-on-error, parallelization-factor 5

Responsibilities:
1. Deserialize and validate incoming WebSocket emergency messages
2. Language detection via Amazon Translate
3. Multimodal embedding of any attached images via Nova MM Embedding
4. Geocode caller's address via Amazon Location Service (MCP tool)
5. Create EmergencyRecord in DynamoDB (version 1)
6. Start Step Functions Express Workflow execution
7. Send acknowledgment back to caller via WebSocket (connection ID from message)

Performance contract: <2000ms total end-to-end (P99)

Failure handling:
- Individual record failures are reported via reportBatchItemFailures
  so Kinesis does NOT retry the entire batch (only the failed shard subset)
- If embedding fails: proceed without embedding (set embedding_ids = [])
- If geocoding fails: proceed with raw address only
- If DynamoDB write fails: raise — let Kinesis retry
- If Step Functions start fails: raise — let Kinesis retry

X-Ray: Active tracing enabled. Annotates with emergency_id, severity_hint.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

# Shared utilities (from Lambda layer)
import sys
sys.path.insert(0, "/opt/python")
sys.path.insert(0, os.path.dirname(__file__))

from observability import log, set_correlation_context, timed_operation, annotate_xray_trace, emit_business_metric

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("POWERTOOLS_LOG_LEVEL", "INFO"))

# AWS clients (module-level for connection reuse)
dynamo = boto3.resource("dynamodb", region_name=os.environ.get("REGION", "us-east-1"))
sfn_client = boto3.client("stepfunctions", region_name=os.environ.get("REGION", "us-east-1"))
translate_client = boto3.client("translate", region_name=os.environ.get("REGION", "us-east-1"))
ssm_client = boto3.client("ssm", region_name=os.environ.get("REGION", "us-east-1"))
apigw_client = None  # Initialized lazily — requires WebSocket API endpoint

EMERGENCIES_TABLE      = os.environ.get("EMERGENCIES_TABLE", "novaguard-emergencies")
CALLER_PROFILES_TABLE  = os.environ.get("CALLER_PROFILES_TABLE", "novaguard-caller-profiles")
STATE_MACHINE_ARN_PARAM = os.environ.get("STEP_FUNCTIONS_ARN_PARAM", "/novaguard/emergency-workflow-arn")


def _get_state_machine_arn() -> str:
    """Retrieve state machine ARN from SSM (cached at module level after first call)."""
    if not hasattr(_get_state_machine_arn, "_cached_arn"):
        response = ssm_client.get_parameter(Name=STATE_MACHINE_ARN_PARAM)
        _get_state_machine_arn._cached_arn = response["Parameter"]["Value"]
    return _get_state_machine_arn._cached_arn


def _get_apigw_client(endpoint_url: str):
    """Get API Gateway Management API client for posting messages to WebSocket connections."""
    global apigw_client
    if apigw_client is None:
        apigw_client = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint_url)
    return apigw_client


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Kinesis batch event handler.

    Processes a batch of Kinesis records. Each record is a serialized
    WebSocketInboundMessage with action="emergency.initiate".

    Returns Kinesis partial batch failure response to enable selective retry.
    """
    failed_item_ids: List[Dict[str, str]] = []
    records = event.get("Records", [])

    log.info("INTAKE_BATCH_START", f"Processing Kinesis batch of {len(records)} records",
             batch_size=len(records), request_id=context.aws_request_id)

    for record in records:
        sequence_number = record.get("kinesis", {}).get("sequenceNumber", "unknown")
        try:
            _process_single_record(record, context)
        except Exception as e:
            log.error("INTAKE_RECORD_FAILED", f"Failed to process Kinesis record {sequence_number}",
                      exc=e, sequence_number=sequence_number)
            failed_item_ids.append({"itemIdentifier": sequence_number})

    # Kinesis partial batch failure — only return failed sequence numbers
    return {"batchItemFailures": failed_item_ids}


def _process_single_record(record: Dict[str, Any], context: Any) -> None:
    """
    Process a single Kinesis record end-to-end.
    Raises on unrecoverable errors to trigger Kinesis retry.
    """
    # Decode Kinesis record
    raw_data = base64.b64decode(record["kinesis"]["data"]).decode("utf-8")
    message = json.loads(raw_data)

    emergency_id = str(uuid.uuid4())
    correlation_id = context.aws_request_id

    set_correlation_context(correlation_id, emergency_id)
    annotate_xray_trace(emergency_id)

    log.info("INTAKE_START", "Beginning emergency intake processing",
             caller_id=message.get("caller_id"), action=message.get("action"))

    # -----------------------------------------------------------------------
    # Step 1: Validate and extract message payload
    # -----------------------------------------------------------------------
    action = message.get("action", "")
    if action not in ("emergency.initiate", "emergency.update"):
        log.warn("INTAKE_SKIP", f"Skipping non-emergency Kinesis record with action: {action}")
        return

    payload         = message.get("payload", {})
    caller_id       = message.get("caller_id") or str(uuid.uuid4())
    connection_id   = message.get("connection_id", "")
    text_content    = payload.get("text_content", "")
    image_s3_keys   = payload.get("image_s3_keys", [])

    if not text_content and not image_s3_keys:
        log.warn("INTAKE_EMPTY_MESSAGE", "Received emergency with no text or image content",
                 caller_id=caller_id)
        # Still create a record — dispatcher can query caller via voice bridge
        text_content = "[No description provided — voice or visual may be available]"

    # -----------------------------------------------------------------------
    # Step 2: Language detection and translation to English
    # -----------------------------------------------------------------------
    translated_description = None
    detected_language = "en"

    if text_content and text_content.strip():
        with timed_operation("language_detection", slo_budget_ms=500):
            try:
                detection_result = translate_client.detect_dominant_language(Text=text_content[:300])
                languages = detection_result.get("Languages", [])
                detected_language = languages[0]["LanguageCode"] if languages else "en"

                if detected_language != "en":
                    translation_result = translate_client.translate_text(
                        Text=text_content,
                        SourceLanguageCode=detected_language,
                        TargetLanguageCode="en",
                    )
                    translated_description = translation_result["TranslatedText"]
                    log.info("INTAKE_TRANSLATED", f"Translated from {detected_language} to English",
                             source_language=detected_language, char_count=len(text_content))
            except ClientError as e:
                log.warn("INTAKE_TRANSLATE_FAILED", "Language detection/translation failed", exc=e)
                # Proceed with original text

    # -----------------------------------------------------------------------
    # Step 3: Multimodal embedding for attached images
    # -----------------------------------------------------------------------
    embedding_ids = []

    if image_s3_keys:
        with timed_operation("image_embedding", slo_budget_ms=3000):
            try:
                from bedrock_client import generate_embedding
                from opensearch_client import upsert_embedding_document

                for s3_key in image_s3_keys[:3]:  # Process max 3 images per emergency
                    # Generate embedding for image + text context combined
                    embedding_vector = generate_embedding(
                        text=translated_description or text_content or "emergency scene photo",
                        image_s3_uri=f"s3://{os.environ['MEDIA_BUCKET']}/{s3_key}",
                    )
                    # Store in OpenSearch incident index for later protocol retrieval
                    doc_id = upsert_embedding_document(
                        index=os.environ.get("OPENSEARCH_PROTOCOL_INDEX", "emergency-protocols"),
                        doc_id=f"{emergency_id}-{s3_key.split('/')[-1]}",
                        embedding=embedding_vector,
                        metadata={
                            "emergency_id": emergency_id,
                            "s3_key":       s3_key,
                            "text_context": (translated_description or text_content)[:500],
                        },
                    )
                    embedding_ids.append(doc_id)
                    log.info("INTAKE_EMBEDDED_IMAGE", f"Embedded image: {s3_key}",
                             doc_id=doc_id, embedding_dims=len(embedding_vector))
            except Exception as e:
                log.warn("INTAKE_EMBED_FAILED", "Image embedding failed (non-fatal)", exc=e)

    # -----------------------------------------------------------------------
    # Step 3b: Text embedding + Duplicate Incident Deduplication
    # Generate a 1024-dim embedding for the caller's description and query
    # OpenSearch for open incidents with cosine similarity >= 0.88.
    # If a match is found, merge the caller into the existing incident rather
    # than spinning up a new Step Functions pipeline — avoids flooding dispatch
    # with duplicate resource requests for the same event (e.g., multi-caller
    # traffic accidents where a dozen bystanders report the same crash).
    # -----------------------------------------------------------------------
    text_embedding: list = []
    duplicate_emergency_id: Optional[str] = None

    description_for_embed = (translated_description or text_content or "").strip()
    if description_for_embed:
        with timed_operation("text_embedding_and_dedup", slo_budget_ms=1500):
            try:
                from bedrock_client import generate_embedding
                from opensearch_client import search_recent_open_incidents, upsert_embedding_document

                text_embedding = generate_embedding(text=description_for_embed[:1000])

                # Search for recent open incidents with the same embedding
                similar_hits = search_recent_open_incidents(
                    index     = os.environ.get("OPENSEARCH_INCIDENT_INDEX", "historical-incidents"),
                    embedding = text_embedding,
                    k         = 3,
                    min_score = 0.88,
                )

                if similar_hits:
                    best_hit   = similar_hits[0]           # highest cosine score
                    best_score = best_hit.get("_score", 0)
                    existing_emergency_id = best_hit["_source"].get("emergency_id", "")

                    if existing_emergency_id:
                        log.info(
                            "INTAKE_DUPLICATE_DETECTED",
                            f"Merging caller into existing incident (score={best_score:.3f})",
                            existing_emergency_id=existing_emergency_id,
                            score=best_score,
                            caller_id=caller_id,
                        )
                        duplicate_emergency_id = existing_emergency_id
                        emit_business_metric("DuplicateIncidentMerge", 1, "Count",
                                             score=str(round(best_score, 2)))

                        # Append this caller's connection to the existing incident
                        dynamo_table = dynamo.Table(EMERGENCIES_TABLE)
                        dynamo_table.update_item(
                            Key={"emergency_id": existing_emergency_id},
                            UpdateExpression=(
                                "SET additional_caller_ids = list_append("
                                "  if_not_exists(additional_caller_ids, :empty_list), :cid"
                                "), "
                                "additional_connections = list_append("
                                "  if_not_exists(additional_connections, :empty_list), :conn"
                                "), "
                                "updated_at = :now"
                            ),
                            ExpressionAttributeValues={
                                ":empty_list": [],
                                ":cid":        [caller_id],
                                ":conn":       [connection_id] if connection_id else [],
                                ":now":        datetime.now(timezone.utc).isoformat(),
                            },
                        )

                        # Acknowledge this caller pointing to the existing incident
                        if connection_id:
                            _acknowledge_caller(connection_id, existing_emergency_id, caller_id)
                        return  # Do NOT start a new Step Functions execution

            except Exception as e:
                log.warn("INTAKE_DEDUP_FAILED", "Deduplication check failed (non-fatal)", exc=e)

    # -----------------------------------------------------------------------
    # Step 4: Create EmergencyRecord in DynamoDB
    # -----------------------------------------------------------------------
    now_utc = datetime.now(timezone.utc)
    ttl_epoch = int(now_utc.timestamp()) + (72 * 3600)  # 72-hour TTL

    emergency_item = {
        "emergency_id":          emergency_id,
        "version":               1,
        "status":                "PENDING_INTAKE",
        "emergency_type":        "UNKNOWN",
        "caller_id":             caller_id,
        "caller_connection_id":  connection_id,
        "caller_communication_mode": payload.get("communication_mode", "TEXT"),
        "preferred_language":    detected_language,
        "raw_description":       text_content,
        "translated_description": translated_description,
        "media_s3_keys":         image_s3_keys,
        "embedding_ids":         embedding_ids,
        "severity_score":        0,  # Will be populated by Triage Agent
        "has_text_embedding":     bool(text_embedding),  # True if dedup index is populated
        "created_at_epoch":      int(now_utc.timestamp()),
        "created_at":            now_utc.isoformat(),
        "updated_at":            now_utc.isoformat(),
        "ttl_epoch":             ttl_epoch,
        # Raw location data from device
        "location_raw":          json.dumps({
            "address_raw":   payload.get("location", ""),
            "latitude":      payload.get("latitude"),
            "longitude":     payload.get("longitude"),
        }),
    }

    dynamo_table = dynamo.Table(EMERGENCIES_TABLE)

    with timed_operation("dynamodb_write", slo_budget_ms=100):
        dynamo_table.put_item(Item=emergency_item)

    log.info("INTAKE_RECORD_CREATED", "Emergency record created in DynamoDB",
             emergency_id=emergency_id, version=1)
    emit_business_metric("EmergencyIntakeCount", 1, "Count",
                         emergency_type="UNKNOWN", language=detected_language)

    # Index this incident's embedding into OpenSearch so future callers can
    # deduplicate against it. Fire-and-forget — a write failure is non-fatal.
    if text_embedding:
        def _index_incident_embedding():
            try:
                from opensearch_client import upsert_embedding_document
                upsert_embedding_document(
                    index     = os.environ.get("OPENSEARCH_INCIDENT_INDEX", "historical-incidents"),
                    doc_id    = f"{emergency_id}-text",
                    embedding = text_embedding,
                    metadata  = {
                        "emergency_id":  emergency_id,
                        "status":        "PENDING_INTAKE",
                        "text_context":  description_for_embed[:500],
                        "created_at":    now_utc.isoformat(),
                        "caller_id":     caller_id,
                    },
                )
            except Exception as ex:
                log.warn("INTAKE_INDEX_FAILED", "OpenSearch incident index write failed", exc=ex)

        import threading
        threading.Thread(target=_index_incident_embedding, daemon=True).start()

    # -----------------------------------------------------------------------
    # Step 5: Start Step Functions Express Workflow
    # -----------------------------------------------------------------------
    workflow_input = {
        "emergency_id":          emergency_id,
        "caller_id":             caller_id,
        "caller_connection_id":  connection_id,
        "raw_description":       translated_description or text_content,
        "original_description":  text_content,
        "detected_language":     detected_language,
        "location_raw":          payload.get("location", ""),
        "latitude":              payload.get("latitude"),
        "longitude":             payload.get("longitude"),
        "media_s3_keys":         image_s3_keys,
        "embedding_ids":         embedding_ids,
        "communication_mode":    payload.get("communication_mode", "TEXT"),
        "intake_timestamp":      now_utc.isoformat(),
        "correlation_id":        correlation_id,
    }

    with timed_operation("sfn_start_execution", slo_budget_ms=300):
        try:
            sfn_response = sfn_client.start_execution(
                stateMachineArn=_get_state_machine_arn(),
                name=f"emergency-{emergency_id}",   # Name uniqueness enforced by emergency UUID
                input=json.dumps(workflow_input),
                traceHeader=correlation_id,
            )
            execution_arn = sfn_response["executionArn"]
            log.info("INTAKE_WORKFLOW_STARTED", "Step Functions execution started",
                     execution_arn=execution_arn, emergency_id=emergency_id)
        except ClientError as e:
            # ExecutionAlreadyExists: duplicate Kinesis delivery — safe to ignore
            if e.response["Error"]["Code"] == "ExecutionAlreadyExists":
                log.warn("INTAKE_DUPLICATE_EXECUTION", "Duplicate execution ID — ignoring",
                         emergency_id=emergency_id)
                return
            raise  # Other SFN errors should trigger Kinesis retry

    # -----------------------------------------------------------------------
    # Step 6: Acknowledge receipt to caller via WebSocket
    # -----------------------------------------------------------------------
    if connection_id:
        _acknowledge_caller(connection_id, emergency_id, caller_id)


def _acknowledge_caller(connection_id: str, emergency_id: str, caller_id: str) -> None:
    """
    Send immediate acknowledgment to the caller WebSocket client.
    This confirms receipt before triage completes (happens in <500ms).
    """
    try:
        ws_endpoint_param = os.environ.get("WS_ENDPOINT_PARAM", "/novaguard/ws-endpoint")
        ws_endpoint = ssm_client.get_parameter(Name=ws_endpoint_param)["Parameter"]["Value"]

        client = _get_apigw_client(ws_endpoint)

        acknowledgment = {
            "event":        "emergency_received",
            "emergency_id": emergency_id,
            "status":       "PENDING_INTAKE",
            "message":      "Your emergency report has been received. Help is being coordinated.",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }

        client.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(acknowledgment).encode("utf-8"),
        )
        log.info("INTAKE_ACK_SENT", "Acknowledgment sent to caller",
                 connection_id=connection_id, emergency_id=emergency_id)
    except Exception as e:
        # Stale connection — do not fail the intake
        log.warn("INTAKE_ACK_FAILED", "Could not send acknowledgment to caller (stale connection?)",
                 exc=e, connection_id=connection_id)
