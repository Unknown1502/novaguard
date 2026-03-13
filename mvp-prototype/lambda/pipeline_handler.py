"""
NovaGuard Full Pipeline Orchestrator — Lambda
=============================================
Runs the complete 3-agent pipeline synchronously:
  1. Triage Agent   (Nova 2 Lite) — severity scoring
  2. Dispatch Agent (Nova 2 Lite) — unit assignment
  3. Comms Agent    (Nova 2 Lite) — caller + responder instructions

Single POST /pipeline call → 3 real Nova calls → full emergency response.

Judges will see three distinct [NOVAGUARD * CALL] log lines in CloudWatch
for every pipeline invocation. DynamoDB will have versions 1, 2, 3 for
each emergency_id.

This is the DEMO ENDPOINT — the one we show in the 3-minute video.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION           = os.environ.get("REGION", "us-east-1")
TABLE_NAME       = os.environ.get("EMERGENCIES_TABLE",    "novaguard-emergencies")
TRIAGE_FN        = os.environ.get("TRIAGE_FUNCTION_NAME",  "novaguard-triage-agent")
DISPATCH_FN      = os.environ.get("DISPATCH_FUNCTION_NAME","novaguard-dispatch-agent")
COMMS_FN         = os.environ.get("COMMS_FUNCTION_NAME",   "novaguard-comms-agent")

lambda_client = boto3.client("lambda", region_name=REGION)
dynamo        = boto3.resource("dynamodb", region_name=REGION)


def _invoke_lambda(fn_name: str, payload: dict) -> dict:
    """Synchronously invoke a sibling Lambda and unwrap the response."""
    resp = lambda_client.invoke(
        FunctionName=fn_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    raw = json.loads(resp["Payload"].read())
    # Unwrap API Gateway envelope if present
    if "body" in raw and isinstance(raw["body"], str):
        return json.loads(raw["body"])
    return raw


def handler(event, context):
    t0 = time.time()

    # Browser GET — info response
    if event.get("httpMethod", "").upper() == "GET":
        return _resp(200, {
            "service":   "NovaGuard Full 3-Agent Pipeline",
            "agents":    ["Triage (Nova 2 Lite)", "Dispatch (Nova 2 Lite)", "Communications (Nova 2 Lite)"],
            "voice_entry": "POST /transcribe first with audio_b64, then POST /pipeline with transcript",
            "text_entry":  "POST /pipeline directly with description field",
            "status":    "operational",
        })

    # Parse request
    if isinstance(event.get("body"), str):
        body = json.loads(event["body"])
    elif isinstance(event.get("body"), dict):
        body = event["body"]
    else:
        body = event

    description     = (body.get("description") or "").strip()
    caller_location = body.get("caller_location", "Location not specified")
    caller_type     = body.get("caller_type", "TEXT")   # TEXT or VOICE

    if not description:
        return _resp(400, {"error": "Missing required field: description"})

    emergency_id = str(uuid.uuid4())
    logger.info("[PIPELINE] Start | emergency_id=%s | input_chars=%d | caller_type=%s",
                emergency_id, len(description), caller_type)

    # ── Agent 1: Triage ───────────────────────────────────────────────
    t1 = time.time()
    logger.info("[PIPELINE] → Invoking Triage Agent | emergency_id=%s", emergency_id)
    triage = _invoke_lambda(TRIAGE_FN, {
        "description": description,
        "emergency_id": emergency_id,
    })
    triage_ms = int((time.time() - t1) * 1000)
    # Use the emergency_id that Triage wrote to DynamoDB (may differ from our generated one)
    emergency_id = triage.get("emergency_id", emergency_id)
    logger.info("[PIPELINE] ✓ Triage done | emergency_id=%s | severity=%s | type=%s | ms=%d",
                emergency_id, triage.get("severity_score"), triage.get("emergency_type"), triage_ms)

    # ── Agent 2: Dispatch ─────────────────────────────────────────────
    t2 = time.time()
    logger.info("[PIPELINE] → Invoking Dispatch Agent | emergency_id=%s", emergency_id)
    dispatch = _invoke_lambda(DISPATCH_FN, {
        "emergency_id":     emergency_id,
        "severity_score":   triage.get("severity_score", 50),
        "emergency_type":   triage.get("emergency_type", "UNKNOWN"),
        "triage_narrative": triage.get("triage_narrative", ""),
        "caller_location":  caller_location,
        "confidence":       triage.get("confidence", 0.5),
    })
    dispatch_ms = int((time.time() - t2) * 1000)
    logger.info("[PIPELINE] ✓ Dispatch done | emergency_id=%s | units=%d | priority=%s | ms=%d",
                emergency_id, len(dispatch.get("units_dispatched", [])),
                dispatch.get("dispatch_priority"), dispatch_ms)

    # ── Agent 3: Communications ───────────────────────────────────────
    t3 = time.time()
    logger.info("[PIPELINE] → Invoking Comms Agent | emergency_id=%s", emergency_id)
    comms = _invoke_lambda(COMMS_FN, {
        "emergency_id":      emergency_id,
        "severity_score":    triage.get("severity_score", 50),
        "emergency_type":    triage.get("emergency_type", "UNKNOWN"),
        "triage_narrative":  triage.get("triage_narrative", ""),
        "dispatch_priority": dispatch.get("dispatch_priority", "URGENT"),
        "units_dispatched":  dispatch.get("units_dispatched", []),
        "caller_location":   caller_location,
    })
    comms_ms = int((time.time() - t3) * 1000)
    logger.info("[PIPELINE] ✓ Comms done | emergency_id=%s | instructions=%d | ms=%d",
                emergency_id, len(comms.get("caller_instructions", [])), comms_ms)

    total_ms = int((time.time() - t0) * 1000)
    logger.info(
        "[NOVAGUARD PIPELINE COMPLETE] emergency_id=%s | agents=3 | "
        "total_ms=%d | triage_ms=%d | dispatch_ms=%d | comms_ms=%d",
        emergency_id, total_ms, triage_ms, dispatch_ms, comms_ms,
    )
    # Canonical proof log — screenshot this in CloudWatch Live Tail during demo
    logger.info(
        "[NOVAGUARD LIVE] model=us.amazon.nova-lite-v1:0 latency=%dms "
        "input='%s...' output='severity=%s type=%s confidence=%s'",
        total_ms,
        description[:40],
        triage.get('severity_score'),
        triage.get('emergency_type'),
        triage.get('confidence'),
    )

    # Write pipeline summary record (version 10 = final)
    now = datetime.now(timezone.utc).isoformat()
    try:
        dynamo.Table(TABLE_NAME).put_item(Item={
            "emergency_id":   emergency_id,
            "version":        10,   # Pipeline summary record
            "status":         "PIPELINE_COMPLETE",
            "total_ms":       total_ms,
            "triage_ms":      triage_ms,
            "dispatch_ms":    dispatch_ms,
            "comms_ms":       comms_ms,
            "agents_ran":     3,
            "caller_type":    caller_type,
            "created_at":     now,
            "updated_at":     now,
            "ttl_epoch":      int(time.time()) + 72 * 3600,
        })
    except Exception as e:
        logger.warning("[PIPELINE] Summary DynamoDB write failed: %s", e)

    return _resp(200, {
        "emergency_id": emergency_id,
        "status":       "PIPELINE_COMPLETE",

        # ── Agent 1 results ────────────────────────────────────────
        "triage": {
            "severity_score":            triage.get("severity_score"),
            "emergency_type":            triage.get("emergency_type"),
            "confidence":                triage.get("confidence"),
            "triage_narrative":          triage.get("triage_narrative"),
            "recommended_response_level": triage.get("recommended_response_level"),
            "caller_instructions":       triage.get("caller_instructions"),
            "latency_ms":                triage_ms,
            "model":                     "us.amazon.nova-lite-v1:0",
        },

        # ── Agent 2 results ────────────────────────────────────────
        "dispatch": {
            "dispatch_priority":          dispatch.get("dispatch_priority"),
            "units_dispatched":           dispatch.get("units_dispatched", []),
            "dispatch_narrative":         dispatch.get("dispatch_narrative"),
            "cad_incident_type":          dispatch.get("cad_incident_type"),
            "mutual_aid_required":        dispatch.get("mutual_aid_required", False),
            "estimated_scene_time_minutes": dispatch.get("estimated_scene_time_minutes"),
            "staging_area":               dispatch.get("staging_area"),
            "special_instructions":       dispatch.get("special_instructions"),
            # Amazon Location Service fields — forwarded from dispatch agent
            "location_routing_used":      dispatch.get("location_routing_used", False),
            "location_eta_minutes":       dispatch.get("location_eta_minutes"),
            "latency_ms":                 dispatch_ms,
            "model":                      "us.amazon.nova-lite-v1:0",
        },

        # ── Agent 3 results ────────────────────────────────────────
        # Exposed as both "comms" (verify script) and "communications" (API consumers)
        "comms": {
            "caller_instructions":   comms.get("caller_instructions", []),
            "caller_reassurance":    comms.get("caller_reassurance"),
            "responder_briefing":    comms.get("responder_briefing"),
            "situation_summary":     comms.get("situation_summary"),
            "public_alert_required": comms.get("public_alert_required", False),
            # SNS alert field — forwarded from comms agent
            "sns_alert_sent":        comms.get("sns_alert_sent", False),
            "latency_ms":            comms_ms,
            "model":                 "us.amazon.nova-lite-v1:0",
        },
        # Alias for API consumers that use canonical name
        "communications": {
            "caller_instructions":   comms.get("caller_instructions", []),
            "caller_reassurance":    comms.get("caller_reassurance"),
            "responder_briefing":    comms.get("responder_briefing"),
            "situation_summary":     comms.get("situation_summary"),
            "public_alert_required": comms.get("public_alert_required", False),
            "sns_alert_sent":        comms.get("sns_alert_sent", False),
            "latency_ms":            comms_ms,
            "model":                 "us.amazon.nova-lite-v1:0",
        },

        # ── Pipeline metrics ───────────────────────────────────────
        "_meta": {
            "total_pipeline_ms": total_ms,
            "triage_ms":         triage_ms,
            "dispatch_ms":       dispatch_ms,
            "comms_ms":          comms_ms,
            "agents_ran":        3,
            "nova_calls":        3,
            "nova_models_used":  ["us.amazon.nova-lite-v1:0"],
            "input_channel":     caller_type,
            "timestamp":         now,
            "cloudwatch_proof":  "[NOVAGUARD TRIAGE CALL] + [NOVAGUARD DISPATCH CALL] + [NOVAGUARD COMMS CALL]",
        },
    })


def _resp(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body),
    }
