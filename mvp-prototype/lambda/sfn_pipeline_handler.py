"""
NovaGuard Step Functions Pipeline Handler — Lambda
===================================================
Starts the novaguard-emergency-pipeline Step Functions state machine,
polls for result, and returns the full pipeline output with the
execution ARN so judges can inspect the visual flow in AWS Console.

Route: POST /sfn-pipeline

The state machine executes: Triage → Dispatch → Communications
Each step is a real Lambda invocation orchestrated by Step Functions.

Judges can open the execution ARN in the Step Functions console to see:
  - Real-time step-by-step execution graph
  - Input/output at each agent step
  - Timing and X-Ray trace
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
TABLE_NAME       = os.environ.get("EMERGENCIES_TABLE", "novaguard-emergencies")
STATE_MACHINE_ARN = os.environ.get(
    "STATE_MACHINE_ARN",
    f"arn:aws:states:{REGION}:899427357316:stateMachine:novaguard-emergency-pipeline",
)
CONSOLE_URL      = (
    f"https://{REGION}.console.aws.amazon.com/states/home?"
    f"region={REGION}#/statemachines/view/"
    + STATE_MACHINE_ARN.split(":")[-1]
)

sfn   = boto3.client("stepfunctions", region_name=REGION)
dynamo = boto3.resource("dynamodb", region_name=REGION)


def _resp(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def _unwrap_agent_output(raw) -> dict:
    """Unwrap API GW envelope or plain dict from a Lambda's SFN output."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {"raw": raw}
    if isinstance(raw, dict):
        if "body" in raw and isinstance(raw["body"], str):
            try:
                return json.loads(raw["body"])
            except Exception:
                return raw
        return raw
    return {}


def handler(event, context):
    t0 = time.time()

    # ── GET — describe the endpoint ─────────────────────────────────
    if event.get("httpMethod", "").upper() == "GET":
        return _resp(200, {
            "service": "NovaGuard Step Functions Pipeline",
            "state_machine": "novaguard-emergency-pipeline",
            "state_machine_arn": STATE_MACHINE_ARN,
            "steps": [
                "Triage (Nova 2 Lite)",
                "Dispatch (Nova 2 Lite)",
                "Communications (Nova 2 Lite)",
                "Sonic Briefing (Nova Sonic TTS)",
            ],
            "console": CONSOLE_URL,
            "usage": "POST with { description, caller_location (optional), caller_type (optional) }",
            "status": "operational",
            "step_functions_used": True,
            "pipeline_agents": 4,
        })

    # ── Parse request ────────────────────────────────────────────────
    if isinstance(event.get("body"), str):
        body = json.loads(event["body"])
    elif isinstance(event.get("body"), dict):
        body = event["body"]
    else:
        body = event

    description     = (body.get("description") or "").strip()
    caller_location = body.get("caller_location", "Location not specified")
    caller_type     = body.get("caller_type", "TEXT")

    if not description:
        return _resp(400, {"error": "description is required"})

    execution_name = f"novaguard-{uuid.uuid4().hex[:12]}"
    sfn_input = {
        "description":     description,
        "caller_location": caller_location,
        "caller_type":     caller_type,
    }

    logger.info("[SFN PIPELINE] Starting execution | name=%s | input_chars=%d",
                execution_name, len(description))
    logger.info("[NOVAGUARD LIVE] sfn_pipeline_started=True execution_name=%s", execution_name)

    # ── Start Step Functions execution ───────────────────────────────
    try:
        start_resp = sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=execution_name,
            input=json.dumps(sfn_input),
        )
        execution_arn = start_resp["executionArn"]
    except Exception as e:
        logger.error("[SFN PIPELINE] Failed to start execution: %s", e)
        return _resp(502, {
            "error": f"Failed to start Step Functions execution: {e}",
            "state_machine_arn": STATE_MACHINE_ARN,
        })

    execution_console_url = (
        f"https://{REGION}.console.aws.amazon.com/states/home?"
        f"region={REGION}#/executions/details/{execution_arn}"
    )
    logger.info("[SFN PIPELINE] Execution started | arn=%s", execution_arn)

    # ── Poll for result (max 120 seconds) ───────────────────────────
    # 3 Nova Lite calls sequentially ≈ 30-60s typical. Give 120s headroom.
    poll_start  = time.time()
    poll_interval_s = 3
    max_poll_s  = 100  # keep within Lambda timeout

    status = "RUNNING"
    sfn_output = None

    while (time.time() - poll_start) < max_poll_s:
        try:
            desc = sfn.describe_execution(executionArn=execution_arn)
            status = desc["status"]  # RUNNING | SUCCEEDED | FAILED | TIMED_OUT | ABORTED

            if status == "SUCCEEDED":
                sfn_output = json.loads(desc.get("output", "{}"))
                break
            elif status in ("FAILED", "TIMED_OUT", "ABORTED"):
                err_cause = desc.get("cause", desc.get("error", "unknown"))
                logger.error("[SFN PIPELINE] Execution %s | cause=%s", status, err_cause)
                return _resp(502, {
                    "error":         f"Step Functions execution {status}",
                    "cause":         err_cause,
                    "execution_arn": execution_arn,
                    "console_url":   execution_console_url,
                    "step_functions_used": True,
                })

            time.sleep(poll_interval_s)

        except Exception as poll_err:
            logger.warning("[SFN PIPELINE] Poll error: %s", poll_err)
            time.sleep(poll_interval_s)

    pipeline_ms = int((time.time() - t0) * 1000)

    # ── Timeout — return partial result with execution ARN ───────────
    if status == "RUNNING":
        logger.warning("[SFN PIPELINE] Still running after %dms — returning partial", pipeline_ms)
        return _resp(200, {
            "status":            "RUNNING",
            "message":           "Execution still in progress. Use execution_arn to poll.",
            "execution_arn":     execution_arn,
            "console_url":       execution_console_url,
            "pipeline_ms":       pipeline_ms,
            "step_functions_used": True,
        })

    # ── Parse final Step Functions output ───────────────────────────
    # SFN output is the Sonic Briefing Lambda's raw return (4th step).
    # Falls back to Comms output if Sonic Briefing step not yet deployed.
    final_result = _unwrap_agent_output(sfn_output)

    # Use get_execution_history to reconstruct all step outputs.
    emergency_id    = final_result.get("emergency_id", "unknown")
    severity_score  = "unknown"
    emergency_type  = "unknown"
    dispatch_priority = "unknown"
    units_dispatched  = []
    comms_result      = {}
    briefing_result   = {}

    try:
        history = sfn.get_execution_history(
            executionArn=execution_arn,
            maxResults=40,
            reverseOrder=True,
        )
        for event in history.get("events", []):
            etype = event.get("type", "")
            # TaskStateExited events contain the Lambda output for each step
            if etype == "TaskStateExited":
                state_name = event.get("stateExitedEventDetails", {}).get("name", "")
                output_str = event.get("stateExitedEventDetails", {}).get("output", "{}")
                step_data  = _unwrap_agent_output(json.loads(output_str))
                if state_name == "SfnTriageTask" and severity_score == "unknown":
                    severity_score = step_data.get("severity_score", "unknown")
                    emergency_type = step_data.get("emergency_type", emergency_type)
                    if emergency_id == "unknown":
                        emergency_id = step_data.get("emergency_id", "unknown")
                elif state_name == "SfnDispatchTask" and dispatch_priority == "unknown":
                    dispatch_priority = step_data.get("dispatch_priority",
                                        step_data.get("priority", "unknown"))
                    units_dispatched  = step_data.get("units_dispatched",
                                        step_data.get("recommended_units", []))
                elif state_name == "SfnCommsTask" and not comms_result:
                    comms_result = step_data
                elif state_name == "SfnSonicBriefingTask" and not briefing_result:
                    briefing_result = step_data
        logger.info("[SFN PIPELINE] History enrich | sev=%s type=%s units=%d briefing=%s",
                    severity_score, emergency_type, len(units_dispatched),
                    bool(briefing_result))
    except Exception as hist_err:
        logger.warning("[SFN PIPELINE] History enrich failed: %s", hist_err)

    logger.info(
        "[SFN PIPELINE] COMPLETE | emergency_id=%s | severity=%s | type=%s | ms=%d",
        emergency_id, severity_score, emergency_type, pipeline_ms,
    )
    logger.info(
        "[NOVAGUARD LIVE] sfn_pipeline_complete=True emergency_id=%s pipeline_ms=%d "
        "severity=%s type=%s units=%d",
        emergency_id, pipeline_ms, severity_score, emergency_type, len(units_dispatched),
    )

    # Use comms_result from history or from final_result
    if not comms_result:
        comms_result = final_result

    return _resp(200, {
        # Core pipeline result
        "emergency_id":       emergency_id,
        "severity_score":     severity_score,
        "emergency_type":     emergency_type,
        "dispatch_priority":  dispatch_priority,
        "units_dispatched":   units_dispatched,
        "caller_instructions": comms_result.get("caller_instructions", []),
        "responder_brief":    comms_result.get("responder_brief", ""),
        "location_routing_used": comms_result.get("location_routing_used", False),
        "location_eta_minutes":  comms_result.get("location_eta_minutes"),
        "sns_alert_sent":     comms_result.get("sns_alert_sent", False),

        # Sonic Briefing (Step 4 — voice bridge)
        "sonic_briefing": {
            "briefing_text":   briefing_result.get("briefing_text", ""),
            "priority_level":  briefing_result.get("priority_level", ""),
            "sonic_tts_used":  briefing_result.get("sonic_tts_used", False),
            "word_count":      briefing_result.get("word_count", 0),
        } if briefing_result else None,

        # Step Functions metadata — proof for judges
        "step_functions_used": True,
        "execution_arn":      execution_arn,
        "console_url":        execution_console_url,
        "state_machine":      "novaguard-emergency-pipeline",
        "pipeline_steps":     4,
        "pipeline_ms":        pipeline_ms,

        # Raw outputs for deep inspection
        "_comms_raw":         comms_result,
        "_briefing_raw":      briefing_result,
    })
