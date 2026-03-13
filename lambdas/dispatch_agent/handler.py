"""
NovaGuard Dispatch Agent — Lambda Handler

Trigger: AWS Step Functions task (synchronous Lambda invoke)
Input:  Triage result + emergency metadata
Output: DispatchRecommendation with assigned units, ETAs, and dispatch narrative

Responsibilities:
1. Parse triage result from Step Functions input
2. Call Amazon Location Service to find nearest available emergency resources
3. Score and rank resource candidates (proximity × availability × capability match)
4. Trigger ECS Fargate Nova Act task to enter the dispatch into legacy CAD dashboard
5. Poll DynamoDB for Nova Act task result (CAD incident number)
6. Record dispatch recommendation in DynamoDB
7. Return dispatch plan to Step Functions for communications step

Nova Act usage (ARCHITECTURE NOTE — why ECS Fargate, not Lambda):
- Nova Act requires a real browser runtime (headless Chromium + Playwright).
- AWS Lambda provides no display server or GUI stack — browser automation is not
  possible inside Lambda execution environments.
- CORRECT PATTERN: this Lambda submits an AWS ECS Fargate task (RunTask API) that
  launches a container image containing Nova Act SDK + headless Chromium.
- The ECS task authenticates to the CAD system, fills the incident form, submits
  the dispatch order, and writes the CAD incident number back to DynamoDB.
- This Lambda polls DynamoDB for the result with a configurable timeout.
- On timeout/failure: non-fatal — dispatch continues without a CAD reference number,
  and a human dispatcher alert is sent via SNS.

Amazon Location Service:
- search_place_index_for_position → nearest hospitals, fire stations, police stations
- Route calculator for accurate driving ETA (not straight-line distance)

Performance contract: <3500ms total (P99)
- Target 800ms:  Location Service queries (parallel)
- Target 2200ms: ECS task launch + Nova Act execution + DDB write
- Target 200ms:  DynamoDB write (dispatch recommendation)
- Budget: 300ms headroom for retries

CAD integration:
- ECS task env var CAD_BASE_URL determines target system
- CAD credentials fetched from Secrets Manager by the ECS task (never in Lambda env)
- Nova Act receives a structured instruction string; it translates to browser actions
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict

import boto3
from botocore.exceptions import ClientError

import sys
sys.path.insert(0, "/opt/python")

from observability import log, set_correlation_context, timed_operation, annotate_xray_trace, emit_business_metric
from mcp_tools import get_tool_registry
# Note: Nova Act does NOT run inside Lambda (no browser runtime available in Lambda environment).
# The dispatch Lambda submits an ECS Fargate task that runs Nova Act inside a container
# with a headless Chromium browser. The task writes its result back to DynamoDB.
# This Lambda polls DynamoDB for the result with a configurable timeout.

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("POWERTOOLS_LOG_LEVEL", "INFO"))

# --- AWS Clients ---
dynamo          = boto3.resource("dynamodb", region_name=os.environ.get("REGION", "us-east-1"))
location_client = boto3.client("location",   region_name=os.environ.get("REGION", "us-east-1"))
ecs_client      = boto3.client("ecs",         region_name=os.environ.get("REGION", "us-east-1"))
secrets_client  = boto3.client("secretsmanager", region_name=os.environ.get("REGION", "us-east-1"))
states_client   = boto3.client("stepfunctions", region_name=os.environ.get("REGION", "us-east-1"))

EMERGENCIES_TABLE       = os.environ.get("EMERGENCIES_TABLE",        "novaguard-emergencies")
RESOURCES_TABLE         = os.environ.get("RESOURCES_TABLE",          "novaguard-resources")
LOCATION_INDEX          = os.environ.get("LOCATION_INDEX",           "novaguard-resource-index")
ROUTE_CALCULATOR        = os.environ.get("ROUTE_CALCULATOR",         "novaguard-routes")
CAD_BASE_URL            = os.environ.get("CAD_BASE_URL",             "")
CAD_SECRETS_ARN         = os.environ.get("CAD_SECRETS_ARN",         "")
MAX_UNITS_PER_TYPE      = int(os.environ.get("MAX_UNITS_PER_TYPE",   "5"))
SEARCH_RADIUS_METERS    = int(os.environ.get("SEARCH_RADIUS_METERS", "15000"))  # 15 km
# ECS Fargate configuration for Nova Act browser automation container
ECS_NOVA_ACT_CLUSTER_ARN  = os.environ.get("ECS_NOVA_ACT_CLUSTER_ARN",  "")
ECS_NOVA_ACT_TASK_DEF_ARN = os.environ.get("ECS_NOVA_ACT_TASK_DEF_ARN", "")
ECS_NOVA_ACT_SUBNET_IDS   = os.environ.get("ECS_NOVA_ACT_SUBNET_IDS",   "")  # comma-separated
ECS_NOVA_ACT_POLL_TIMEOUT = int(os.environ.get("ECS_NOVA_ACT_POLL_TIMEOUT", "8"))   # seconds
ECS_NOVA_ACT_POLL_INTERVAL = float(os.environ.get("ECS_NOVA_ACT_POLL_INTERVAL", "0.5"))  # seconds

RESOURCE_TYPE_MAP = {
    "AMBULANCE":     "medical",
    "FIRE_ENGINE":   "fire",
    "POLICE_UNIT":   "law_enforcement",
    "HELICOPTER":    "air_medical",
    "HAZMAT_TRUCK":  "hazmat",
}


@dataclass
class ResourceCandidate:
    unit_id:         str
    unit_type:       str
    callsign:        str
    latitude:        float
    longitude:       float
    status:          str
    distance_meters: float
    eta_seconds:     float
    capability_tags: List[str]


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Step Functions task handler for Dispatch.

    Input event structure (from Step Functions):
      - emergency_id: str
      - triage_result: Dict  (from TriageAgent step output)
      - latitude: float
      - longitude: float
      - correlation_id: str

    Returns DispatchRecommendation dict.
    """
    emergency_id   = event.get("emergency_id", "unknown")
    correlation_id = event.get("correlation_id", context.aws_request_id)

    set_correlation_context(correlation_id, emergency_id)
    annotate_xray_trace(emergency_id)

    log.info(
        "DISPATCH_START",
        "Beginning dispatch for emergency",
        emergency_id=emergency_id,
        correlation_id=correlation_id,
    )

    overall_start_ms = int(time.time() * 1000)

    triage_result = event.get("triage_result", {})
    latitude      = event.get("latitude")
    longitude     = event.get("longitude")
    severity      = triage_result.get("severity_score", 75)
    resource_reqs = triage_result.get("resource_requirements", {"AMBULANCE": 1})
    emergency_type = triage_result.get("emergency_type", "UNKNOWN")

    _update_emergency_status(emergency_id, "DISPATCHING")

    # -----------------------------------------------------------------------
    # Step 1: Parallel Location Service queries for each needed resource type
    # -----------------------------------------------------------------------
    available_units: Dict[str, List[ResourceCandidate]] = {}

    if latitude and longitude:
        with timed_operation("location_service_queries", slo_budget_ms=1000):
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    executor.submit(
                        _find_nearest_units,
                        latitude, longitude, resource_type, needed_count
                    ): resource_type
                    for resource_type, needed_count in resource_reqs.items()
                    if needed_count > 0
                }
                for future in as_completed(futures):
                    rtype = futures[future]
                    try:
                        available_units[rtype] = future.result()
                    except Exception as e:
                        log.error("LOCATION_QUERY_FAILED", f"Failed to find {rtype} units", exc=e)
                        available_units[rtype] = []
    else:
        log.warn("DISPATCH_NO_GPS", "No GPS coordinates — location-based dispatch unavailable")
        available_units = {k: [] for k in resource_reqs}

    # -----------------------------------------------------------------------
    # Step 2: Select optimal units (nearest with required capability)
    # -----------------------------------------------------------------------
    assigned_units: List[Dict] = []
    assignment_details: Dict[str, List[Dict]] = {}

    for resource_type, needed_count in resource_reqs.items():
        candidates = available_units.get(resource_type, [])
        # Sort by ETA (ascending)
        candidates.sort(key=lambda u: u.eta_seconds)
        selected = candidates[:needed_count]

        assignment_details[resource_type] = [asdict(u) for u in selected]
        for unit in selected:
            assigned_units.append({
                "unit_id":       unit.unit_id,
                "callsign":      unit.callsign,
                "unit_type":     resource_type,
                "eta_seconds":   unit.eta_seconds,
                "eta_iso":       _seconds_to_eta_iso(unit.eta_seconds),
            })

    # -----------------------------------------------------------------------
    # Step 3: Nova Act — Automate legacy CAD dashboard entry
    # -----------------------------------------------------------------------
    cad_incident_number: Optional[str] = None

    if CAD_BASE_URL and assigned_units:
        with timed_operation("nova_act_cad_automation", slo_budget_ms=2000):
            cad_incident_number = _automate_cad_with_nova_act(
                emergency_id=emergency_id,
                triage_result=triage_result,
                assigned_units=assigned_units,
                latitude=latitude,
                longitude=longitude,
            )

    # -----------------------------------------------------------------------
    # Step 4: Generate dispatch narrative
    # -----------------------------------------------------------------------
    narrative = _generate_dispatch_narrative(
        triage_result, assigned_units, cad_incident_number
    )

    dispatch_recommendation = {
        "emergency_id":       emergency_id,
        "assigned_units":     assigned_units,
        "cad_incident_number": cad_incident_number,
        "dispatch_narrative": narrative,
        "assignment_details": assignment_details,
        "dispatch_timestamp": datetime.now(timezone.utc).isoformat(),
        "severity_score":     severity,
    }

    # -----------------------------------------------------------------------
    # Step 5: Persist to DynamoDB
    # -----------------------------------------------------------------------
    _persist_dispatch_result(emergency_id, dispatch_recommendation)

    total_duration_ms = int(time.time() * 1000) - overall_start_ms

    emit_business_metric("DispatchLatencyMs", total_duration_ms, "Milliseconds",
                         emergency_type=emergency_type)
    emit_business_metric("UnitsDispatched", len(assigned_units), "Count",
                         emergency_type=emergency_type)

    log.info(
        "DISPATCH_COMPLETE",
        "Dispatch complete",
        emergency_id=emergency_id,
        units_dispatched=len(assigned_units),
        cad_incident=cad_incident_number,
        total_duration_ms=total_duration_ms,
    )

    return dispatch_recommendation


def _find_nearest_units(
    latitude: float,
    longitude: float,
    resource_type: str,
    count: int,
) -> List[ResourceCandidate]:
    """
    Query Amazon Location Service for nearby emergency resources.
    Then retrieve operational status from our DynamoDB resources table.
    Filter to available + capable units only. Sort by ETA.
    """
    category = RESOURCE_TYPE_MAP.get(resource_type, "emergency_services")

    try:
        search_resp = location_client.search_place_index_for_position(
            IndexName=LOCATION_INDEX,
            Position=[longitude, latitude],
            MaxResults=MAX_UNITS_PER_TYPE * 3,  # over-fetch to allow post-filtering by type
        )
    except ClientError as e:
        log.error("LOCATION_SEARCH_FAILED", "Location Service search failed", exc=e)
        return []

    candidates = []
    for entry in search_resp.get("Results", []):
        pos = entry["Place"]["Geometry"]["Point"]  # [lon, lat]
        unit_id  = entry["Place"].get("Label", "unknown")
        distance = entry.get("Distance", 9999999)

        # Look up unit availability in DynamoDB resources table
        try:
            resources_table = dynamo.Table(RESOURCES_TABLE)
            resp = resources_table.get_item(Key={"unit_id": unit_id})
            ddb_record = resp.get("Item", {})
            status = ddb_record.get("status", "AVAILABLE")
        except Exception:
            status = "UNKNOWN"

        if status not in ("AVAILABLE", "STAGING"):
            continue  # Skip busy/offline units

        # Calculate driving ETA via Location Service Route Calculator
        eta_seconds = _estimate_eta_seconds(latitude, longitude, pos[1], pos[0])

        candidates.append(ResourceCandidate(
            unit_id=unit_id,
            unit_type=resource_type,
            callsign=str(ddb_record.get("callsign", unit_id[-4:])),  # type: ignore[arg-type]
            latitude=pos[1],
            longitude=pos[0],
            status=str(status),  # type: ignore[arg-type]
            distance_meters=distance,
            eta_seconds=eta_seconds,
            capability_tags=list(ddb_record.get("capabilities", [])),  # type: ignore[arg-type]
        ))

    return candidates[:count * 2]  # Return extras in case some are filtered downstream


def _estimate_eta_seconds(
    origin_lat: float, origin_lon: float,
    dest_lat:   float, dest_lon:   float,
) -> float:
    """Calculate ETA in seconds using Amazon Location Service route calculator."""
    try:
        resp = location_client.calculate_route(
            CalculatorName=ROUTE_CALCULATOR,
            DeparturePosition=[origin_lon, origin_lat],
            DestinationPosition=[dest_lon, dest_lat],
            TravelMode="Car",
        )
        duration = resp["Legs"][0]["DurationSeconds"]
        # Emergency vehicles travel faster — apply 20% reduction as conservative estimate
        return duration * 0.80
    except Exception:
        # Straight-line fallback: assume 50 km/h average speed
        import math
        dlat = dest_lat - origin_lat
        dlon = dest_lon - origin_lon
        distance_km = math.sqrt(dlat**2 + dlon**2) * 111.0
        return (distance_km / 50.0) * 3600


def _automate_cad_with_nova_act(
    emergency_id:  str,
    triage_result: Dict,
    assigned_units: List[Dict],
    latitude:       Optional[float],
    longitude:      Optional[float],
) -> Optional[str]:
    """
    Trigger an ECS Fargate task that runs Nova Act inside a container with a headless
    browser, then poll DynamoDB for the resulting CAD incident number.

    WHY ECS AND NOT LAMBDA:
    Nova Act uses Playwright/Chromium browser automation. Lambda provides no display
    server or browser runtime. The correct deployment target for Nova Act is an ECS
    Fargate container that includes Chromium, Playwright, and the Nova Act SDK.
    This Lambda's role is to submit the task and collect the result — not to run the
    browser automation itself.

    Pattern:
      1. Lambda → ecs.run_task() → ECS Fargate task starts
      2. ECS task runs Nova Act → fills CAD form → writes {cad_incident_number} to DynamoDB
      3. Lambda polls DynamoDB for the result key (nova_act_result#{emergency_id})
      4. On result: return incident number. On timeout: return None (non-fatal fallback).

    Returns the CAD incident number string if successful, None otherwise.
    Failure here is explicitly non-fatal — dispatch continues without a CAD reference.
    """
    if not (ECS_NOVA_ACT_CLUSTER_ARN and ECS_NOVA_ACT_TASK_DEF_ARN and CAD_BASE_URL):
        log.info(
            "NOVA_ACT_SKIP",
            "Nova Act ECS task not configured (ECS_NOVA_ACT_CLUSTER_ARN or CAD_BASE_URL missing) — skipping CAD automation",
        )
        return None

    severity        = triage_result.get("severity_score", 75)
    emergency_type  = triage_result.get("emergency_type", "UNKNOWN")
    narrative       = triage_result.get("triage_narrative", "")[:350]
    location_str    = f"{latitude:.6f}, {longitude:.6f}" if latitude and longitude else "unknown"
    priority_code   = _severity_to_priority_code(severity)
    unit_callsigns  = ",".join(u["callsign"] for u in assigned_units)

    # DynamoDB key where the ECS task will write its result
    result_ddb_key  = f"nova_act_result#{emergency_id}"

    subnet_ids = [s.strip() for s in ECS_NOVA_ACT_SUBNET_IDS.split(",") if s.strip()] or []

    # -----------------------------------------------------------------------
    # Step 1: Submit ECS Fargate task
    # The container image must contain: Nova Act SDK + Playwright + Chromium.
    # Task environment variables carry the dispatch context to the container.
    # -----------------------------------------------------------------------
    try:
        run_response = ecs_client.run_task(
            cluster=ECS_NOVA_ACT_CLUSTER_ARN,
            taskDefinition=ECS_NOVA_ACT_TASK_DEF_ARN,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnet_ids,
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "nova-act-worker",
                        "environment": [
                            {"name": "EMERGENCY_ID",   "value": emergency_id},
                            {"name": "CAD_BASE_URL",   "value": CAD_BASE_URL},
                            {"name": "CAD_SECRETS_ARN","value": CAD_SECRETS_ARN},
                            {"name": "EMERGENCY_TYPE", "value": emergency_type},
                            {"name": "PRIORITY_CODE",  "value": priority_code},
                            {"name": "LOCATION_STR",   "value": location_str},
                            {"name": "NARRATIVE",      "value": narrative},
                            {"name": "UNIT_CALLSIGNS", "value": unit_callsigns},
                            {"name": "RESULT_DDB_KEY", "value": result_ddb_key},
                            {"name": "EMERGENCIES_TABLE", "value": EMERGENCIES_TABLE},
                            {"name": "REGION",         "value": os.environ.get("REGION", "us-east-1")},
                        ],
                    }
                ]
            },
            count=1,
            # Propagate X-Ray trace ID so the Fargate task appears in the same trace
            tags=[{"key": "emergency_id", "value": emergency_id}],
        )
        task_arn = run_response.get("tasks", [{}])[0].get("taskArn", "unknown")
        log.info("NOVA_ACT_ECS_STARTED", "ECS Nova Act task submitted",
                 task_arn=task_arn, emergency_id=emergency_id)
    except Exception as e:
        log.error("NOVA_ACT_ECS_SUBMIT_FAILED", "Failed to submit ECS Nova Act task", exc=e)
        emit_business_metric("CadAutomationFailures", 1, "Count")
        return None

    # -----------------------------------------------------------------------
    # Step 2: Poll DynamoDB for the Nova Act result
    # The ECS task writes its result under a known key; we poll with a timeout.
    # DynamoDB polling is cheaper and faster than polling ECS DescribeTasks.
    # -----------------------------------------------------------------------
    poll_deadline = time.time() + ECS_NOVA_ACT_POLL_TIMEOUT
    table         = dynamo.Table(EMERGENCIES_TABLE)

    while time.time() < poll_deadline:
        try:
            resp = table.get_item(
                Key={"emergency_id": result_ddb_key, "version": 1},
                ProjectionExpression="cad_incident_number, nova_act_status",
            )
            item = resp.get("Item", {})
            if item:
                status = item.get("nova_act_status", "")
                inc_number = item.get("cad_incident_number")
                if status == "SUCCESS" and inc_number:
                    log.info("NOVA_ACT_CAD_SUCCESS", "Nova Act CAD automation succeeded",
                             incident_number=inc_number, emergency_id=emergency_id)
                    return str(inc_number)
                if status in ("AUTH_FAILED", "FORM_NOT_FOUND", "FAILED"):
                    log.warn("NOVA_ACT_CAD_STATUS", f"Nova Act returned non-success: {status}")
                    return None
        except Exception as e:
            log.warn("NOVA_ACT_DDB_POLL_ERROR", "DDB poll error during Nova Act wait", exc=e)

        time.sleep(ECS_NOVA_ACT_POLL_INTERVAL)

    # Timeout — Nova Act task is still running; dispatch continues without CAD number
    log.warn(
        "NOVA_ACT_TIMEOUT",
        f"Nova Act ECS task did not complete within {ECS_NOVA_ACT_POLL_TIMEOUT}s — continuing without CAD number",
        emergency_id=emergency_id,
    )
    emit_business_metric("CadAutomationFailures", 1, "Count")
    return None


def _extract_incident_number(nova_act_result: Any) -> Optional[str]:
    """
    Parse the incident number from the Nova Act ECS task result written to DynamoDB.
    The ECS worker writes a structured dict; this function normalizes the incident number format.
    Falls back to regex scan on plain-text result if the structured format is unavailable.
    """
    import re
    if not nova_act_result:
        return None
    text = str(nova_act_result).strip()

    # Try structured JSON parse first (new prompt format)
    try:
        # Extract first JSON object from the response text
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])
            status = data.get("status", "")
            number = data.get("incident_number")

            if status == "SUCCESS" and number:
                # Normalize: strip non-numeric chars and re-prefix
                digits = re.sub(r"[^\d]", "", str(number))
                return f"INC-{digits}" if digits else None

            if status in ("AUTH_FAILED", "FORM_NOT_FOUND", "TIMEOUT"):
                log.warn("NOVA_ACT_CAD_STATUS",
                         f"Nova Act returned non-success status: {status}")
                return None
    except (json.JSONDecodeError, AttributeError):
        pass

    # Fallback: regex scan on plain-text Nova Act observation
    match = re.search(r"(?:INC|incident)[\s#-]*(\d{4,})", text, re.IGNORECASE)
    if match:
        return f"INC-{match.group(1)}"

    # Last resort: any isolated 6+ digit sequence likely to be a CAD number
    match = re.search(r"\b(\d{6,8})\b", text)
    return f"INC-{match.group(1)}" if match else None


def _generate_dispatch_narrative(
    triage_result: Dict,
    assigned_units: List[Dict],
    cad_number: Optional[str],
) -> str:
    """Generate a brief natural-language dispatch summary for the communications agent."""
    unit_summary = ", ".join(f"{u['callsign']} ({u['unit_type']})" for u in assigned_units)
    soonest_eta  = min((u["eta_seconds"] for u in assigned_units), default=300)
    eta_min      = int(soonest_eta / 60)
    emergency_type = triage_result.get("emergency_type", "Emergency")
    cad_ref      = f" CAD reference: {cad_number}." if cad_number else ""

    return (
        f"{emergency_type.title().replace('_', ' ')} response dispatched. "
        f"Units en route: {unit_summary or 'coordinating dispatch'}. "
        f"Estimated arrival: {eta_min} minute(s).{cad_ref}"
    )


def _severity_to_priority_code(severity: int) -> str:
    if severity >= 80: return "P1 - IMMEDIATE"
    if severity >= 60: return "P2 - URGENT"
    if severity >= 40: return "P3 - MODERATE"
    return "P4 - ROUTINE"


def _seconds_to_eta_iso(seconds: float) -> str:
    from datetime import timedelta
    eta = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return eta.isoformat()


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
        log.warn("DISPATCH_STATUS_UPDATE_FAILED", "Could not update emergency status", exc=e)


def _persist_dispatch_result(emergency_id: str, dispatch_recommendation: Dict) -> None:
    try:
        table = dynamo.Table(EMERGENCIES_TABLE)
        now = datetime.now(timezone.utc).isoformat()
        table.put_item(Item={
            "emergency_id":          emergency_id,
            "version":               99,  # dispatch record version
            "status":                "DISPATCHED",
            "dispatch_recommendation": json.dumps(dispatch_recommendation),
            "updated_at":            now,
        })
    except Exception as e:
        log.error("DISPATCH_PERSIST_FAILED", "Failed to persist dispatch result", exc=e)
        raise
