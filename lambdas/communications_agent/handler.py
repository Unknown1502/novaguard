"""
NovaGuard Communications Agent — Lambda Handler

Trigger: AWS Step Functions task (synchronous Lambda invoke)
Input:  Dispatch recommendation + emergency metadata
Output: CommunicationsPayload with caller message text and audio session ID

Responsibilities:
1. Determine best communication mode (VOICE / TEXT / ASL_VIDEO / TTY) from caller profile
2. For VOICE mode: initiate Nova 2 Sonic bidirectional audio stream via WebSocket
3. For TEXT mode: synthesize a clear status message and push via SNS → SMS
4. For ASL_VIDEO mode: route to human relay service ASL interpreter queue
5. Store audio chunks to S3 (for compliance/replay)
6. Update emergency status to CALLER_NOTIFIED
7. Multilingual translation via Nova 2 Lite when caller is non-English

Nova 2 Sonic usage (primary use case for this agent):
- Amazon Bedrock Nova Sonic model for real-time bidirectional voice communication
- Stream opened via InvokeModelWithBidirectionalStreamingAPI
- Caller voice = PCM 16kHz → Sonic receives audio → generates response audio
- Agent maintains conversation state: can answer "where is help?" queries in real time
- Conversation timeout = 300 seconds (5 minutes); caller can disconnect at any time
- The Sonic system prompt instructs it to: provide status updates, answer ETA queries,
  stay calm, and escalate to human if caller asks for a human operator

Accessibility notes:
- TTY/TDD support: text-mode message pushed to Twilio programmable messaging
- ASL: route to video relay interpreter queue (human in the loop)
- Non-verbal callers: use SMS/text channel exclusively
- Multilingual: Nova 2 Lite translates the dispatch narrative before voice synthesis

Architecture:
- This Lambda starts the Sonic stream and stores the stream session_id
- The WebSocket Lambda (ws_handler) proxies audio frames between client and Sonic
- This Lambda exits after session handoff (does NOT block on audio stream completion)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

import sys
sys.path.insert(0, "/opt/python")

from observability import log, set_correlation_context, timed_operation, annotate_xray_trace, emit_business_metric
from bedrock_client import BedrockClient, invoke_nova_lite_for_translation

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("POWERTOOLS_LOG_LEVEL", "INFO"))

# --- AWS Clients ---
dynamo      = boto3.resource("dynamodb", region_name=os.environ.get("REGION", "us-east-1"))
sns_client  = boto3.client("sns",        region_name=os.environ.get("REGION", "us-east-1"))
sqs_client  = boto3.client("sqs",        region_name=os.environ.get("REGION", "us-east-1"))
apigw_mgmt  = None  # initialized lazily on first use

EMERGENCIES_TABLE        = os.environ.get("EMERGENCIES_TABLE",        "novaguard-emergencies")
CONNECTIONS_TABLE        = os.environ.get("CONNECTIONS_TABLE",        "novaguard-connections")
CALLER_NOTIFICATIONS_SNS = os.environ.get("CALLER_NOTIFICATIONS_SNS", "")
ASL_RELAY_QUEUE_URL      = os.environ.get("ASL_RELAY_QUEUE_URL",      "")
AUDIO_CHUNKS_BUCKET      = os.environ.get("AUDIO_CHUNKS_BUCKET",      "novaguard-audio-chunks")
WS_ENDPOINT              = os.environ.get("WS_API_ENDPOINT",          "")
SONIC_MODEL_ID           = os.environ.get("NOVA_SONIC_MODEL_ID",      "amazon.nova-sonic-v1:0")

SUPPORTED_VOICE_LANGUAGES = {"en", "es", "fr", "de", "zh", "ar", "hi", "ja", "ko", "pt"}

SONIC_SYSTEM_PROMPT = """
You are a compassionate and calm emergency communications assistant for NovaGuard.
Your role is to provide real-time status updates to callers who have reported an emergency.

You have the following information about this emergency:
{emergency_context}

Guidelines:
- Stay calm, reassuring, and professional at all times.
- Proactively tell the caller that help is on the way with the estimated arrival time.
- If the caller asks for more details about responding units, you may share unit types and ETA.
- If the caller's situation changes (e.g., more injured, fire is spreading), acknowledge it
  and say that you are updating the dispatch team.
- If the caller asks to speak with a human operator, say: "I'm connecting you to a human
  dispatcher now. Please hold."  Then END THE CONVERSATION.
- Do NOT provide medical advice beyond "keep the person calm and still unless in danger."
- Do NOT share the names of first responders for privacy/security reasons.
- Speak clearly and at a moderate pace. Avoid jargon.
""".strip()


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Step Functions task handler for Communications.

    Input event structure:
      - emergency_id: str
      - triage_result: Dict
      - dispatch_recommendation: Dict (from DispatchAgent step output)
      - communication_mode: str  — VOICE | TEXT | ASL_VIDEO | TTY
      - detected_language: str
      - caller_phone: Optional[str]
      - caller_ws_connection_id: Optional[str]
      - correlation_id: str

    Returns CommunicationsPayload dict.
    """
    emergency_id    = event.get("emergency_id", "unknown")
    correlation_id  = event.get("correlation_id", context.aws_request_id)

    set_correlation_context(correlation_id, emergency_id)
    annotate_xray_trace(emergency_id)

    log.info(
        "COMMS_START",
        "Beginning caller communications",
        emergency_id=emergency_id,
        correlation_id=correlation_id,
    )

    overall_start_ms = int(time.time() * 1000)

    comm_mode      = event.get("communication_mode", "TEXT").upper()
    language       = event.get("detected_language", "en").lower()
    caller_phone   = event.get("caller_phone")
    ws_conn_id     = event.get("caller_ws_connection_id")
    triage_result  = event.get("triage_result", {})
    dispatch_rec   = event.get("dispatch_recommendation", {})

    # Build the caller-facing status message
    status_message = _build_status_message(triage_result, dispatch_rec)

    # Translate to caller's language if not English
    translated_message = status_message
    if language != "en" and language in SUPPORTED_VOICE_LANGUAGES:
        with timed_operation("nova_lite_translation", slo_budget_ms=500):
            translated_message = _translate_message(status_message, language) or status_message

    # -----------------------------------------------------------------------
    # Route by communication mode
    # -----------------------------------------------------------------------
    sonic_session_id: Optional[str] = None
    notification_id:  Optional[str] = None

    if comm_mode == "VOICE" and ws_conn_id:
        # Initiate Nova 2 Sonic streaming session
        with timed_operation("nova_sonic_session_init", slo_budget_ms=800):
            sonic_session_id = _init_sonic_stream_session(
                emergency_id=emergency_id,
                ws_connection_id=ws_conn_id,
                triage_result=triage_result,
                dispatch_rec=dispatch_rec,
                language=language,
            )

    elif comm_mode == "TEXT" and caller_phone:
        notification_id = _send_sms_notification(caller_phone, translated_message, emergency_id)

    elif comm_mode == "ASL_VIDEO":
        notification_id = _route_to_asl_interpreter(emergency_id, dispatch_rec)

    elif comm_mode == "TTY":
        # TTY uses SMS channel with reformatted message for screen readers
        if caller_phone:
            notification_id = _send_sms_notification(
                caller_phone,
                _format_tty_message(translated_message),
                emergency_id,
            )

    else:
        # Fallback: SMS if phone is available, otherwise just log
        if caller_phone:
            notification_id = _send_sms_notification(caller_phone, translated_message, emergency_id)
        log.info("COMMS_MODE_FALLBACK", "Using fallback text notification",
                 comm_mode=comm_mode, emergency_id=emergency_id)

    # -----------------------------------------------------------------------
    # Persist communications result
    # -----------------------------------------------------------------------
    total_duration_ms = int(time.time() * 1000) - overall_start_ms

    comms_payload = {
        "emergency_id":       emergency_id,
        "communication_mode": comm_mode,
        "language":           language,
        "status_message":     translated_message,
        "sonic_session_id":   sonic_session_id,
        "notification_id":    notification_id,
        "comms_timestamp":    datetime.now(timezone.utc).isoformat(),
        "duration_ms":        total_duration_ms,
    }

    _persist_comms_result(emergency_id, comms_payload)
    _update_emergency_status(emergency_id, "CALLER_NOTIFIED")

    emit_business_metric("CommunicationsLatencyMs", total_duration_ms, "Milliseconds",
                         comm_mode=comm_mode)
    emit_business_metric("CallerNotifications", 1, "Count", comm_mode=comm_mode)

    log.info(
        "COMMS_COMPLETE",
        "Caller communications delivered",
        emergency_id=emergency_id,
        comm_mode=comm_mode,
        sonic_session_id=sonic_session_id,
        total_duration_ms=total_duration_ms,
    )

    return comms_payload


def _build_status_message(triage_result: Dict, dispatch_rec: Dict) -> str:
    """Build a clear, direct English status message for the caller."""
    narrative   = dispatch_rec.get("dispatch_narrative", "")
    units       = dispatch_rec.get("assigned_units", [])
    soonest_eta = min((u.get("eta_seconds", 300) for u in units), default=300)
    eta_min     = max(1, int(soonest_eta / 60))
    emergency_type = triage_result.get("emergency_type", "Emergency")

    subject = emergency_type.replace("_", " ").title()

    if narrative:
        return f"Your {subject} report has been received. {narrative} Help will arrive in approximately {eta_min} minute(s). Please stay calm and remain on the line."
    else:
        return f"Your emergency has been received and help is on the way. Estimated arrival: {eta_min} minute(s). Please stay calm."


def _translate_message(message: str, target_language: str) -> Optional[str]:
    """
    Translate the status message to the caller's language using Nova 2 Lite.
    Returns translated text or None on failure (caller falls back to English).
    """
    try:
        return invoke_nova_lite_for_translation(
            text=message,
            target_language=target_language,
        )
    except Exception as e:
        log.warn("TRANSLATION_FAILED", f"Translation to '{target_language}' failed", exc=e)
        return None


def _init_sonic_stream_session(
    emergency_id:      str,
    ws_connection_id:  str,
    triage_result:     Dict,
    dispatch_rec:      Dict,
    language:          str,
) -> Optional[str]:
    """
    Record a Nova Sonic session intent in DynamoDB.

    The actual bidirectional stream is initiated in the WebSocket handler (ws_handler.py)
    when the client sends the first audio frame. This Lambda records the session parameters
    so the WebSocket handler knows how to configure Sonic.

    Returns a session_id for tracking.
    """
    import uuid
    session_id = f"sonic-{emergency_id}-{uuid.uuid4().hex[:8]}"

    emergency_context = (
        f"Emergency type: {triage_result.get('emergency_type', 'Unknown')}.\n"
        f"Severity score: {triage_result.get('severity_score', 50)}/100.\n"
        f"Dispatch: {dispatch_rec.get('dispatch_narrative', 'Units have been dispatched.')}\n"
        f"Caller instructions from triage: {triage_result.get('caller_instructions', '')}"
    )

    system_prompt = SONIC_SYSTEM_PROMPT.format(emergency_context=emergency_context)

    # Pre-seed text injected as Sonic's initial assistant turn — enables sub-500ms
    # first-audio-byte by having Sonic start generating speech before caller speaks.
    # Keep it concise: one reassuring sentence + ETA.
    units       = dispatch_rec.get("assigned_units", [])
    soonest_eta = min((u.get("eta_seconds", 300) for u in units), default=300)
    eta_min     = max(1, int(soonest_eta / 60))
    pre_seed_text = (
        f"Help is on the way. "
        f"{dispatch_rec.get('dispatch_narrative', 'Units have been dispatched.')}"
        f" Estimated arrival: {eta_min} minute{'s' if eta_min != 1 else ''}."
        f" Please stay calm and remain on the line."
    )

    try:
        table = dynamo.Table(CONNECTIONS_TABLE)
        table.put_item(Item={
            "connection_id":   ws_connection_id,
            "emergency_id":    emergency_id,
            "session_id":      session_id,
            "sonic_model_id":  SONIC_MODEL_ID,
            "system_prompt":   system_prompt,
            "pre_seed_text":   pre_seed_text,   # consumed by ws_handler for sub-500ms first audio byte
            "language":        language,
            "created_at":      datetime.now(timezone.utc).isoformat(),
            "status":          "PENDING_AUDIO",
            "ttl":             int(time.time()) + 3600,  # 1h TTL
        })

        log.info(
            "SONIC_SESSION_CREATED",
            "Nova Sonic session registered",
            session_id=session_id,
            ws_connection_id=ws_connection_id,
            emergency_id=emergency_id,
        )
    except Exception as e:
        log.error("SONIC_SESSION_PERSIST_FAILED", "Failed to persist Sonic session", exc=e)
        return None

    return session_id


def _send_sms_notification(
    phone_number:  str,
    message:       str,
    emergency_id:  str,
) -> Optional[str]:
    """Publish a caller status update via SNS → SMS."""
    if not CALLER_NOTIFICATIONS_SNS:
        log.warn("SMS_NO_SNS_ARN", "CALLER_NOTIFICATIONS_SNS not configured — SMS skipped")
        return None
    try:
        resp = sns_client.publish(
            TopicArn=CALLER_NOTIFICATIONS_SNS,
            Message=message,
            Subject="NovaGuard Emergency Update",
            MessageAttributes={
                "destination_phone": {
                    "DataType": "String",
                    "StringValue": phone_number,
                },
                "emergency_id": {
                    "DataType": "String",
                    "StringValue": emergency_id,
                },
            },
        )
        message_id = resp.get("MessageId")
        log.info("SMS_SENT", "SMS notification sent", message_id=message_id)
        return message_id
    except ClientError as e:
        log.error("SMS_FAILED", "SMS notification failed", exc=e)
        emit_business_metric("SmsNotificationFailures", 1, "Count")
        return None


def _route_to_asl_interpreter(emergency_id: str, dispatch_rec: Dict) -> Optional[str]:
    """Send emergency context to ASL video relay interpreter queue."""
    if not ASL_RELAY_QUEUE_URL:
        log.warn("ASL_NO_QUEUE", "ASL relay queue not configured")
        return None
    try:
        msg_body = json.dumps({
            "emergency_id":      emergency_id,
            "dispatch_narrative": dispatch_rec.get("dispatch_narrative", ""),
            "assigned_units":    dispatch_rec.get("assigned_units", []),
            "timestamp":         datetime.now(timezone.utc).isoformat(),
            "priority":          "URGENT",
        })
        resp = sqs_client.send_message(
            QueueUrl=ASL_RELAY_QUEUE_URL,
            MessageBody=msg_body,
            MessageAttributes={
                "emergency_id": {
                    "DataType": "String",
                    "StringValue": emergency_id,
                }
            },
        )
        return resp.get("MessageId")
    except ClientError as e:
        log.error("ASL_ROUTE_FAILED", "ASL relay routing failed", exc=e)
        return None


def _format_tty_message(message: str) -> str:
    """
    Format a message for TTY/TDD devices.
    TTY uses SHORT LINES, ALL CAPS optionally, and GA (Go Ahead) markers.
    """
    lines = [line.upper() for line in message.split(".") if line.strip()]
    return " GA ".join(l.strip() for l in lines) + " GA"


def _update_emergency_status(emergency_id: str, new_status: str) -> None:
    try:
        table = dynamo.Table(EMERGENCIES_TABLE)
        table.update_item(
            Key={"emergency_id": emergency_id, "version": 1},
            UpdateExpression="SET #s = :s, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": new_status,
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        log.warn("COMMS_STATUS_UPDATE_FAILED", "Could not update emergency status", exc=e)


def _persist_comms_result(emergency_id: str, comms_payload: Dict) -> None:
    try:
        table = dynamo.Table(EMERGENCIES_TABLE)
        table.put_item(Item={
            "emergency_id":   emergency_id,
            "version":        199,
            "status":         "CALLER_NOTIFIED",
            "comms_payload":  json.dumps(comms_payload),
            "updated_at":     datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        log.error("COMMS_PERSIST_FAILED", "Failed to persist communications result", exc=e)
        raise
