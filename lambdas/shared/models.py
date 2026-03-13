"""
NovaGuard Shared Data Models

Defines the canonical data structures that flow between agents.
All agents use these Pydantic models for structured input/output validation.

Design principles:
- Every inter-agent message is a typed, validated object.
- Models are append-only — new fields added, none removed, for backward compatibility.
- Discriminated unions identify message type at deserialization time.
- Datetimes stored as ISO-8601 strings (DynamoDB/JSON transport compatibility).
- Severity score: 0-100 integer (0=no emergency, 100=immediate life threat).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Dict, Any

try:
    from pydantic import BaseModel, Field, validator
    PYDANTIC_AVAILABLE = True
except ImportError:
    # Graceful fallback to dataclasses if pydantic not in layer
    from dataclasses import dataclass, field
    PYDANTIC_AVAILABLE = False


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class EmergencyStatus(str, Enum):
    PENDING_INTAKE     = "PENDING_INTAKE"
    TRIAGING           = "TRIAGING"
    AWAITING_DISPATCH  = "AWAITING_DISPATCH"
    DISPATCHED         = "DISPATCHED"
    IN_PROGRESS        = "IN_PROGRESS"
    RESOLVED           = "RESOLVED"
    ESCALATED          = "ESCALATED"
    CANCELLED          = "CANCELLED"

class EmergencyType(str, Enum):
    MEDICAL            = "MEDICAL"
    FIRE               = "FIRE"
    POLICE             = "POLICE"
    NATURAL_DISASTER   = "NATURAL_DISASTER"
    TRAFFIC            = "TRAFFIC"
    HAZMAT             = "HAZMAT"
    WELFARE_CHECK      = "WELFARE_CHECK"
    UNKNOWN            = "UNKNOWN"

class ResourceType(str, Enum):
    AMBULANCE          = "AMBULANCE"
    FIRE_ENGINE        = "FIRE_ENGINE"
    POLICE_UNIT        = "POLICE_UNIT"
    HELICOPTER         = "HELICOPTER"
    HAZMAT_UNIT        = "HAZMAT_UNIT"

class ResourceStatus(str, Enum):
    AVAILABLE          = "AVAILABLE"
    DISPATCHED         = "DISPATCHED"
    ON_SCENE           = "ON_SCENE"
    RETURNING          = "RETURNING"
    OUT_OF_SERVICE     = "OUT_OF_SERVICE"

class CallerCommunicationMode(str, Enum):
    TEXT               = "TEXT"        # Deaf/hard-of-hearing, non-verbal
    VOICE              = "VOICE"       # Traditional voice call
    VIDEO              = "VIDEO"       # Video relay
    HYBRID             = "HYBRID"      # Text + voice bridged

class AgentMessageType(str, Enum):
    INTAKE_COMPLETE    = "INTAKE_COMPLETE"
    TRIAGE_REQUEST     = "TRIAGE_REQUEST"
    TRIAGE_COMPLETE    = "TRIAGE_COMPLETE"
    DISPATCH_REQUEST   = "DISPATCH_REQUEST"
    DISPATCH_COMPLETE  = "DISPATCH_COMPLETE"
    COMMS_REQUEST      = "COMMS_REQUEST"
    DISPATCHER_APPROVAL_REQUEST = "DISPATCHER_APPROVAL_REQUEST"
    DISPATCHER_APPROVAL_RESPONSE = "DISPATCHER_APPROVAL_RESPONSE"


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

class GeoCoordinate(BaseModel):
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    accuracy_meters: Optional[float] = None
    source: str = "device"  # "device" | "geocoded" | "manual"

class EmergencyLocation(BaseModel):
    address_raw: str                             # Caller-provided free text
    address_normalized: Optional[str] = None    # Geocoded canonical address
    coordinate: Optional[GeoCoordinate] = None
    geohash5: Optional[str] = None              # Geohash precision-5 for proximity search
    unit_number: Optional[str] = None
    additional_info: Optional[str] = None       # "3rd floor", "behind the gas station"
    geocode_confidence: Optional[float] = None  # 0.0-1.0 confidence from Location Service


# ---------------------------------------------------------------------------
# Caller
# ---------------------------------------------------------------------------

class CallerProfile(BaseModel):
    caller_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    communication_mode: CallerCommunicationMode = CallerCommunicationMode.TEXT
    preferred_language: str = "en"              # BCP-47 language tag
    has_hearing_impairment: bool = True
    has_speech_impairment: bool = False
    has_mobility_impairment: bool = False
    name: Optional[str] = None
    phone_last4: Optional[str] = None           # Partial phone for dispatcher verification
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Emergency (core entity)
# ---------------------------------------------------------------------------

class EmergencyIntakeData(BaseModel):
    """
    Raw input from caller — may be ill-formed, multi-lingual, or minimal.
    The Intake Agent normalizes this into EmergencyRecord.
    """
    caller_id: str
    connection_id: str                          # WebSocket connection ID for caller
    text_content: Optional[str] = None         # Caller's free-text description
    voice_content_s3_key: Optional[str] = None# Pre-signed S3 key for audio upload
    image_s3_keys: List[str] = []              # S3 keys for uploaded photos
    detected_language: Optional[str] = None    # AWS Translate detection result
    client_timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    device_location: Optional[GeoCoordinate] = None


class TriageResult(BaseModel):
    """
    Output from Triage Agent.
    Every field must be present for downstream dispatch to function correctly.
    """
    severity_score: int = Field(..., ge=0, le=100)  # 0=no emergency, 100=critical
    confidence: float = Field(..., ge=0.0, le=1.0)  # AI confidence in this assessment
    emergency_type: EmergencyType
    sub_type: Optional[str] = None             # "cardiac_arrest", "structure_fire", "robbery"
    estimated_patient_count: int = 1
    suspected_hazards: List[str] = []          # "fire", "chemicals", "weapons", "falling_debris"
    recommended_response_level: int = Field(..., ge=1, le=5)  # 1=routine, 5=mass casualty
    protocol_references: List[str] = []        # Protocol IDs retrieved from OpenSearch
    triage_narrative: str                      # Human-readable triage summary for dispatcher
    resource_requirements: Dict[str, int] = {} # {"AMBULANCE": 2, "FIRE_ENGINE": 1}
    fallback_used: bool = False
    model_reasoning_trace: Optional[str] = None  # Nova Lite chain-of-thought (audit only)
    triage_duration_ms: int = 0
    triage_timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ResourceAssignment(BaseModel):
    resource_id: str
    resource_type: ResourceType
    unit_name: str
    current_location: GeoCoordinate
    eta_minutes: float
    route_distance_km: float
    status: ResourceStatus
    contact_channel: Optional[str] = None      # Radio channel or phone number


class DispatchResult(BaseModel):
    """Output from Dispatch Agent."""
    assigned_resources: List[ResourceAssignment]
    primary_hospital: Optional[str] = None     # Nearest ER with capacity
    hospital_eta_minutes: Optional[float] = None
    hospital_capacity_status: Optional[str] = None  # From Nova Act: "available" | "limited" | "divert"
    dispatch_narrative: str                    # Human-readable for dispatcher console
    nova_act_dashboard_screenshot_s3: Optional[str] = None
    fallback_used: bool = False
    dispatch_duration_ms: int = 0
    dispatch_timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class EmergencyRecord(BaseModel):
    """
    The canonical emergency entity stored in DynamoDB.
    PK: emergency_id, SK: version (integer, incremented on each update)
    All writes are new versions — never update-in-place (event sourcing pattern).
    """
    emergency_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    version: int = 1
    status: EmergencyStatus = EmergencyStatus.PENDING_INTAKE
    emergency_type: EmergencyType = EmergencyType.UNKNOWN

    # Caller
    caller_id: str
    caller_connection_id: str
    caller_communication_mode: CallerCommunicationMode = CallerCommunicationMode.TEXT
    preferred_language: str = "en"

    # Location
    location: Optional[EmergencyLocation] = None

    # Intake
    raw_description: Optional[str] = None
    translated_description: Optional[str] = None  # English translation if non-English
    media_s3_keys: List[str] = []
    embedding_ids: List[str] = []              # OpenSearch document IDs for embedded media

    # Triage
    triage_result: Optional[TriageResult] = None
    severity_score: int = 0                    # Denormalized for DynamoDB GSI sort key
    severity_score_override: Optional[int] = None  # Dispatcher can override AI-assigned severity

    # Dispatch
    dispatch_result: Optional[DispatchResult] = None
    assigned_dispatcher: Optional[str] = None
    dispatcher_connection_id: Optional[str] = None
    dispatch_approved_by: Optional[str] = None

    # Resolution
    resolved_at: Optional[str] = None
    resolution_notes: Optional[str] = None
    outcome: Optional[str] = None             # "transported_to_hospital", "on_scene_resolved", etc.

    # Timing
    created_at_epoch: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp()))
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ttl_epoch: int = Field(
        default_factory=lambda: int(datetime.now(timezone.utc).timestamp()) + (72 * 3600)
    )

    class Config:
        use_enum_values = True


# ---------------------------------------------------------------------------
# Inter-agent messages (A2A communication over SQS)
# ---------------------------------------------------------------------------

class AgentMessage(BaseModel):
    """
    Envelope for all messages sent over the agent SQS queue.
    Body contains the full EmergencyRecord at the time of handoff.
    """
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    message_type: AgentMessageType
    source_agent: str                          # "intake" | "triage" | "dispatch" | "comms"
    target_agent: str
    emergency_id: str
    payload: Dict[str, Any]                    # Serialized EmergencyRecord or sub-object
    correlation_id: Optional[str] = None       # Step Functions execution ID for tracing
    task_token: Optional[str] = None           # Step Functions waitForTaskToken value
    sent_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: Optional[str] = None           # ISO-8601, message stale after this time


# ---------------------------------------------------------------------------
# WebSocket message protocol
# ---------------------------------------------------------------------------

class WebSocketInboundMessage(BaseModel):
    """
    Messages from caller client to server.
    Route selection: $request.body.action
    """
    action: str                                # "emergency.initiate" | "emergency.update" | "ping"
    caller_id: Optional[str] = None
    emergency_id: Optional[str] = None
    payload: Dict[str, Any] = {}
    client_timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class WebSocketOutboundMessage(BaseModel):
    """
    Messages from server to caller or dispatcher WebSocket clients.
    """
    event: str                                 # "status_update" | "triage_complete" | "dispatch_update" | "protocol_guidance" | "sonic_audio"
    emergency_id: str
    payload: Dict[str, Any]
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Bedrock Guardrails trace
# ---------------------------------------------------------------------------

class GuardrailAssessment(BaseModel):
    blocked: bool = False
    reason: Optional[str] = None
    modified_output: Optional[str] = None
    pii_found: bool = False
    grounding_score: Optional[float] = None


# ---------------------------------------------------------------------------
# Utility: serialize to DynamoDB item format
# ---------------------------------------------------------------------------

def emergency_record_to_dynamo_item(record: EmergencyRecord) -> Dict[str, Any]:
    """
    Convert EmergencyRecord to DynamoDB put_item format.
    Recursively serializes nested Pydantic models.
    Strips None values (DynamoDB does not allow null except for explicit NULL type).
    """
    def _serialize(obj: Any) -> Any:
        if hasattr(obj, "dict"):
            return {k: _serialize(v) for k, v in obj.dict().items() if v is not None}
        if isinstance(obj, list):
            return [_serialize(i) for i in obj]
        if isinstance(obj, dict):
            return {k: _serialize(v) for k, v in obj.items() if v is not None}
        return obj

    item = _serialize(record)
    return item
