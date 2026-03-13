"""
NovaGuard Communications Agent — Lambda
========================================
Step 3 in the 3-agent pipeline.
Receives triage + dispatch results, calls Nova 2 Lite to generate:
  1. CALLER INSTRUCTIONS — plain-language, text-only (for deaf/HoH callers)
  2. RESPONDER BRIEFING  — radio-ready for incoming units
  3. SITUATION SUMMARY   — one-line CAD log entry

The caller instructions are specifically written for deaf/HoH users
(no "listen for sirens", text-first communication guidance).

Every invocation produces a [NOVAGUARD COMMS CALL] CloudWatch log line.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION     = os.environ.get("REGION", "us-east-1")
MODEL_ID   = os.environ.get("NOVA_LITE_MODEL_ID", "us.amazon.nova-lite-v1:0")
TABLE_NAME = os.environ.get("EMERGENCIES_TABLE",  "novaguard-emergencies")

bedrock         = boto3.client("bedrock-runtime", region_name=REGION)
dynamo          = boto3.resource("dynamodb",       region_name=REGION)
sns             = boto3.client("sns",              region_name=REGION)
SNS_TOPIC_ARN   = os.environ.get("DISPATCHER_SNS_TOPIC", "")

COMMS_SYSTEM_PROMPT = """You are NovaGuard's AI Communications Agent — expert in emergency communication.

You generate three outputs from a triage + dispatch result:

1. CALLER_INSTRUCTIONS (for deaf/hard-of-hearing callers — TEXT ONLY):
   - Assume the caller CANNOT hear — never say "listen for sirens" or "you'll hear the truck"
   - Use visual and physical cues: "Watch for flashing lights", "Feel the vibration as units arrive"
   - Short, numbered steps — max 4 steps
   - Calm, clear, plain English (Grade 8 reading level)
   - End with reassurance and an ETA

2. RESPONDER_BRIEFING (professional radio language for incoming units):
   - Lead with incident type and severity
   - Caller communication method: DEAF/HOH — TEXT ONLY, no voice
   - Special hazards if any
   - Max 3 sentences

3. SITUATION_SUMMARY (one line for the CAD incident log)

Return ONLY valid JSON — no markdown, no explanation:
{
  "caller_instructions": [
    "1. Stay where you are and keep your phone visible and unlocked.",
    "2. If you can, stand near a window or at the door so responders can see you.",
    "3. Watch for flashing red and blue lights — units will arrive in approximately X minutes.",
    "4. You will receive text updates from NovaGuard as responders get closer."
  ],
  "caller_reassurance": "Help is on the way — you did the right thing contacting us.",
  "responder_briefing": "Units responding to CARDIAC_ARREST. Caller is deaf/HoH — text communication only, no voice contact. Patient is 68yo male, unresponsive.",
  "situation_summary": "68yo male unresponsive, cardiac arrest suspected — 2 units dispatched IMMEDIATE priority.",
  "estimated_resolution_minutes": 45,
  "public_alert_required": false
}"""


def handler(event, context):
    t0 = time.time()

    # Browser GET — info
    if event.get("httpMethod", "").upper() == "GET":
        return _api_resp(200, {
            "service": "NovaGuard Communications Agent",
            "model":   MODEL_ID,
            "status":  "operational",
            "usage":   "POST { emergency_id, emergency_type, triage_narrative, dispatch_priority, units_dispatched }",
        })

    if isinstance(event.get("body"), str):
        body = json.loads(event["body"])
    elif isinstance(event.get("body"), dict):
        body = event["body"]
    else:
        body = event

    emergency_id      = body.get("emergency_id", "unknown")
    severity_score    = int(body.get("severity_score", 50))
    emergency_type    = body.get("emergency_type", "UNKNOWN")
    triage_narrative  = body.get("triage_narrative", "Emergency reported.")
    dispatch_priority = body.get("dispatch_priority", "URGENT")
    units             = body.get("units_dispatched", [])
    caller_location   = body.get("caller_location", "Location not specified")

    logger.info("[COMMS] Start | emergency_id=%s | type=%s | priority=%s",
                emergency_id, emergency_type, dispatch_priority)

    # Build units summary
    if units:
        units_str = "; ".join(
            f"{u.get('unit_id','?')} {u.get('unit_type','?')} ETA {u.get('eta_minutes','?')}min"
            for u in units
        )
        min_eta = min((u.get("eta_minutes", 99) for u in units), default=10)
    else:
        units_str = "Units en route"
        min_eta = 10

    comms_prompt = (
        f"INCIDENT COMMUNICATIONS REQUEST\n"
        f"Emergency ID:     {emergency_id}\n"
        f"Type:             {emergency_type}  |  Severity: {severity_score}/100\n"
        f"Dispatch Priority: {dispatch_priority}\n"
        f"Units Dispatched: {units_str}\n"
        f"Minimum ETA:      {min_eta} minutes\n"
        f"Location:         {caller_location}\n"
        f"Triage Narrative: {triage_narrative}\n"
        f"Caller Type:      DEAF/HOH — TEXT ONLY, no voice communication\n\n"
        f"Generate caller instructions (deaf-safe, text-only), responder briefing, and CAD summary."
    )

    try:
        response = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": COMMS_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": comms_prompt}]}],
            inferenceConfig={"maxTokens": 600, "temperature": 0.2, "topP": 0.9},
        )
    except ClientError as e:
        logger.error("[COMMS] Bedrock error: %s", e)
        result = _fallback_comms(emergency_id, emergency_type, units, min_eta)
        return _wrap(event, result)

    latency_ms  = int((time.time() - t0) * 1000)
    output_text = response["output"]["message"]["content"][0]["text"]
    usage       = response["usage"]

    logger.info(
        "[NOVAGUARD COMMS CALL] Nova 2 Lite comms | emergency_id=%s | "
        "in=%d out=%d | latency=%dms",
        emergency_id, usage.get("inputTokens", 0), usage.get("outputTokens", 0), latency_ms,
    )

    cleaned = output_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").lstrip("json").strip()
    try:
        comms = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("[COMMS] Non-JSON from Nova: %s", output_text[:200])
        comms = {
            "caller_instructions": [output_text],
            "caller_reassurance":  "Help is on the way.",
            "responder_briefing":  f"{emergency_type} response.",
            "situation_summary":   f"{emergency_type} severity {severity_score}.",
        }

    now = datetime.now(timezone.utc).isoformat()
    try:
        dynamo.Table(TABLE_NAME).put_item(Item={
            "emergency_id":         emergency_id,
            "version":              3,
            "status":               "COMMUNICATIONS_SENT",
            "caller_instructions":  json.dumps(comms.get("caller_instructions", [])),
            "caller_reassurance":   comms.get("caller_reassurance", ""),
            "responder_briefing":   comms.get("responder_briefing", ""),
            "situation_summary":    comms.get("situation_summary", ""),
            "public_alert_required": comms.get("public_alert_required", False),
            "nova_model":           MODEL_ID,
            "nova_latency_ms":      latency_ms,
            "nova_input_tokens":    usage.get("inputTokens", 0),
            "nova_output_tokens":   usage.get("outputTokens", 0),
            "created_at":           now,
            "updated_at":           now,
            "ttl_epoch":            int(time.time()) + 72 * 3600,
        })
        db_status = "written"
    except Exception as e:
        logger.error("[COMMS] DynamoDB failed: %s", e)
        db_status = f"failed: {e}"

    # ── SNS: send dispatcher alert ────────────────────────────────────
    sns_status = "skipped"
    if SNS_TOPIC_ARN:
        try:
            briefing_text = comms.get("responder_briefing", "Emergency response required.")
            summary_text  = comms.get("situation_summary", "")
            sns_msg = (
                f"[NovaGuard Alert] Emergency ID: {emergency_id}\n"
                f"Type: {emergency_type} | Priority: {dispatch_priority}\n"
                f"Summary: {summary_text}\n"
                f"Briefing: {briefing_text}\n"
                f"Units: {units_str}"
            )
            sns.publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject=f"NovaGuard {dispatch_priority} — {emergency_type} (ID:{emergency_id})",
                Message=sns_msg,
            )
            sns_status = "published"
            logger.info("[NOVAGUARD SNS] Alert published | emergency_id=%s | topic=%s",
                        emergency_id, SNS_TOPIC_ARN)
        except Exception as sns_err:
            logger.warning("[NOVAGUARD SNS] Publish failed (non-fatal): %s", sns_err)
            sns_status = f"failed: {sns_err}"
    else:
        logger.info("[NOVAGUARD SNS] DISPATCHER_SNS_TOPIC not set — skipping SNS publish")

    result = {
        "emergency_id":         emergency_id,
        "status":               "COMPLETE",
        "caller_instructions":  comms.get("caller_instructions", []),
        "caller_reassurance":   comms.get("caller_reassurance", ""),
        "responder_briefing":   comms.get("responder_briefing", ""),
        "situation_summary":    comms.get("situation_summary", ""),
        "estimated_resolution_minutes": comms.get("estimated_resolution_minutes", 30),
        "public_alert_required": comms.get("public_alert_required", False),
        "sns_alert_sent":        sns_status == "published",
        "_meta": {
            "latency_ms":    latency_ms,
            "model":         MODEL_ID,
            "input_tokens":  usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
            "db_status":     db_status,
            "sns_status":     sns_status,
            "timestamp":     now,
        },
    }
    return _wrap(event, result)


def _fallback_comms(emergency_id: str, etype: str, units: list, eta: int) -> dict:
    return {
        "emergency_id": emergency_id,
        "status": "COMPLETE_FALLBACK",
        "caller_instructions": [
            f"1. Stay calm — your emergency report was received.",
            f"2. Stay at your current safe location.",
            f"3. Watch for flashing emergency lights — help arrives in approximately {eta} minutes.",
            "4. Keep your phone screen on to receive text updates.",
        ],
        "caller_reassurance": "You did the right thing. Help is on the way.",
        "responder_briefing":  f"Units responding to {etype}. Caller is deaf/HoH — text-only channel.",
        "situation_summary":   f"{etype} — units dispatched.",
        "_meta": {"fallback": True},
    }


def _wrap(event: dict, result: dict) -> dict:
    if "httpMethod" in event or "requestContext" in event:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json",
                        "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(result),
        }
    return result


def _api_resp(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }
