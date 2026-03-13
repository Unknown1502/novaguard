"""
NovaGuard Bedrock Tool Registry

Defines all tools available to Nova 2 Lite agents via the Bedrock Converse Tool Use API.
Tools are passed as the `toolConfig.tools` list in each Converse request. Nova 2 Lite
executes a full tool-use loop: it selects tools, NovaGuard executes them against real
AWS services, and the responses are fed back until the model reaches `end_turn`.

Each tool is registered with:
- Precise input/output JSON schemas (Bedrock Converse `toolSpec` format)
- Execution function bound to real AWS service clients
- Idempotency guards where applicable

Tool categories:
  LOCATION      — Amazon Location Service (geocoding, routing, proximity)
  STORAGE       — DynamoDB and S3 reads/writes
  PROTOCOL      — OpenSearch Serverless (emergency protocol retrieval)
  NOTIFICATION  — SNS/SQS message publishing
  VISUAL        — Nova Multimodal Embedding operations
  SYSTEM        — System clock, UUID generation, calculations

Important: Tool functions must be synchronous, idempotent where possible,
and must complete within the model's tool execution timeout.
"""

from __future__ import annotations

import json
import os
import uuid
import math
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

class BedrockToolRegistry:
    """
    Registry of all tools available to NovaGuard agents via the Bedrock Converse Tool Use API.

    Usage:
        registry = BedrockToolRegistry()
        tools_spec = registry.get_tools_spec()    # Pass as toolConfig.tools to Bedrock Converse
        result = registry.execute("tool_name", {"param": "value"})
    """

    def __init__(self):
        self._tools: Dict[str, Dict] = {}
        self._executors: Dict[str, Callable] = {}
        self._register_all()

    def register(self, name: str, description: str, input_schema: Dict, executor: Callable):
        self._tools[name] = {
            "toolSpec": {
                "name": name,
                "description": description,
                "inputSchema": {"json": input_schema},
            }
        }
        self._executors[name] = executor

    def get_tools_spec(self) -> List[Dict]:
        return list(self._tools.values())

    def execute(self, tool_name: str, tool_input: Dict) -> Any:
        if tool_name not in self._executors:
            raise ValueError(f"Unknown tool: {tool_name}. Available: {list(self._executors.keys())}")
        logger.info("Executing tool: %s | input_keys=%s", tool_name, list(tool_input.keys()))
        start_ms = int(time.time() * 1000)
        result = self._executors[tool_name](tool_input)
        duration_ms = int(time.time() * 1000) - start_ms
        logger.info("Tool %s complete | duration_ms=%d", tool_name, duration_ms)
        return result

    def _register_all(self):
        # ---- Location tools ----
        self.register(
            name="geocode_address",
            description=(
                "Convert a free-text address or location description to geographic coordinates. "
                "Use this when the emergency description contains a location that needs geocoding. "
                "Returns latitude, longitude, normalized_address, and confidence score."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "address_text": {
                        "type": "string",
                        "description": "The address or location description to geocode",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum geocoding results to return (default: 1)",
                        "default": 1,
                    },
                },
                "required": ["address_text"],
            },
            executor=self._tool_geocode_address,
        )

        self.register(
            name="find_nearest_resources",
            description=(
                "Find the nearest available emergency resources (ambulance, fire engine, police unit) "
                "to a given GPS coordinate. Searches the DynamoDB resource table and returns up to 10 "
                "nearest available units with current positions and ETAs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "latitude":      {"type": "number", "description": "Incident latitude"},
                    "longitude":     {"type": "number", "description": "Incident longitude"},
                    "resource_type": {
                        "type": "string",
                        "enum": ["AMBULANCE", "FIRE_ENGINE", "POLICE_UNIT", "HELICOPTER", "HAZMAT_UNIT", "ALL"],
                        "description": "Type of resource to search for",
                    },
                    "max_results":   {"type": "integer", "default": 5},
                    "max_radius_km": {"type": "number", "default": 25.0},
                },
                "required": ["latitude", "longitude", "resource_type"],
            },
            executor=self._tool_find_nearest_resources,
        )

        self.register(
            name="calculate_route_eta",
            description=(
                "Calculate the driving route and ETA from a resource's current position to the emergency location. "
                "Uses Amazon Location Service with real-time traffic data."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "origin_latitude":       {"type": "number"},
                    "origin_longitude":      {"type": "number"},
                    "destination_latitude":  {"type": "number"},
                    "destination_longitude": {"type": "number"},
                },
                "required": ["origin_latitude", "origin_longitude", "destination_latitude", "destination_longitude"],
            },
            executor=self._tool_calculate_route_eta,
        )

        # ---- Protocol retrieval tools ----
        self.register(
            name="retrieve_emergency_protocols",
            description=(
                "Search the emergency protocol knowledge base using semantic similarity. "
                "Returns relevant protocol documents and procedures for the given emergency type and context. "
                "Use this to retrieve first responder guidance and CPR/first aid instructions for callers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query_text": {
                        "type": "string",
                        "description": "Semantic query describing the emergency situation",
                    },
                    "emergency_type": {
                        "type": "string",
                        "enum": ["MEDICAL", "FIRE", "POLICE", "NATURAL_DISASTER", "TRAFFIC", "HAZMAT", "WELFARE_CHECK", "UNKNOWN"],
                    },
                    "max_results":     {"type": "integer", "default": 3},
                    "min_score":       {"type": "number", "default": 0.7, "description": "Minimum relevance score (0.0-1.0)"},
                },
                "required": ["query_text"],
            },
            executor=self._tool_retrieve_protocols,
        )

        # ---- Emergency record tools ----
        self.register(
            name="get_emergency_record",
            description="Retrieve the current state of an emergency record from DynamoDB by emergency_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "emergency_id": {"type": "string", "description": "The emergency UUID"},
                },
                "required": ["emergency_id"],
            },
            executor=self._tool_get_emergency_record,
        )

        self.register(
            name="update_emergency_status",
            description=(
                "Update the status of an active emergency. Used by agents to record state transitions "
                "(e.g., TRIAGING → AWAITING_DISPATCH). Creates a new version of the record (append-only)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "emergency_id": {"type": "string"},
                    "new_status":   {
                        "type": "string",
                        "enum": ["TRIAGING", "AWAITING_DISPATCH", "DISPATCHED", "IN_PROGRESS", "RESOLVED", "ESCALATED"],
                    },
                    "notes":        {"type": "string", "description": "Optional status change notes"},
                },
                "required": ["emergency_id", "new_status"],
            },
            executor=self._tool_update_emergency_status,
        )

        # ---- Severity scoring helper ----
        self.register(
            name="calculate_severity_score",
            description=(
                "Calculate a structured emergency severity score (0-100) based on key emergency indicators. "
                "Use this tool alongside your own reasoning to ensure the score is grounded in observable factors. "
                "Inputs map directly to a validated scoring rubric used by emergency medical services."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "is_life_threatening":        {"type": "boolean"},
                    "patient_unconscious":        {"type": "boolean"},
                    "active_bleeding":            {"type": "boolean"},
                    "breathing_difficulty":       {"type": "boolean"},
                    "chest_pain":                 {"type": "boolean"},
                    "suspected_cardiac_arrest":   {"type": "boolean"},
                    "fire_or_explosion_present":  {"type": "boolean"},
                    "hazardous_materials":        {"type": "boolean"},
                    "multiple_patients":          {"type": "boolean"},
                    "patient_count":              {"type": "integer", "default": 1},
                    "elderly_or_pediatric":       {"type": "boolean"},
                    "caller_distress_level":      {"type": "integer", "minimum": 1, "maximum": 5, "description": "1=calm, 5=extreme panic"},
                },
                "required": ["is_life_threatening"],
            },
            executor=self._tool_calculate_severity_score,
        )

        # ---- Notification tools ----
        self.register(
            name="send_dispatcher_alert",
            description="Publish an alert to the dispatcher SNS topic to notify available dispatchers of a new emergency.",
            input_schema={
                "type": "object",
                "properties": {
                    "emergency_id":   {"type": "string"},
                    "severity_score": {"type": "integer"},
                    "emergency_type": {"type": "string"},
                    "location":       {"type": "string"},
                    "summary":        {"type": "string"},
                },
                "required": ["emergency_id", "severity_score", "emergency_type", "summary"],
            },
            executor=self._tool_send_dispatcher_alert,
        )

        # ---- Audit logging ----
        self.register(
            name="log_ai_decision",
            description=(
                "Record an AI decision to the append-only audit log. "
                "Every AI-derived decision (severity score, dispatch recommendation, escalation) "
                "MUST be logged via this tool before being acted upon."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "emergency_id":    {"type": "string"},
                    "agent_name":      {"type": "string", "enum": ["intake", "triage", "dispatch", "comms"]},
                    "decision_type":   {"type": "string"},
                    "decision_value":  {},
                    "confidence":      {"type": "number", "description": "0.0-1.0 confidence"},
                    "reasoning":       {"type": "string"},
                },
                "required": ["emergency_id", "agent_name", "decision_type"],
            },
            executor=self._tool_log_ai_decision,
        )

        # ---- System tools ----
        self.register(
            name="get_current_timestamp",
            description="Return the current UTC timestamp in ISO-8601 format.",
            input_schema={"type": "object", "properties": {}},
            executor=lambda _: {"timestamp": datetime.now(timezone.utc).isoformat()},
        )

    # -----------------------------------------------------------------------
    # Tool executor implementations
    # -----------------------------------------------------------------------

    def _tool_geocode_address(self, params: Dict) -> Dict:
        location_client = boto3.client("location", region_name=os.environ["REGION"])
        place_index = os.environ.get("PLACE_INDEX_NAME", "novaguard-places")
        try:
            response = location_client.search_place_index_for_text(
                IndexName=place_index,
                Text=params["address_text"],
                MaxResults=params.get("max_results", 1),
            )
            results = []
            for result in response.get("Results", []):
                place = result.get("Place", {})
                geo = place.get("Geometry", {}).get("Point", [0, 0])
                results.append({
                    "latitude":           geo[1] if len(geo) > 1 else 0,
                    "longitude":          geo[0] if len(geo) > 0 else 0,
                    "normalized_address": place.get("Label", ""),
                    "confidence":         result.get("Relevance", 0),
                })
            return {"results": results, "status": "success"}
        except ClientError as e:
            logger.error("Geocode failed: %s", e)
            return {"results": [], "status": "error", "error": str(e)}

    def _tool_find_nearest_resources(self, params: Dict) -> Dict:
        """Query DynamoDB geohash GSI for nearby resources."""
        import geohash2  # Installed in Lambda layer

        lat = params["latitude"]
        lon = params["longitude"]
        resource_type = params.get("resource_type", "ALL")
        max_results = params.get("max_results", 5)
        max_radius_km = params.get("max_radius_km", 25.0)

        # Precision-5 geohash covers ~5km grid cell. Expand search to neighboring cells.
        center_hash = geohash2.encode(lat, lon, precision=5)
        search_hashes = [center_hash] + list(geohash2.neighbors(center_hash).values())  # type: ignore[attr-defined]

        dynamo = boto3.client("dynamodb", region_name=os.environ["REGION"])
        resources_table = os.environ["RESOURCES_TABLE"]

        found_resources = []
        for gh in search_hashes:
            try:
                query_params = {
                    "TableName": resources_table,
                    "IndexName": "geohash5-type-index",
                    "KeyConditionExpression": "geohash5 = :gh",
                    "FilterExpression": "#s = :available",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {
                        ":gh": {"S": gh},
                        ":available": {"S": "AVAILABLE"},
                    },
                }
                if resource_type != "ALL":
                    query_params["KeyConditionExpression"] += " AND resource_type = :rt"
                    query_params["ExpressionAttributeValues"][":rt"] = {"S": resource_type}

                response = dynamo.query(**query_params)
                for item in response.get("Items", []):
                    resource_lat = float((item.get("latitude") or {"N": "0"}).get("N", "0"))  # type: ignore[union-attr]
                    resource_lon = float((item.get("longitude") or {"N": "0"}).get("N", "0"))  # type: ignore[union-attr]
                    dist_km = self._haversine_km(lat, lon, resource_lat, resource_lon)
                    if dist_km <= max_radius_km:
                        found_resources.append({
                            "resource_id":   item.get("resource_id", {}).get("S", ""),
                            "resource_type": item.get("resource_type", {}).get("S", ""),
                            "unit_name":     item.get("unit_name", {}).get("S", "Unknown"),
                            "latitude":      resource_lat,
                            "longitude":     resource_lon,
                            "distance_km":   round(dist_km, 2),
                            "status":        item.get("status", {}).get("S", "UNKNOWN"),
                        })
            except Exception as e:
                logger.warning("Resource query for geohash %s failed: %s", gh, e)

        # Sort by distance, return top N
        found_resources.sort(key=lambda r: r["distance_km"])
        return {"resources": found_resources[:max_results], "total_found": len(found_resources)}

    def _tool_calculate_route_eta(self, params: Dict) -> Dict:
        location_client = boto3.client("location", region_name=os.environ["REGION"])
        calculator = os.environ.get("ROUTE_CALCULATOR_NAME", "novaguard-route-calc")
        try:
            response = location_client.calculate_route(
                CalculatorName=calculator,
                DeparturePosition=[params["origin_longitude"], params["origin_latitude"]],
                DestinationPosition=[params["destination_longitude"], params["destination_latitude"]],
                TravelMode="Car",
                OptimizeFor="FastestRoute",
            )
            legs = response.get("Legs", [{}])
            total_duration_s = sum(leg.get("DurationSeconds", 0) for leg in legs)
            total_distance_km = sum(leg.get("Distance", 0) for leg in legs)
            return {
                "eta_minutes":     round(total_duration_s / 60, 1),
                "distance_km":     round(total_distance_km, 2),
                "status":          "success",
            }
        except ClientError as e:
            logger.error("Route calculation failed: %s", e)
            return {"eta_minutes": None, "distance_km": None, "status": "error", "error": str(e)}

    def _tool_retrieve_protocols(self, params: Dict) -> Dict:
        """Vector search against OpenSearch Serverless emergency protocols index."""
        from bedrock_client import generate_embedding  # noqa: relative import in Lambda

        try:
            query_embedding = generate_embedding(text=params["query_text"])
        except Exception as e:
            logger.error("Embedding generation failed: %s", e)
            return {"protocols": [], "error": str(e)}

        endpoint = os.environ.get("OPENSEARCH_ENDPOINT", "")
        index = os.environ.get("OPENSEARCH_PROTOCOL_INDEX", "emergency-protocols")

        try:
            import boto3
            from opensearchpy import OpenSearch, RequestsHttpConnection
            from requests_aws4auth import AWS4Auth

            session = boto3.Session()
            credentials = session.get_credentials()
            if credentials is None:
                raise RuntimeError("No AWS credentials available for OpenSearch auth")
            resolved = credentials.resolve()  # type: ignore[union-attr]
            awsauth = AWS4Auth(
                resolved.access_key,  # type: ignore[union-attr]
                resolved.secret_key,  # type: ignore[union-attr]
                os.environ["REGION"],
                "aoss",
                session_token=resolved.token,  # type: ignore[union-attr]
            )

            client = OpenSearch(
                hosts=[{"host": endpoint.replace("https://", ""), "port": 443}],
                http_auth=awsauth,
                use_ssl=True,
                verify_certs=True,
                connection_class=RequestsHttpConnection,
                timeout=10,
            )

            max_results = params.get("max_results", 3)
            min_score = params.get("min_score", 0.7)

            query_body = {
                "size": max_results,
                "query": {
                    "knn": {
                        "embedding_vector": {
                            "vector": query_embedding,
                            "k": max_results * 2,  # Over-fetch and filter by score
                        }
                    }
                },
                "_source": ["protocol_id", "title", "emergency_type", "procedure_summary",
                            "first_responder_actions", "caller_instructions"],
                "min_score": min_score,
            }

            response = client.search(index=index, body=query_body)
            protocols = [
                {
                    "protocol_id":            hit["_source"].get("protocol_id", hit["_id"]),
                    "title":                  hit["_source"].get("title", ""),
                    "relevance_score":        round(hit["_score"], 3),
                    "procedure_summary":      hit["_source"].get("procedure_summary", ""),
                    "caller_instructions":    hit["_source"].get("caller_instructions", ""),
                    "first_responder_actions": hit["_source"].get("first_responder_actions", []),
                }
                for hit in response["hits"]["hits"]
            ]
            return {"protocols": protocols, "status": "success"}
        except Exception as e:
            logger.error("OpenSearch protocol retrieval failed: %s", e)
            return {"protocols": [], "status": "error", "error": str(e)}

    def _tool_get_emergency_record(self, params: Dict) -> Dict:
        dynamo = boto3.client("dynamodb", region_name=os.environ["REGION"])
        table = os.environ["EMERGENCIES_TABLE"]
        try:
            response = dynamo.query(
                TableName=table,
                KeyConditionExpression="emergency_id = :eid",
                ExpressionAttributeValues={":eid": {"S": params["emergency_id"]}},
                ScanIndexForward=False,
                Limit=1,
            )
            items = response.get("Items", [])
            if not items:
                return {"error": "Emergency not found", "status": "not_found"}
            from boto3.dynamodb.types import TypeDeserializer
            deserializer = TypeDeserializer()
            item = {k: deserializer.deserialize(v) for k, v in items[0].items()}
            return {"emergency": item, "status": "success"}
        except ClientError as e:
            return {"error": str(e), "status": "error"}

    def _tool_update_emergency_status(self, params: Dict) -> Dict:
        dynamo = boto3.resource("dynamodb", region_name=os.environ["REGION"])
        table = dynamo.Table(os.environ["EMERGENCIES_TABLE"])
        try:
            now = datetime.now(timezone.utc).isoformat()
            # Append-only: increment version and write new item
            get_resp = table.query(
                KeyConditionExpression="emergency_id = :eid",
                ExpressionAttributeValues={":eid": params["emergency_id"]},
                ScanIndexForward=False,
                Limit=1,
            )
            current_version = get_resp["Items"][0]["version"] if get_resp["Items"] else 0
            table.put_item(
                Item={
                    "emergency_id":   params["emergency_id"],
                    "version":        int(current_version) + 1,
                    "status":         params["new_status"],
                    "updated_at":     now,
                    "status_notes":   params.get("notes", ""),
                },
            )
            return {"status": "success", "new_version": int(current_version) + 1}
        except Exception as e:
            return {"error": str(e), "status": "error"}

    def _tool_calculate_severity_score(self, params: Dict) -> Dict:
        """
        Structured severity scoring rubric based on Priority Dispatch protocols.
        Produces a 0-100 score from observable emergency indicators.
        """
        score = 0
        flags = []

        if params.get("suspected_cardiac_arrest"):         score += 35; flags.append("cardiac_arrest")
        if params.get("is_life_threatening"):              score += 20; flags.append("life_threatening")
        if params.get("patient_unconscious"):              score += 15; flags.append("unconscious")
        if params.get("breathing_difficulty"):             score += 12; flags.append("breathing_difficulty")
        if params.get("active_bleeding"):                  score += 10; flags.append("active_bleeding")
        if params.get("chest_pain"):                       score += 10; flags.append("chest_pain")
        if params.get("fire_or_explosion_present"):        score += 15; flags.append("fire_explosion")
        if params.get("hazardous_materials"):              score += 12; flags.append("hazmat")
        if params.get("multiple_patients"):                score += 8;  flags.append("multiple_patients")
        if params.get("elderly_or_pediatric"):             score += 5;  flags.append("vulnerable_patient")

        # Distress-level bonus (1-5 scale adds 0-8 points)
        distress = params.get("caller_distress_level", 1)
        score += int((distress - 1) * 2)

        # Patient count multiplier (up to +15)
        count = params.get("patient_count", 1)
        score += min(15, (count - 1) * 3)

        final_score = min(100, score)

        return {
            "severity_score": final_score,
            "severity_label": (
                "CRITICAL" if final_score >= 80 else
                "HIGH"     if final_score >= 60 else
                "MODERATE" if final_score >= 40 else
                "LOW"
            ),
            "contributing_factors": flags,
        }

    def _tool_send_dispatcher_alert(self, params: Dict) -> Dict:
        sns_client = boto3.client("sns", region_name=os.environ["REGION"])
        topic_arn = os.environ["DISPATCHER_ALERTS_TOPIC_ARN"]
        try:
            message = {
                "emergency_id":   params["emergency_id"],
                "severity_score": params["severity_score"],
                "emergency_type": params["emergency_type"],
                "location":       params.get("location", "Unknown"),
                "summary":        params["summary"],
                "timestamp":      datetime.now(timezone.utc).isoformat(),
            }
            sns_client.publish(
                TopicArn=topic_arn,
                Message=json.dumps(message),
                Subject=f"NovaGuard Alert — Severity {params['severity_score']}/100",
                MessageAttributes={
                    "emergency_type": {"DataType": "String", "StringValue": params["emergency_type"]},
                    "severity":       {"DataType": "Number", "StringValue": str(params["severity_score"])},
                },
            )
            return {"status": "sent"}
        except ClientError as e:
            return {"status": "error", "error": str(e)}

    def _tool_log_ai_decision(self, params: Dict) -> Dict:
        dynamo = boto3.resource("dynamodb", region_name=os.environ["REGION"])
        table = dynamo.Table(os.environ["AUDIT_LOG_TABLE"])
        now = datetime.now(timezone.utc)
        try:
            table.put_item(
                Item={
                    "audit_id":      str(uuid.uuid4()),
                    "timestamp_iso": now.isoformat(),
                    "emergency_id":  params["emergency_id"],
                    "agent_name":    params["agent_name"],
                    "decision_type": params["decision_type"],
                    "decision_value": json.dumps(params.get("decision_value", {})),
                    "confidence":    str(params.get("confidence", "")),
                    "reasoning":     params.get("reasoning", ""),
                    "ttl_epoch":     int(now.timestamp()) + (365 * 7 * 24 * 3600),
                },
            )
            return {"status": "logged"}
        except Exception as e:
            logger.error("Audit log write failed: %s", e)
            return {"status": "error", "error": str(e)}

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Haversine formula for great-circle distance between two GPS coordinates."""
        R = 6371.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Singleton instance (module-level, reused across Lambda invocations)
# ---------------------------------------------------------------------------

_registry_instance: Optional[BedrockToolRegistry] = None


def get_tool_registry() -> BedrockToolRegistry:
    """Return the singleton BedrockToolRegistry. Reused across Lambda invocations in same container."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = BedrockToolRegistry()
    return _registry_instance
