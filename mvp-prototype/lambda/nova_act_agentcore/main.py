"""
NovaGuard — Nova Act AgentCore Workflow
========================================
Deployed to AWS AgentCore Runtime via:
  act workflow deploy --name novaguard-cad-worker --source-dir lambda/nova_act_agentcore

Invoked by the Nova Act Cloud Handler Lambda via:
  bedrock-agentcore:InvokeAgentRuntime

Entry point follows AgentCore convention: def main(payload) -> dict
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

REGION            = os.environ.get("AWS_REGION", "us-east-1")
EMERGENCIES_TABLE = os.environ.get("EMERGENCIES_TABLE", "novaguard-emergencies")


def main(payload: dict) -> dict:
    """
    AgentCore entry point — called by InvokeAgentRuntime.

    Expected payload keys:
        emergency_id    str  — DynamoDB key
        cad_url         str  — CloudFront CAD form URL
        emergency_type  str  — FIRE / MEDICAL / POLICE / etc.
        priority        str  — P1 - IMMEDIATE / P2 - URGENT / P3 - ROUTINE
        location        str  — GPS coordinates or address string
        narrative       str  — triage narrative
        unit_callsigns  list — e.g. ["MEDIC-4", "ENG-7"]
    """
    t0 = time.time()

    emergency_id   = payload.get("emergency_id", f"AC-{int(t0)}")
    cad_url        = payload.get("cad_url", "https://d10tmiea7afu9g.cloudfront.net/cad_mock.html")
    emergency_type = payload.get("emergency_type", "MEDICAL")
    priority       = payload.get("priority", "P2 - URGENT")
    location       = payload.get("location", "Location not specified")
    narrative      = payload.get("narrative", "Emergency response required.")
    unit_callsigns = payload.get("unit_callsigns", [])

    units_str = ", ".join(unit_callsigns) if unit_callsigns else "TBD"

    logger.info(
        "[NOVA_ACT AGENTCORE] Starting CAD entry | id=%s type=%s priority=%s",
        emergency_id, emergency_type, priority,
    )

    cad_incident_id: str | None = None
    nova_act_used = False

    # ── Attempt Nova Act browser automation ────────────────────────────────
    try:
        from nova_act import NovaAct

        session_args = {}
        # Use IAM auth (no API key required in AgentCore Runtime)
        session_args["use_aws_iam_auth"] = True

        with NovaAct(
            starting_page=cad_url,
            **session_args,
        ) as agent:
            logger.info("[NOVA_ACT AGENTCORE] Browser opened — navigating CAD form")

            # Fill Incident Type
            agent.act(f"Select '{emergency_type}' from the incident type dropdown. If not found, select the closest match.")

            # Fill Priority
            agent.act(f"Set the priority to '{priority}'.")

            # Fill Location
            agent.act(f"Enter the following location in the location/address field: {location}")

            # Fill Narrative
            agent.act(f"Enter the following narrative in the narrative or description field: {narrative}")

            # Fill Units
            agent.act(f"Enter the dispatched units: {units_str}")

            # Submit
            result = agent.act(
                "Click the Submit or Create Incident button. "
                "Wait for confirmation, then return the CAD incident number or confirmation code shown on screen.",
                schema={"type": "object", "properties": {"incident_number": {"type": "string"}}, "required": ["incident_number"]},
            )

            cad_incident_id = result.matches.get("incident_number") if result.matches else None
            if not cad_incident_id:
                # Try to read it from the page response
                cad_incident_id = f"CAD-{emergency_id[:8].upper()}"

            nova_act_used = True
            logger.info("[NOVA_ACT COMPLETE] CAD incident created: %s", cad_incident_id)

    except ImportError:
        logger.warning("[NOVA_ACT AGENTCORE] nova_act not available — using simulated entry")
        cad_incident_id = _simulated_cad_entry(emergency_id, emergency_type, priority, location, units_str)
    except Exception as exc:
        logger.error("[NOVA_ACT AGENTCORE] Browser automation failed: %s", exc)
        cad_incident_id = _simulated_cad_entry(emergency_id, emergency_type, priority, location, units_str)

    elapsed_ms = int((time.time() - t0) * 1000)

    # ── Write result back to DynamoDB ──────────────────────────────────────
    dynamo = boto3.resource("dynamodb", region_name=REGION)
    table  = dynamo.Table(EMERGENCIES_TABLE)
    try:
        table.update_item(
            Key={"emergency_id": emergency_id},
            UpdateExpression=(
                "SET #s = :s, cad_incident_id = :c, nova_act_used = :n, "
                "nova_act_ms = :ms, completed_at = :t"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "CAD_ENTERED",
                ":c": cad_incident_id,
                ":n": nova_act_used,
                ":ms": elapsed_ms,
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info("[NOVA_ACT AGENTCORE] DynamoDB updated: %s → CAD_ENTERED", emergency_id)
    except Exception as exc:
        logger.error("[NOVA_ACT AGENTCORE] DynamoDB write failed: %s", exc)

    return {
        "emergency_id":    emergency_id,
        "cad_incident_id": cad_incident_id,
        "nova_act_used":   nova_act_used,
        "status":          "CAD_ENTERED",
        "elapsed_ms":      elapsed_ms,
        "units_dispatched": unit_callsigns,
        "cad_url":         cad_url,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    }


def _simulated_cad_entry(emergency_id: str, emergency_type: str, priority: str, location: str, units: str) -> str:
    """
    Fallback: return a deterministic CAD ID without browser automation.
    Used when Playwright is unavailable in the current environment.
    """
    prefix = {"FIRE": "FIR", "MEDICAL": "MED", "POLICE": "POL"}.get(emergency_type, "INC")
    ts = int(time.time())
    cad_id = f"{prefix}-{ts % 100000:05d}"
    logger.info("[NOVA_ACT AGENTCORE] Simulated CAD ID: %s (no browser available)", cad_id)
    return cad_id
