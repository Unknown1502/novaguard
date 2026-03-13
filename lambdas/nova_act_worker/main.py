"""
NovaGuard Nova Act ECS Worker

This module runs inside an ECS Fargate container that includes:
  - Playwright (headless Chromium browser)
  - Amazon Nova Act SDK (nova-act package)
  - boto3 for DynamoDB result write-back

DEPLOYMENT:
  - This is NOT a Lambda function. It runs as an ECS Fargate task.
  - The Dispatch Lambda submits an ECS RunTask request to launch this container.
  - When complete, this worker writes the CAD incident number back to DynamoDB.
  - The Dispatch Lambda polls DynamoDB to collect the result.

WHY ECS, NOT LAMBDA:
  Nova Act uses Playwright/Chromium for browser automation. AWS Lambda provides no
  display server or GUI stack. ECS Fargate containers provide the full Linux
  environment required for headless browser execution.

ENVIRONMENT VARIABLES (injected by dispatch Lambda via ECS task overrides):
  EMERGENCY_ID       str  — unique emergency identifier
  CAD_BASE_URL       str  — URL of the legacy CAD web UI
  CAD_SECRETS_ARN    str  — Secrets Manager ARN for CAD credentials
  EMERGENCY_TYPE     str  — e.g. MEDICAL, FIRE, POLICE
  PRIORITY_CODE      str  — e.g. P1 - IMMEDIATE
  LOCATION_STR       str  — GPS coordinates string
  NARRATIVE          str  — triage narrative (sanitized)
  UNIT_CALLSIGNS     str  — comma-separated callsign list
  RESULT_DDB_KEY     str  — DynamoDB key to write result into
  EMERGENCIES_TABLE  str  — DynamoDB table name
  REGION             str  — AWS region

CONTAINER IMAGE:
  Base: public.ecr.aws/lambda/python:3.12 (or custom Ubuntu 22.04)
  Additional packages:
    nova-act          — Amazon Nova Act SDK
    playwright        — Headless browser automation
    chromium          — Browser runtime (installed via playwright install chromium)
    boto3             — AWS SDK
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)

# ── Configuration from ECS task environment overrides ──────────────────────
REGION              = os.environ.get("REGION", "us-east-1")
EMERGENCY_ID        = os.environ["EMERGENCY_ID"]
# CAD_BASE_URL kept for backward compat with ECS task def; now points to Medicare.gov
CAD_BASE_URL        = os.environ.get("CAD_BASE_URL", "https://www.medicare.gov/care-compare/")
EMERGENCY_TYPE      = os.environ.get("EMERGENCY_TYPE", "UNKNOWN")
PRIORITY_CODE       = os.environ.get("PRIORITY_CODE", "P2 - URGENT")
LOCATION_STR        = os.environ.get("LOCATION_STR", "unknown")
NARRATIVE           = os.environ.get("NARRATIVE", "")
UNIT_CALLSIGNS      = os.environ.get("UNIT_CALLSIGNS", "")
RESULT_DDB_KEY      = os.environ["RESULT_DDB_KEY"]
EMERGENCIES_TABLE   = os.environ["EMERGENCIES_TABLE"]
NOVA_ACT_API_KEY    = os.environ.get("NOVA_ACT_API_KEY", "")
NOVA_ACT_WORKFLOW   = os.environ.get("NOVA_ACT_WORKFLOW_NAME", "novaguard-cad-workflow")

# ── AWS clients ─────────────────────────────────────────────────────────────
dynamo = boto3.resource("dynamodb", region_name=REGION)


def _write_result_to_ddb(status: str, result_data: dict | None = None, duration_ms: int = 0) -> None:
    """Write the Nova Act hospital search result back to DynamoDB."""
    table = dynamo.Table(EMERGENCIES_TABLE)
    nova_status = "HOSPITALS_FOUND" if status == "SUCCESS" else "LOOKUP_FAILED"
    item = {
        "emergency_id":    RESULT_DDB_KEY,
        "version":         10,
        "nova_act_used":   True,
        "status":          nova_status,
        "duration_ms":     str(duration_ms),
        "case_label":      f"Hospital Lookup — {EMERGENCY_TYPE} ({PRIORITY_CODE})",
        "updated_at":      datetime.now(timezone.utc).isoformat(),
    }
    if result_data:
        item["hospitals"] = json.dumps(result_data.get("hospitals", []))
        item["hospitals_found"] = int(result_data.get("hospitals_found", 0))
        item["search_location"] = result_data.get("search_location", LOCATION_STR)
        item["source_url"] = result_data.get("source_url", CAD_BASE_URL)
    table.put_item(Item=item)
    hospital_count = len(result_data.get("hospitals", [])) if result_data else 0
    logger.info("Wrote Nova Act result to DynamoDB: status=%s hospitals_found=%d",
                nova_status, hospital_count)


def run_nova_act_hospital_search() -> None:
    """
    Main entry point. Uses Nova Act to search Medicare.gov Care Compare
    for hospitals near the emergency location.

    This automates a REAL U.S. government website — not a self-built form.
    The extracted hospital data feeds into the dispatch pipeline for
    optimal unit routing and destination hospital selection.

    Steps:
    1. Build Nova Act instruction for Medicare.gov hospital search
    2. Run Nova Act via IAM Workflow auth (real browser automation)
    3. Parse hospital data from the structured result
    4. Write result to DynamoDB (Dispatch Lambda polls for this)
    """
    try:
        from nova_act import NovaAct, Workflow  # type: ignore
        from nova_act.types.act_errors import ActAgentError, ActClientError
    except ImportError as e:
        logger.error("Nova Act SDK not installed in container: %s", e)
        _write_result_to_ddb("FAILED", None)
        sys.exit(1)

    logger.info("Starting Nova Act hospital search for emergency: %s | location: %s",
                EMERGENCY_ID, LOCATION_STR)
    run_start = time.time()

    # ── Build Nova Act instruction for Medicare.gov hospital search ──────
    instruction = f"""You are an emergency medical dispatch AI. Find the nearest hospitals to an emergency location using the Medicare.gov Care Compare website.

[STEP 1 — VERIFY PAGE]
  The page is already open at Medicare.gov Care Compare.
  You should see a heading like "Find & compare providers near you" and a provider type selector.
  If there is a cookie consent banner or popup, dismiss it first by clicking Accept or Close.

[STEP 2 — SELECT HOSPITALS]
  Find the provider type dropdown (it may say "Select a provider type" or "PROVIDER TYPE").
  Select "Hospitals" from the dropdown options.
  Wait a moment for the page to update after selection.

[STEP 3 — ENTER LOCATION]
  Find the location or address input field (it may be labeled with a location icon or placeholder text).
  Type this address: {LOCATION_STR}
  If autocomplete suggestions appear, click the one that best matches the address.
  If no autocomplete appears, that is fine — proceed to the next step.

[STEP 4 — SEARCH]
  Click the "Search" button.
  Wait up to 15 seconds for hospital results to appear on the page.

[STEP 5 — EXTRACT HOSPITAL DATA]
  Look at the search results. For each of the first 3 hospitals shown, extract:
    - Hospital name
    - Full street address
    - Overall quality rating (e.g. number of stars, or text like "Above average")
    - Distance from the search location (if displayed, e.g. "2.1 miles")

[STEP 6 — RETURN RESULTS AS JSON]
  Return ONLY a JSON object with no markdown formatting or extra text:
  {{
    "status": "SUCCESS",
    "search_location": "{LOCATION_STR}",
    "hospitals_found": <total number of results shown on page>,
    "hospitals": [
      {{"name": "<name>", "address": "<address>", "rating": "<rating>", "distance": "<distance>"}},
      {{"name": "<name>", "address": "<address>", "rating": "<rating>", "distance": "<distance>"}},
      {{"name": "<name>", "address": "<address>", "rating": "<rating>", "distance": "<distance>"}}
    ],
    "source_url": "{CAD_BASE_URL}"
  }}

  If the search returns no results:
  {{"status": "NO_RESULTS", "search_location": "{LOCATION_STR}", "hospitals_found": 0, "hospitals": [], "source_url": "{CAD_BASE_URL}"}}
""".strip()

    # ── Step 3: Run Nova Act (v3 SDK) ────────────────────────────────────────
    try:
        result_text = None
        workflow_used = False

        # ── Auth Strategy 1: API Key (simplest, works with free tier) ────────
        if NOVA_ACT_API_KEY:
            logger.info("[NOVA_ACT] Using API key auth")
            try:
                with NovaAct(
                    starting_page=CAD_BASE_URL,
                    nova_act_api_key=NOVA_ACT_API_KEY,
                    headless=True,
                ) as nova:
                    act_result = nova.act_get(
                        instruction,
                        schema={"type": "object"},
                    )
                    result_text = act_result.response if act_result.response else str(act_result)
                    logger.info("[NOVA_ACT] API key session complete")
            except Exception as api_err:
                logger.warning("[NOVA_ACT] API key auth failed: %s — trying IAM Workflow", api_err)

        # ── Auth Strategy 2: IAM Workflow (requires workflow definition + IAM perm) ──
        if result_text is None:
            logger.info("[NOVA_ACT] Attempting IAM Workflow auth: %s", NOVA_ACT_WORKFLOW)
            try:
                # Nova Act raises "Ambiguous Authentication Failure" if NOVA_ACT_API_KEY
                # env var exists (even empty string) while using Workflow context.
                # Temporarily remove it from the environment for the Workflow call.
                import os as _os
                _os.environ.pop("NOVA_ACT_API_KEY", None)

                with Workflow(
                    workflow_definition_name=NOVA_ACT_WORKFLOW,
                    model_id="nova-act-latest",
                ) as workflow:
                    with NovaAct(
                        starting_page=CAD_BASE_URL,
                        workflow=workflow,
                        # headless is managed by the Nova Act cloud service in Workflow mode;
                        # do NOT pass headless=True here — it triggers local Playwright init
                    ) as nova:
                        # Use act_get with schema={"type":"object"} since we ask the model
                        # to return a JSON object. Default schema={"type":"string"} would fail.
                        act_result = nova.act_get(
                            instruction,
                            schema={"type": "object"},
                        )
                        result_text = act_result.response or ""
                        workflow_used = True
                logger.info("[NOVA_ACT] IAM Workflow run finished")
            except Exception as wf_err:
                logger.warning("[NOVA_ACT] IAM Workflow auth failed: %s", wf_err)

        # ── NO SIMULATION FALLBACK — fail honestly ────────────────────────
        if result_text is None:
            logger.error("[NOVA_ACT] All auth strategies failed. No simulation fallback.")
            duration = int((time.time() - run_start) * 1000)
            _write_result_to_ddb("FAILED", None, duration_ms=duration)
            sys.exit(1)

        logger.info("[NOVA_ACT] completed. workflow_used=%s result_preview=%s",
                    workflow_used, (result_text or "")[:300])

        # ── Parse hospital data from result ────────────────────────────────
        result_data = None
        status = "UNKNOWN"
        result_text = result_text or ""

        # Try JSON parsing
        start = result_text.find("{")
        end   = result_text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(result_text[start:end])
                status = data.get("status", "UNKNOWN")
                if data.get("hospitals") is not None:
                    result_data = data
                    if not status or status == "UNKNOWN":
                        status = "SUCCESS" if data["hospitals"] else "NO_RESULTS"
            except json.JSONDecodeError:
                logger.warning("[NOVA_ACT] Failed to parse JSON: %s", result_text[:300])

        # ── Write result to DynamoDB ──────────────────────────────────────
        duration = int((time.time() - run_start) * 1000)
        _write_result_to_ddb(status, result_data, duration_ms=duration)
        hospital_count = len(result_data.get("hospitals", [])) if result_data else 0
        logger.info("[NOVA_ACT COMPLETE] Status=%s Hospitals=%d workflow=%s duration_ms=%d",
                    status, hospital_count, workflow_used, duration)

    except (ActAgentError, ActClientError) as e:
        logger.error("Nova Act agent/client error: %s", e)
        _write_result_to_ddb("AGENT_ERROR", None)
        sys.exit(1)
    except Exception as e:
        logger.error("Nova Act execution failed: %s", e, exc_info=True)
        _write_result_to_ddb("FAILED", None)
        sys.exit(1)


if __name__ == "__main__":
    run_nova_act_hospital_search()
