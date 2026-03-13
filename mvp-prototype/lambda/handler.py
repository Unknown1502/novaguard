"""
NovaGuard MVP Triage Lambda
===========================
Self-contained — no shared/ layer dependencies.
Receives an emergency description, calls Nova 2 Lite, writes result to DynamoDB.

Input event (from API Gateway):
  { "description": "My father fell and is not responding..." }

Output:
  {
    "emergency_id": "uuid",
    "severity_score": 95,
    "emergency_type": "MEDICAL",
    "confidence": 0.98,
    "triage_narrative": "...",
    "recommended_response_level": 5,
    "latency_ms": 1748
  }
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION        = os.environ.get("REGION", "us-east-1")
MODEL_ID      = os.environ.get("NOVA_LITE_MODEL_ID", "us.amazon.nova-lite-v1:0")
TABLE_NAME    = os.environ.get("EMERGENCIES_TABLE", "novaguard-emergencies")

bedrock    = boto3.client("bedrock-runtime", region_name=REGION)
dynamo     = boto3.resource("dynamodb",       region_name=REGION)
translate  = boto3.client("translate",        region_name=REGION)

SYSTEM_PROMPT = """You are the NovaGuard AI Triage Agent — an expert emergency medical dispatcher.

You receive emergency reports submitted via text by deaf or hard-of-hearing callers who cannot call 911 by voice.

Your job:
1. Assess the severity of the emergency (0-100 score)
2. Classify the emergency type
3. Estimate confidence in your assessment
4. Write a concise triage narrative for the dispatcher

SEVERITY SCORING RUBRIC:
90-100: Immediately life-threatening (cardiac arrest, unresponsive, active shooting, structural fire)
70-89:  Serious but stable (fractures, lacerations requiring stitches, mental health crisis)
50-69:  Urgent but not immediately life-threatening (moderate injuries, medical distress)
30-49:  Non-urgent medical (minor injuries, illness without danger signs)
0-29:   Non-emergency (general assistance, information requests)

EMERGENCY TYPES: MEDICAL, FIRE, POLICE, RESCUE, HAZMAT, MULTI_AGENCY

RESPONSE FORMAT — return ONLY valid JSON, no markdown, no explanation:
{
  "severity_score": <0-100 integer>,
  "emergency_type": "<type>",
  "confidence": <0.0-1.0 float>,
  "triage_narrative": "<1-2 sentence dispatcher-ready summary>",
  "caller_instructions": "<what to tell the caller to do right now>",
  "recommended_response_level": <1-5 integer where 5=maximum response>
}"""


def handler(event, context):
    """Lambda entry point — handles both direct invocation and API Gateway proxy."""
    t0 = time.time()

    # Browser-friendly: GET requests return API info instead of 403
    http_method = event.get("httpMethod", "").upper()
    if http_method == "GET":
        return _response(200, {
            "service": "NovaGuard Emergency Triage API",
            "model": MODEL_ID,
            "status": "operational",
            "usage": "POST to this endpoint with { \"description\": \"<emergency description>\" }",
            "example_curl": (
                "curl -X POST https://438f02a6qk.execute-api.us-east-1.amazonaws.com/prod/triage "
                "-H 'Content-Type: application/json' "
                "-d '{\"description\": \"My father is having a heart attack\"}'"
            ),
            "endpoints": {
                "POST /triage": "AI triage via Nova 2 Lite — returns severity score, type, narrative",
                "GET  /health": "liveness check",
            },
        })

    # Parse body from API Gateway or direct invocation
    if isinstance(event.get("body"), str):
        body = json.loads(event["body"])
    elif isinstance(event.get("body"), dict):
        body = event["body"]
    else:
        body = event

    description = (body.get("description") or "").strip()
    if not description:
        return _response(400, {"error": "Missing required field: description"})

    # ── Amazon Translate — auto-detect and translate to English ──────
    detected_language = "en"
    was_translated    = False
    original_description = description
    try:
        translate_result = translate.translate_text(
            Text=description[:500],
            SourceLanguageCode="auto",
            TargetLanguageCode="en",
        )
        detected_language = translate_result.get("SourceLanguageCode", "en")
        if detected_language != "en":
            description   = translate_result["TranslatedText"]
            was_translated = True
            logger.info(
                "[NOVAGUARD TRANSLATE] Detected=%s | was_translated=%s | original='%s' | translated='%s'",
                detected_language, was_translated, original_description[:60], description[:60],
            )
        else:
            logger.info("[NOVAGUARD TRANSLATE] Language=en | no translation required")
    except Exception as translate_err:
        logger.warning("[NOVAGUARD TRANSLATE] Failed (non-fatal): %s", translate_err)

    emergency_id = str(uuid.uuid4())
    logger.info("[NOVAGUARD] Triage started | emergency_id=%s | input_chars=%d", emergency_id, len(description))

    # ── Call Nova 2 Lite ──────────────────────────────────────────────
    try:
        bedrock_response = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{
                "role": "user",
                "content": [{
                    "text": (
                        f"EMERGENCY REPORT — NovaGuard Triage Request\n"
                        f"Submitted by: Deaf/HoH caller (text-only channel)\n"
                        f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n\n"
                        f"CALLER'S DESCRIPTION:\n{description}"
                    )
                }]
            }],
            inferenceConfig={
                "maxTokens": 512,
                "temperature": 0.0,   # Deterministic for emergency triage
                "topP": 1.0,
            },
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]  # type: ignore[index]
        logger.error("[NOVAGUARD] Bedrock error: %s — %s", code, e)
        if code in ("ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException"):
            return _response(429, {
                "error": f"Bedrock throttled: {code}",
                "detail": "Too many concurrent requests — retry in 2 seconds",
                "emergency_id": emergency_id,
                "retry_after_seconds": 2,
            })
        return _response(502, {
            "error": f"Bedrock error: {code}",
            "detail": str(e),
            "emergency_id": emergency_id,
        })

    latency_ms = int((time.time() - t0) * 1000)

    # ── Parse Nova response ───────────────────────────────────────────
    output_text = bedrock_response["output"]["message"]["content"][0]["text"]  # type: ignore[index]
    stop_reason = bedrock_response["stopReason"]  # type: ignore[index]
    usage       = bedrock_response["usage"]  # type: ignore[index]

    logger.info(
        "[NOVAGUARD REAL CALL] Nova 2 Lite invoked | emergency_id=%s | stop=%s | "
        "in=%d out=%d | latency=%dms",
        emergency_id, stop_reason,
        usage.get("inputTokens", 0), usage.get("outputTokens", 0),
        latency_ms,
    )

    # Strip markdown fences if Nova wrapped the JSON
    cleaned = output_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        triage_result = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("[NOVAGUARD] Non-JSON response from Nova: %s", output_text[:200])
        triage_result = {"raw_output": output_text}

    severity_score = triage_result.get("severity_score", 0)
    emergency_type = triage_result.get("emergency_type", "UNKNOWN")
    confidence     = triage_result.get("confidence", 0.0)

    logger.info(
        "[NOVAGUARD] Triage complete | emergency_id=%s | severity=%d | type=%s | confidence=%.2f",
        emergency_id, severity_score, emergency_type, confidence,
    )

    # ── Write to DynamoDB ─────────────────────────────────────────────
    now = datetime.now(timezone.utc).isoformat()
    try:
        table = dynamo.Table(TABLE_NAME)
        table.put_item(Item={
            "emergency_id":    emergency_id,
            "version":         1,
            "status":          "TRIAGED",
            "severity_score":  severity_score,
            "emergency_type":  emergency_type,
            "confidence":      str(confidence),
            "triage_narrative": triage_result.get("triage_narrative", ""),
            "caller_instructions": triage_result.get("caller_instructions", ""),
            "recommended_response_level": triage_result.get("recommended_response_level", 3),
            "input_description": description[:500],  # Truncate for storage
            "nova_model_id":   MODEL_ID,
            "nova_latency_ms": latency_ms,
            "nova_input_tokens":  usage.get("inputTokens", 0),
            "nova_output_tokens": usage.get("outputTokens", 0),
            "created_at":      now,
            "updated_at":      now,
            "ttl_epoch":       int(time.time()) + (72 * 3600),  # 72-hour TTL
        })
        logger.info("[NOVAGUARD] DynamoDB write OK | emergency_id=%s", emergency_id)
        db_status = "written"
    except Exception as e:
        logger.error("[NOVAGUARD] DynamoDB write failed: %s", e)
        db_status = f"failed: {e}"

    # ── Build response ────────────────────────────────────────────────
    result = {
        "emergency_id":    emergency_id,
        "status":          "TRIAGED",
        "severity_score":  severity_score,
        "emergency_type":  emergency_type,
        "confidence":      confidence,
        "triage_narrative": triage_result.get("triage_narrative", ""),
        "caller_instructions": triage_result.get("caller_instructions", ""),
        "recommended_response_level": triage_result.get("recommended_response_level", 3),
        "detected_language": detected_language,
        "was_translated":    was_translated,
        "_meta": {
            "latency_ms":     latency_ms,
            "model":          MODEL_ID,
            "region":         REGION,
            "input_tokens":   usage.get("inputTokens", 0),
            "output_tokens":  usage.get("outputTokens", 0),
            "stop_reason":    stop_reason,
            "db_status":      db_status,
            "timestamp":      now,
        }
    }

    return _response(200, result)


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body),
    }
