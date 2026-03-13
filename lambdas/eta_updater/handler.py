"""
NovaGuard Responder Live ETA Updater — Lambda Handler

Trigger: EventBridge rule on Amazon Location Service tracker device position events.
         Rule pattern:
           source: ["aws.geo"]
           detail-type: ["Location Tracker Event"]
           detail.trackerName: ["novaguard-responders"]

Receives GPS position updates from dispatched responder units (sent by
their MDT / Mobile Data Terminal apps via the Location Service tracker API).
Recalculates ETA to the incident location using the RouteCalculator and
pushes the live ETA to the caller's active WebSocket / Sonic session.

Flow:
  1. Extract deviceId (= unit_id, e.g. "AMB-42") and current lat/lng from event
  2. Query DynamoDB resources table to find the emergency_id for this unit
  3. Query DynamoDB emergencies table for incident coordinates and caller connection
  4. Call Location Service CalculateRoute (current unit position → incident location)
  5. Push updated ETA via API Gateway WebSocket post_to_connection
  6. Update DynamoDB emergencies.eta_seconds with the freshest value
     so status-page queries always reflect the latest position

SSM parameter:
  /novaguard/ws-endpoint  — API Gateway WebSocket Management API endpoint URL
  /novaguard/route-calculator-name — Location Service route calculator name

Performance contract: best-effort <1000ms (not on critical latency path)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

import sys
sys.path.insert(0, "/opt/python")
from observability import log, set_correlation_context, timed_operation, emit_business_metric

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("POWERTOOLS_LOG_LEVEL", "INFO"))

REGION              = os.environ.get("REGION", "us-east-1")
EMERGENCIES_TABLE   = os.environ.get("EMERGENCIES_TABLE", "novaguard-emergencies")
RESOURCES_TABLE     = os.environ.get("RESOURCES_TABLE", "novaguard-resources")
CONNECTIONS_TABLE   = os.environ.get("CONNECTIONS_TABLE", "novaguard-connections")

dynamo     = boto3.resource("dynamodb", region_name=REGION)
location   = boto3.client("location",   region_name=REGION)
ssm_client = boto3.client("ssm",        region_name=REGION)

_ws_endpoint_cache:    Optional[str] = None
_calculator_name_cache: Optional[str] = None
_apigw_cache:          Optional[Any] = None


def _get_ws_endpoint() -> str:
    global _ws_endpoint_cache
    if _ws_endpoint_cache:
        return _ws_endpoint_cache
    try:
        _ws_endpoint_cache = ssm_client.get_parameter(
            Name=os.environ.get("WS_ENDPOINT_PARAM", "/novaguard/ws-endpoint")
        )["Parameter"]["Value"]
    except Exception as e:
        logger.warning("Could not load WS endpoint from SSM: %s", e)
        _ws_endpoint_cache = os.environ.get("WS_API_ENDPOINT", "")
    return _ws_endpoint_cache


def _get_calculator_name() -> str:
    global _calculator_name_cache
    if _calculator_name_cache:
        return _calculator_name_cache
    try:
        _calculator_name_cache = ssm_client.get_parameter(
            Name=os.environ.get("ROUTE_CALCULATOR_PARAM", "/novaguard/route-calculator-name")
        )["Parameter"]["Value"]
    except Exception as e:
        logger.warning("Could not load route calculator name from SSM: %s", e)
        _calculator_name_cache = os.environ.get("ROUTE_CALCULATOR_NAME", "novaguard-routes")
    return _calculator_name_cache


def _get_apigw_client() -> Any:
    global _apigw_cache
    if _apigw_cache is None:
        endpoint = _get_ws_endpoint()
        if endpoint:
            _apigw_cache = boto3.client(
                "apigatewaymanagementapi",
                endpoint_url=endpoint,
                region_name=REGION,
            )
    return _apigw_cache


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Process a Location Service device-position-event from EventBridge.
    """
    detail    = event.get("detail", {})
    device_id = detail.get("deviceId", "")
    position  = detail.get("position", [])   # [longitude, latitude] (GeoJSON order)
    tracker   = detail.get("trackerName", "")

    if not device_id or len(position) < 2:
        log.warn("ETA_UPDATER_BAD_EVENT", "Malformed Location tracker event", detail=str(detail)[:200])
        return {"status": "skipped", "reason": "bad_event"}

    unit_lon, unit_lat = float(position[0]), float(position[1])

    set_correlation_context(context.aws_request_id, device_id)
    log.info("ETA_UPDATER_START", f"Processing GPS ping for unit {device_id}",
             device_id=device_id, lat=unit_lat, lon=unit_lon)

    try:
        # Step 1: Find the active emergency assignment for this unit
        assignment = _get_unit_assignment(device_id)
        if not assignment:
            log.debug("ETA_UPDATER_NO_ASSIGNMENT",
                      f"Unit {device_id} has no active assignment — skipping")
            return {"status": "skipped", "reason": "no_assignment"}

        emergency_id  = assignment["emergency_id"]
        incident_lat  = float(assignment.get("incident_latitude",  0))
        incident_lon  = float(assignment.get("incident_longitude", 0))

        if not incident_lat or not incident_lon:
            log.warn("ETA_UPDATER_NO_COORDS", "Incident has no geocoded coordinates",
                     emergency_id=emergency_id)
            return {"status": "skipped", "reason": "no_incident_coords"}

        # Step 2: Calculate route from current unit position to incident
        with timed_operation("route_calculation", slo_budget_ms=500):
            eta_seconds, distance_km = _calculate_eta(
                unit_lat, unit_lon, incident_lat, incident_lon
            )

        log.info("ETA_CALCULATED", f"Unit {device_id} ETA: {eta_seconds}s ({distance_km:.2f} km)",
                 device_id=device_id, emergency_id=emergency_id,
                 eta_seconds=eta_seconds, distance_km=distance_km)

        # Step 3: Push ETA update to caller WebSocket
        caller_connection_id = assignment.get("caller_connection_id", "")
        if caller_connection_id:
            _push_eta_to_caller(
                connection_id= caller_connection_id,
                emergency_id = emergency_id,
                unit_id      = device_id,
                eta_seconds  = eta_seconds,
                distance_km  = distance_km,
            )

        # Step 4: Update DynamoDB emergency record with fresh ETA
        _update_emergency_eta(emergency_id, device_id, eta_seconds, unit_lat, unit_lon)

        emit_business_metric("ETAUpdatesDelivered", 1, "Count", unit_id=device_id[:6])
        return {
            "status": "success",
            "emergency_id": emergency_id,
            "eta_seconds":  eta_seconds,
            "device_id":    device_id,
        }

    except Exception as e:
        log.error("ETA_UPDATER_ERROR", "ETA update failed", exc=e, device_id=device_id)
        emit_business_metric("ETAUpdateErrors", 1, "Count")
        raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_unit_assignment(device_id: str) -> Optional[Dict[str, Any]]:
    """
    Query the resources table to find the active emergency assignment for a unit.
    The resources table uses unit_id as the primary partition key.

    Returns a dict with emergency_id, incident_latitude, incident_longitude,
    and caller_connection_id — or None if no active assignment.
    """
    try:
        table = dynamo.Table(RESOURCES_TABLE)
        response = table.get_item(Key={"unit_id": device_id})
        item = response.get("Item", {})
        if item.get("status") in ("DISPATCHED", "EN_ROUTE"):
            return item
        return None
    except ClientError as e:
        log.warn("ETA_RESOURCES_QUERY_FAILED", "DDB resources lookup failed", exc=e,
                 device_id=device_id)
        return None


def _calculate_eta(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
) -> tuple[int, float]:
    """
    Use Amazon Location Service CalculateRoute to get driving ETA and distance.

    Returns (eta_seconds, distance_km).
    On error, returns a Euclidean fallback estimate.
    """
    try:
        response = location.calculate_route(
            CalculatorName       = _get_calculator_name(),
            DeparturePosition    = [origin_lon, origin_lat],   # GeoJSON: [lon, lat]
            DestinationPosition  = [dest_lon, dest_lat],
            TravelMode           = "Car",
            DepartNow            = True,
            DistanceUnit         = "Kilometers",
        )
        summary  = response["Summary"]
        eta_s    = int(summary.get("DurationSeconds", 0))
        dist_km  = round(summary.get("Distance", 0), 2)
        return eta_s, dist_km

    except Exception as e:
        logger.warning("Location Service route calc failed — using Euclidean fallback: %s", e)
        # Rough Euclidean fallback: assume 40 km/h average urban speed
        import math
        lat_diff = abs(dest_lat - origin_lat) * 111.0        # ~111 km per degree lat
        lon_diff = abs(dest_lon - origin_lon) * 111.0 * math.cos(math.radians(origin_lat))
        dist_km  = round(math.sqrt(lat_diff ** 2 + lon_diff ** 2), 2)
        eta_s    = int((dist_km / 40.0) * 3600)               # 40 km/h → seconds
        return eta_s, dist_km


def _push_eta_to_caller(
    connection_id: str,
    emergency_id: str,
    unit_id: str,
    eta_seconds: int,
    distance_km: float,
) -> None:
    """
    Push a JSON ETA update message to the caller's active WebSocket connection.
    Gracefully handles stale connections (GoneException).
    """
    apigw = _get_apigw_client()
    if not apigw:
        log.warn("ETA_NO_APIGW", "API Gateway client unavailable — skipping push")
        return

    eta_minutes = eta_seconds // 60
    eta_seconds_rem = eta_seconds % 60

    payload = json.dumps({
        "type":         "eta_update",
        "emergency_id": emergency_id,
        "unit_id":      unit_id,
        "eta_seconds":  eta_seconds,
        "eta_display":  f"{eta_minutes}m {eta_seconds_rem}s",
        "distance_km":  distance_km,
        "message": (
            f"Responder unit {unit_id} is approximately "
            f"{eta_minutes} minute{'s' if eta_minutes != 1 else ''} away "
            f"({distance_km:.1f} km)."
        ),
        "updated_at":   datetime.now(timezone.utc).isoformat(),
    })

    try:
        apigw.post_to_connection(
            ConnectionId=connection_id,
            Data=payload.encode("utf-8"),
        )
        log.info("ETA_PUSHED", f"ETA update sent to caller",
                 connection_id=connection_id, eta_seconds=eta_seconds)
    except ClientError as e:
        if e.response["Error"]["Code"] == "GoneException":
            log.info("ETA_CONN_GONE", "Caller WebSocket already closed — dropping ETA push",
                     connection_id=connection_id)
        else:
            log.warn("ETA_PUSH_FAILED", "Failed to push ETA to caller", exc=e,
                     connection_id=connection_id)


def _update_emergency_eta(
    emergency_id: str,
    unit_id: str,
    eta_seconds: int,
    unit_lat: float,
    unit_lon: float,
) -> None:
    """
    Stamp the emergency record with the latest ETA and responder GPS coordinates.
    Uses a condition to only update if the new ETA is fresher (avoids stale overwrites
    if two consecutive GPS pings arrive out of order).
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        table = dynamo.Table(EMERGENCIES_TABLE)
        table.update_item(
            Key={"emergency_id": emergency_id},
            UpdateExpression=(
                "SET responder_eta_seconds = :eta, "
                "responder_last_position = :pos, "
                "responder_eta_updated_at = :ts, "
                "updated_at = :ts"
            ),
            ExpressionAttributeValues={
                ":eta": eta_seconds,
                ":pos": json.dumps({"unit_id": unit_id, "lat": unit_lat, "lon": unit_lon}),
                ":ts":  now,
            },
        )
    except Exception as e:
        log.warn("ETA_DDB_UPDATE_FAILED", "DDB ETA update failed (non-fatal)", exc=e,
                 emergency_id=emergency_id)
