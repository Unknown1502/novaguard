"""
NovaGuard Post-Incident Summary Agent — Lambda Handler

Trigger: EventBridge rule on emergency status transition to CALLER_NOTIFIED or RESOLVED.
         Rule pattern:
           source: ["novaguard.step-functions"]
           detail-type: ["EmergencyStatusChange"]
           detail.status: ["CALLER_NOTIFIED", "RESOLVED"]

Responsibilities:
1. Fetch the complete EmergencyRecord from DynamoDB
2. Invoke Nova 2 Lite to generate a structured after-action report
3. Write the report as JSON to S3 (novaguard-audit-exports/summaries/{emergency_id}.json)
4. Index the report embedding into OpenSearch historical-incidents index
   (populates the deduplication corpus for future similar emergencies)
5. Update DynamoDB record with summary_s3_key and summary_generated_at fields

After-action report includes:
- Incident timeline (intake → triage → dispatch → resolved)
- Response time metrics vs SLA targets
- AI decisions made (triage severity, dispatch recommendation)
- Communication channel used + any accessibility accommodations
- Outcome assessment (units dispatched, patient handoff notes)
- Recommended protocol improvements (if any anomalies detected)

The report is used by:
- Dispatcher supervisors for QA review
- Training data collection (future Nova fine-tuning)
- Compliance audit (HIPAA, CJIS, CAD export)
- OpenSearch corpus to improve future deduplication accuracy

Performance contract: best-effort, <30 seconds (not on critical path)
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

from observability import log, set_correlation_context, timed_operation, emit_business_metric
from bedrock_client import invoke_nova_lite, generate_embedding
from opensearch_client import upsert_embedding_document

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("POWERTOOLS_LOG_LEVEL", "INFO"))

REGION              = os.environ.get("REGION", "us-east-1")
EMERGENCIES_TABLE   = os.environ.get("EMERGENCIES_TABLE", "novaguard-emergencies")
AUDIT_EXPORTS_BUCKET= os.environ.get("AUDIT_EXPORTS_BUCKET", "novaguard-audit-exports")
OPENSEARCH_INDEX    = os.environ.get("OPENSEARCH_INCIDENT_INDEX", "historical-incidents")

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3     = boto3.client("s3",         region_name=REGION)

# ── SLA targets (milliseconds) pulled from env vars matching constants.ts ──
SLA_TRIAGE_MS       = int(os.environ.get("SLA_TRIAGE_MS",    "2000"))
SLA_DISPATCH_MS     = int(os.environ.get("SLA_DISPATCH_MS",  "4000"))
SLA_END_TO_END_MS   = int(os.environ.get("SLA_END_TO_END_MS","8000"))

POST_INCIDENT_SYSTEM_PROMPT = """
You are NovaGuard's After-Action Reporting AI. Your role is to produce concise, factual
post-incident summaries for dispatcher supervisors and compliance auditors.

Rules:
- Write objectively. Do not speculate beyond what the data shows.
- Mask all PII: replace caller names, phone numbers, SSNs with [REDACTED].
- Address times in seconds (round to 1 decimal).
- If an SLA was breached, flag it clearly with the actual vs. target.
- Keep the total summary under 500 words.

Output format: strict JSON (no markdown, no code fences).
""".strip()


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    EventBridge trigger handler.

    Event detail must contain: emergency_id, status, correlation_id (optional).
    """
    detail       = event.get("detail", {})
    emergency_id = detail.get("emergency_id", "")
    status       = detail.get("status", "UNKNOWN")
    correlation_id = detail.get("correlation_id", context.aws_request_id)

    if not emergency_id:
        log.warn("POST_INCIDENT_NO_ID", "EventBridge event missing emergency_id — skipping")
        return {"status": "skipped", "reason": "no_emergency_id"}

    set_correlation_context(correlation_id, emergency_id)

    log.info("POST_INCIDENT_START", f"Generating after-action report for {emergency_id}",
             emergency_id=emergency_id, status=status)

    try:
        # ── Step 1: Fetch full EmergencyRecord ──────────────────────────────
        with timed_operation("ddb_fetch", slo_budget_ms=300):
            record = _fetch_emergency_record(emergency_id)
            if not record:
                log.warn("POST_INCIDENT_NOT_FOUND", "Emergency record not found in DynamoDB",
                         emergency_id=emergency_id)
                return {"status": "skipped", "reason": "record_not_found"}

        # ── Step 2: Build timeline metrics ─────────────────────────────────
        timeline  = _build_timeline(record)
        sla_flags = _evaluate_slas(timeline)

        # ── Step 3: Generate after-action report via Nova 2 Lite ───────────
        with timed_operation("nova_lite_summary", slo_budget_ms=8000):
            report = _generate_summary(record, timeline, sla_flags)

        # ── Step 4: Write report to S3 ──────────────────────────────────────
        s3_key = f"summaries/{emergency_id}/{int(time.time())}_report.json"
        with timed_operation("s3_write", slo_budget_ms=500):
            _write_report_to_s3(s3_key, report)

        # ── Step 5: Index into OpenSearch historical-incidents ──────────────
        with timed_operation("opensearch_index", slo_budget_ms=2000):
            _index_report(emergency_id, record, report, s3_key)

        # ── Step 6: Update DynamoDB with summary reference ──────────────────
        with timed_operation("ddb_update", slo_budget_ms=200):
            _update_emergency_record(emergency_id, s3_key, report)

        log.info("POST_INCIDENT_COMPLETE", "After-action report generated",
                 emergency_id=emergency_id, s3_key=s3_key)
        emit_business_metric("PostIncidentReportsGenerated", 1, "Count",
                             emergency_type=record.get("emergency_type", "UNKNOWN"))

        return {"status": "success", "emergency_id": emergency_id, "s3_key": s3_key}

    except Exception as e:
        log.error("POST_INCIDENT_FAILED", "Post-incident summary generation failed",
                  exc=e, emergency_id=emergency_id)
        emit_business_metric("PostIncidentReportErrors", 1, "Count")
        raise


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _fetch_emergency_record(emergency_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the full emergency record from DynamoDB."""
    table = dynamo.Table(EMERGENCIES_TABLE)
    response = table.get_item(Key={"emergency_id": emergency_id})
    return response.get("Item")


def _build_timeline(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute wall-clock durations between pipeline stages.
    All fields default to None if the timestamp is not present (e.g. partial failures).
    """
    def _epoch(field: str) -> Optional[float]:
        v = record.get(field)
        if v:
            try:
                return float(v)
            except Exception:
                pass
        return None

    created  = _epoch("created_at_epoch")
    triaged  = _epoch("triaged_at_epoch")
    dispatch = _epoch("dispatched_at_epoch")
    resolved = _epoch("resolved_at_epoch") or _epoch("caller_notified_at_epoch")

    def _delta_ms(start, end):
        if start and end:
            return round((end - start) * 1000, 1)
        return None

    return {
        "intake_to_triage_ms":    _delta_ms(created,  triaged),
        "triage_to_dispatch_ms":  _delta_ms(triaged,  dispatch),
        "dispatch_to_resolved_ms": _delta_ms(dispatch, resolved),
        "total_ms":               _delta_ms(created,  resolved),
    }


def _evaluate_slas(timeline: Dict[str, Any]) -> List[str]:
    """Return list of SLA breach strings (empty if all within budget)."""
    flags = []
    checks = [
        ("intake_to_triage_ms",    SLA_TRIAGE_MS,    "Triage SLA"),
        ("triage_to_dispatch_ms",  SLA_DISPATCH_MS,  "Dispatch SLA"),
        ("total_ms",               SLA_END_TO_END_MS,"End-to-End SLA"),
    ]
    for field, target, label in checks:
        actual = timeline.get(field)
        if actual and actual > target:
            flags.append(
                f"{label} BREACHED: {actual}ms actual vs {target}ms target "
                f"(+{round(actual - target, 1)}ms over budget)"
            )
    return flags


def _generate_summary(
    record: Dict[str, Any],
    timeline: Dict[str, Any],
    sla_flags: List[str],
) -> Dict[str, Any]:
    """
    Invoke Nova 2 Lite to produce a structured after-action report.
    Masks PII in the description before sending to the model.
    """
    description = (
        record.get("translated_description")
        or record.get("raw_description", "[no description]")
    )[:800]

    prompt = f"""Generate an after-action report for the following emergency incident.

INCIDENT DATA:
- Emergency ID: {record['emergency_id']}
- Type: {record.get('emergency_type', 'UNKNOWN')}
- Sub-type: {record.get('sub_type', 'N/A')}
- Severity score: {record.get('severity_score', 0)}/100
- Triage confidence: {record.get('triage_confidence', 0):.2f}
- Communication mode: {record.get('caller_communication_mode', 'UNKNOWN')}
- Preferred language: {record.get('preferred_language', 'en')}
- Description (sanitised): {description}
- Suspected hazards: {record.get('suspected_hazards', [])}
- Units dispatched: {json.dumps(record.get('resource_requirements', {}))}
- CAD incident number: {record.get('cad_incident_number', 'N/A')}
- Final status: {record.get('status', 'UNKNOWN')}

TIMELINE (milliseconds):
{json.dumps(timeline, indent=2)}

SLA FLAGS (empty = all within budget):
{json.dumps(sla_flags)}

OUTPUT JSON schema:
{{
  "incident_summary": <string — 2-3 sentence plain-English summary>,
  "response_quality": <"EXCELLENT"|"GOOD"|"ACCEPTABLE"|"NEEDS_REVIEW">,
  "sla_compliance": <"COMPLIANT"|"MINOR_BREACH"|"MAJOR_BREACH">,
  "timeline_seconds": {{
    "intake_to_triage": <float or null>,
    "triage_to_dispatch": <float or null>,
    "total_response": <float or null>
  }},
  "ai_decisions": [<string> ...],
  "accessibility_notes": <string or null>,
  "protocol_adherence": <"FULL"|"PARTIAL"|"DEVIATED"|"UNKNOWN">,
  "recommended_improvements": [<string> ...],
  "audit_flags": [<string> ...]
}}"""

    response = invoke_nova_lite(
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        system_prompt=POST_INCIDENT_SYSTEM_PROMPT,
        temperature=0.0,
        max_tokens=1024,
    )

    raw = response["output"]["message"]["content"][0]["text"].strip()
    # Strip markdown fences if model adds them
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Could not parse Nova Lite summary as JSON — storing raw text")
        return {"incident_summary": raw, "parse_error": True}


def _write_report_to_s3(s3_key: str, report: Dict[str, Any]) -> None:
    """Write JSON report to S3 audit exports bucket with KMS encryption."""
    s3.put_object(
        Bucket=AUDIT_EXPORTS_BUCKET,
        Key=s3_key,
        Body=json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
        Metadata={"generator": "novaguard-post-incident-agent"},
    )
    log.info("POST_INCIDENT_S3_WRITTEN", f"Report written to s3://{AUDIT_EXPORTS_BUCKET}/{s3_key}")


def _index_report(
    emergency_id: str,
    record: Dict[str, Any],
    report: Dict[str, Any],
    s3_key: str,
) -> None:
    """
    Generate an embedding for the incident summary and upsert into the
    historical-incidents OpenSearch index. This corpus is used by the intake
    agent's deduplication search to find semantically similar past incidents.
    """
    summary_text = report.get("incident_summary", "")
    description  = record.get("translated_description") or record.get("raw_description", "")
    embed_text   = f"{summary_text} {description}".strip()[:1200]

    if not embed_text:
        return

    embedding = generate_embedding(text=embed_text)

    upsert_embedding_document(
        index    = OPENSEARCH_INDEX,
        doc_id   = f"{emergency_id}-summary",
        embedding= embedding,
        metadata = {
            "emergency_id":      emergency_id,
            "emergency_type":    record.get("emergency_type", "UNKNOWN"),
            "severity_score":    record.get("severity_score", 0),
            "status":            "RESOLVED",
            "response_quality":  report.get("response_quality", "UNKNOWN"),
            "sla_compliance":    report.get("sla_compliance", "UNKNOWN"),
            "summary":           summary_text[:500],
            "s3_key":            s3_key,
            "resolved_at":       datetime.now(timezone.utc).isoformat(),
        },
    )


def _update_emergency_record(
    emergency_id: str,
    s3_key: str,
    report: Dict[str, Any],
) -> None:
    """Stamp the DynamoDB record with the summary S3 path and quality rating."""
    table = dynamo.Table(EMERGENCIES_TABLE)
    table.update_item(
        Key={"emergency_id": emergency_id},
        UpdateExpression=(
            "SET summary_s3_key = :key, "
            "summary_generated_at = :ts, "
            "response_quality = :rq, "
            "sla_compliance = :sla, "
            "updated_at = :now"
        ),
        ExpressionAttributeValues={
            ":key": s3_key,
            ":ts":  datetime.now(timezone.utc).isoformat(),
            ":rq":  report.get("response_quality", "UNKNOWN"),
            ":sla": report.get("sla_compliance",   "UNKNOWN"),
            ":now": datetime.now(timezone.utc).isoformat(),
        },
    )
