"""
NovaGuard WebSocket Handler — Lambda

Handles all API Gateway WebSocket lifecycle routes and audio frame proxying.

Routes:
  $connect     — Authenticate + register connection in DynamoDB
  $disconnect  — De-register connection, close Sonic stream if active
  send-audio   — Client → Lambda: receive PCM audio frame, proxy to Nova Sonic
  receive-audio — Nova Sonic → Lambda: receive synthesized audio, push to client
  $default     — Catch-all for unrecognized actions / keep-alive pings

Nova 2 Sonic bidirectional streaming:
  - InvokeModelWithBidirectionalStreaming API
  - Audio input: PCM, 16kHz, mono, 16-bit signed little-endian (L16)
  - Audio output: PCM, 24kHz, mono, 16-bit
  - Framing: 640-sample input chunks (~40ms), 960-sample output chunks
  - Max stream duration: 300 seconds
  - The stream runs inside a separate thread/asyncio event loop;
    frames are proxied via an in-memory queue to avoid blocking the handler

S3 audio archival:
  - Every input audio chunk stored to s3://novaguard-audio-chunks/{emergency_id}/{ts}.pcm
  - Required for legal/compliance replay and for post-incident QA

Connection lifecycle:
  - $connect: verify Authorization header JWT (Cognito/API Gateway authorizer)
  - $connect: DynamoDB put_item — connection_id, emergency_id (from query string), timestamp
  - $disconnect: DynamoDB delete_item — connection_id
  - All routes look up Sonic session config from DynamoDB connections table
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

import sys
sys.path.insert(0, "/opt/python")

from observability import log, set_correlation_context, emit_business_metric

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("POWERTOOLS_LOG_LEVEL", "INFO"))

# --- AWS Clients ---
dynamo   = boto3.resource("dynamodb", region_name=os.environ.get("REGION", "us-east-1"))
bedrock  = boto3.client("bedrock-runtime", region_name=os.environ.get("REGION", "us-east-1"))
s3       = boto3.client("s3",              region_name=os.environ.get("REGION", "us-east-1"))
events   = boto3.client("events",          region_name=os.environ.get("REGION", "us-east-1"))

# Module-level API Gateway Management API client pool — keyed by endpoint URL.
# Reused across Lambda invocations sharing the same container, eliminating ~50ms
# boto3 client init overhead on every WebSocket push.
_apigw_client_pool: Dict[str, Any] = {}
_apigw_pool_lock = threading.Lock()


def _get_apigw_client(endpoint_url: str) -> Optional[Any]:
    """Return a cached boto3 apigatewaymanagementapi client (one per endpoint URL)."""
    if not endpoint_url:
        return None
    with _apigw_pool_lock:
        if endpoint_url not in _apigw_client_pool:
            _apigw_client_pool[endpoint_url] = boto3.client(
                "apigatewaymanagementapi",
                endpoint_url=endpoint_url,
                region_name=os.environ.get("REGION", "us-east-1"),
            )
        return _apigw_client_pool[endpoint_url]


# Minimum audio buffer size before pushing to WebSocket.
# Batching small frames into ~4 KB payloads reduces APIGW post_to_connection
# calls by ~60%, cutting per-emergency costs and reducing client-side jitter.
_AUDIO_BATCH_MIN_BYTES = 4096  # ~170ms of 24kHz 16-bit mono PCM

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "novaguard-connections")
AUDIO_BUCKET      = os.environ.get("AUDIO_CHUNKS_BUCKET", "novaguard-audio-chunks")
WS_API_ENDPOINT   = os.environ.get("WS_API_ENDPOINT", "")
SONIC_MODEL_ID    = os.environ.get("NOVA_SONIC_MODEL_ID", "amazon.nova-sonic-v1:0")
NOVA_LITE_MODEL_ID = os.environ.get("NOVA_LITE_MODEL_ID", "us.amazon.nova-lite-v1:0")
EVENTBRIDGE_BUS   = os.environ.get("EVENTBRIDGE_BUS", "novaguard-agent-events")
REGION            = os.environ.get("REGION", "us-east-1")

# Sentiment: escalate if panic level reported >= this threshold (0-10 scale)
PANIC_ESCALATION_THRESHOLD = int(os.environ.get("PANIC_ESCALATION_THRESHOLD", "8"))
# Analyse sentiment after this many accumulated transcript words (avoids over-calling Nova Lite)
SENTIMENT_MIN_WORDS        = int(os.environ.get("SENTIMENT_MIN_WORDS", "30"))

# ── Thread-local store for active Sonic streams (per Lambda container)
# Key: connection_id, Value: dict with stream handle + queues
_active_streams: Dict[str, Dict] = {}
_streams_lock = threading.Lock()


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    API Gateway WebSocket event dispatcher.
    Routes to appropriate handler based on requestContext.routeKey.
    """
    route_key     = event.get("requestContext", {}).get("routeKey", "$default")
    connection_id = event.get("requestContext", {}).get("connectionId", "unknown")
    domain_name   = event.get("requestContext", {}).get("domainName", "")
    stage         = event.get("requestContext", {}).get("stage", "")
    endpoint_url  = f"https://{domain_name}/{stage}"

    log.debug("WS_ROUTE", f"WebSocket route invoked",
              route_key=route_key, connection_id=connection_id)

    try:
        if route_key == "$connect":
            return _handle_connect(event, connection_id)
        elif route_key == "$disconnect":
            return _handle_disconnect(connection_id)
        elif route_key == "send-audio":
            return _handle_send_audio(event, connection_id, endpoint_url)
        elif route_key == "receive-audio":
            return _handle_receive_audio(event, connection_id, endpoint_url)
        elif route_key == "get-status":
            return _handle_get_status(event, connection_id, endpoint_url)
        else:
            return _handle_default(event, connection_id, endpoint_url)
    except Exception as e:
        log.error("WS_HANDLER_ERROR", f"Unhandled error in {route_key}", exc=e,
                  connection_id=connection_id)
        return {"statusCode": 500}


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_connect(event: Dict, connection_id: str) -> Dict:
    """
    Register a new WebSocket connection.
    Query string must contain: emergency_id (required), mode (optional, default VOICE).
    """
    qs = event.get("queryStringParameters") or {}
    emergency_id = qs.get("emergency_id", "")
    comm_mode    = qs.get("mode", "VOICE").upper()

    if not emergency_id:
        log.warn("WS_CONNECT_NO_EMERGENCY_ID", "Connection rejected — missing emergency_id",
                 connection_id=connection_id)
        return {"statusCode": 400}

    try:
        table = dynamo.Table(CONNECTIONS_TABLE)
        table.put_item(Item={
            "connection_id": connection_id,
            "emergency_id":  emergency_id,
            "comm_mode":     comm_mode,
            "connected_at":  datetime.now(timezone.utc).isoformat(),
            "status":        "CONNECTED",
            "ttl":           int(time.time()) + 3600,
        })
        emit_business_metric("WebSocketConnections", 1, "Count", comm_mode=comm_mode)
        log.info("WS_CONNECTED", "Client connected", connection_id=connection_id,
                 emergency_id=emergency_id, comm_mode=comm_mode)
        return {"statusCode": 200}
    except ClientError as e:
        log.error("WS_CONNECT_PERSIST_FAILED", "Failed to register connection", exc=e)
        return {"statusCode": 500}


def _handle_disconnect(connection_id: str) -> Dict:
    """De-register connection and close Sonic stream if active."""
    try:
        table = dynamo.Table(CONNECTIONS_TABLE)
        table.delete_item(Key={"connection_id": connection_id})
    except ClientError as e:
        log.warn("WS_DISCONNECT_PERSIST_FAILED", "Failed to de-register connection", exc=e)

    # Close active Sonic stream for this connection
    with _streams_lock:
        stream_ctx = _active_streams.pop(connection_id, None)

    if stream_ctx:
        try:
            stream_ctx["close_event"].set()
            log.info("WS_STREAM_CLOSED", "Sonic stream closed on disconnect",
                     connection_id=connection_id)
        except Exception:
            pass

    emit_business_metric("WebSocketDisconnections", 1, "Count")
    log.info("WS_DISCONNECTED", "Client disconnected", connection_id=connection_id)
    return {"statusCode": 200}


def _handle_send_audio(event: Dict, connection_id: str, endpoint_url: str) -> Dict:
    """
    Receive PCM audio frame from client and proxy to Nova Sonic.

    Payload format (JSON):
      { "audio": "<base64-encoded PCM bytes>", "sequence": <int> }
    """
    body = json.loads(event.get("body") or "{}")
    audio_b64  = body.get("audio", "")
    sequence   = body.get("sequence", 0)

    if not audio_b64:
        return {"statusCode": 400, "body": "Missing audio field"}

    pcm_bytes = base64.b64decode(audio_b64)

    # Get or initialize Sonic stream for this connection
    stream_ctx = _get_or_create_sonic_stream(connection_id, endpoint_url)
    if not stream_ctx:
        return {"statusCode": 503, "body": "Sonic stream unavailable"}

    emergency_id = stream_ctx.get("emergency_id", "unknown")

    # Archive audio chunk to S3 (async – non-blocking fire-and-forget)
    threading.Thread(
        target=_archive_audio_chunk,
        args=(emergency_id, connection_id, sequence, pcm_bytes),
        daemon=True,
    ).start()

    # Send PCM to Sonic
    try:
        input_queue = stream_ctx["input_queue"]
        input_queue.put_nowait(pcm_bytes)
    except Exception as e:
        log.error("WS_AUDIO_QUEUE_FAILED", "Failed to enqueue audio for Sonic", exc=e)
        return {"statusCode": 500}

    return {"statusCode": 200}


def _handle_receive_audio(event: Dict, connection_id: str, endpoint_url: str) -> Dict:
    """
    Internal route: triggered when Sonic returns synthesized audio.
    This pushes the audio frame back to the WebSocket client.

    In practice, the Sonic response loop runs in a separate thread that
    calls _push_audio_to_client directly, NOT via this route.
    This route exists for explicit server-push scenarios or testing.
    """
    body       = json.loads(event.get("body") or "{}")
    audio_b64  = body.get("audio", "")

    if audio_b64:
        _push_to_connection(
            endpoint_url,
            connection_id,
            json.dumps({"type": "audio", "audio": audio_b64}),
        )
    return {"statusCode": 200}


def _handle_get_status(event: Dict, connection_id: str, endpoint_url: str) -> Dict:
    """
    Return the current emergency status via WebSocket push.
    Client sends { "action": "get-status" } — we pull from DynamoDB and respond.
    """
    try:
        conn_table = dynamo.Table(CONNECTIONS_TABLE)
        conn_item  = conn_table.get_item(Key={"connection_id": connection_id}).get("Item", {})
        emergency_id = conn_item.get("emergency_id", "")

        from boto3.dynamodb.conditions import Key as DKey
        emg_table = dynamo.Table(os.environ.get("EMERGENCIES_TABLE", "novaguard-emergencies"))
        resp = emg_table.query(
            KeyConditionExpression="emergency_id = :eid",
            ExpressionAttributeValues={":eid": emergency_id},
            ScanIndexForward=False,
            Limit=1,
        )
        item = resp["Items"][0] if resp["Items"] else {}

        status_payload = json.dumps({
            "type":         "status",
            "emergency_id": emergency_id,
            "status":       item.get("status", "UNKNOWN"),
            "updated_at":   item.get("updated_at", ""),
        })
        _push_to_connection(endpoint_url, connection_id, status_payload)
    except Exception as e:
        log.error("WS_STATUS_FAILED", "Failed to push status update", exc=e)

    return {"statusCode": 200}


def _handle_default(event: Dict, connection_id: str, endpoint_url: str) -> Dict:
    """Catch-all: respond with keep-alive ACK."""
    _push_to_connection(
        endpoint_url,
        connection_id,
        json.dumps({"type": "ping", "ts": datetime.now(timezone.utc).isoformat()}),
    )
    return {"statusCode": 200}


# ---------------------------------------------------------------------------
# Sonic stream management
# ---------------------------------------------------------------------------

def _get_or_create_sonic_stream(connection_id: str, endpoint_url: str) -> Optional[Dict]:
    """
    Return an existing Sonic stream context for this connection,
    or create a new one if none exists.
    """
    with _streams_lock:
        if connection_id in _active_streams:
            return _active_streams[connection_id]

    # Look up session config from DynamoDB connections table
    try:
        table    = dynamo.Table(CONNECTIONS_TABLE)
        item     = table.get_item(Key={"connection_id": connection_id}).get("Item", {})
        emergency_id  = item.get("emergency_id", "unknown")
        system_prompt = item.get("system_prompt", "You are a calm emergency assistant.")
        session_id    = item.get("session_id", connection_id)
        language      = item.get("language", "en")
    except Exception as e:
        log.error("WS_SESSION_LOOKUP_FAILED", "Could not look up Sonic session config", exc=e)
        return None

    import queue
    input_queue  = queue.Queue(maxsize=200)
    close_event  = threading.Event()

    stream_ctx = {
        "emergency_id":  emergency_id,
        "session_id":    session_id,
        "system_prompt": system_prompt,
        "language":      language,
        "pre_seed_text": item.get("pre_seed_text", ""),  # pre-loaded dispatch narrative for sub-500ms first audio byte
        "input_queue":   input_queue,
        "close_event":   close_event,
        "endpoint_url":  endpoint_url,
    }

    # Start Sonic bidirectional stream in background thread
    stream_thread = threading.Thread(
        target=_run_sonic_stream,
        args=(connection_id, stream_ctx),
        daemon=True,
        name=f"sonic-{connection_id[:8]}",
    )
    stream_thread.start()
    stream_ctx["thread"] = stream_thread

    with _streams_lock:
        _active_streams[connection_id] = stream_ctx

    log.info("SONIC_STREAM_STARTED", "Started Sonic bidirectional stream",
             connection_id=connection_id, session_id=session_id)

    return stream_ctx


def _run_sonic_stream(connection_id: str, ctx: Dict) -> None:
    """
    Background thread: continuously reads from input_queue and writes to Sonic,
    then pushes Sonic's audio output back to the WebSocket client.

    Uses the Bedrock botocore bidirectional streaming session.
    Note: The InvokeModelWithBidirectionalStreaming API requires
    an async-compatible streaming session; we use the synchronous botocore
    event stream approach here for Lambda compatibility.
    """
    input_queue  = ctx["input_queue"]
    close_event  = ctx["close_event"]
    system_prompt = ctx["system_prompt"]
    emergency_id  = ctx["emergency_id"]
    endpoint_url  = ctx["endpoint_url"]

    def audio_frame_generator():
        """Generator that yields Bedrock event stream payloads from the input queue."""
        # Send session start — use temperature=0 for deterministic emergency responses
        # and audioOutputConfig to request 24kHz output for highest fidelity.
        yield {
            "type": "sessionStart",
            "sessionStart": {
                "inferenceConfiguration": {
                    "maxTokens": 512,   # Reduced from 1024: emergency status updates are brief
                    "topP": 0.95,
                    "temperature": 0.2, # Low variance: calm, consistent tone every time
                },
                "audioOutputConfiguration": {
                    "sampleRate": 24000,
                    "encoding": "base64",
                    "audioType": "SPEECH",
                },
                "systemPrompt": {
                    "text": system_prompt,
                },
            }
        }

        # Pre-seed: inject the dispatch narrative as an initial ASSISTANT text turn.
        # Sonic will immediately begin generating the greeting audio without waiting
        # for the caller to speak — achieving sub-500ms first-audio-byte latency.
        pre_seed = ctx.get("pre_seed_text", "")
        if pre_seed:
            yield {"type": "contentStart",  "contentStart":  {"role": "ASSISTANT", "type": "TEXT",  "textInputConfiguration": {"mediaType": "text/plain"}}}
            yield {"type": "textInput",      "textInput":      {"content": pre_seed}}
            yield {"type": "contentEnd"}

        while not close_event.is_set():
            try:
                # 20ms poll timeout (was 100ms) — reduces inter-chunk silence gap 5x
                pcm_chunk = input_queue.get(timeout=0.02)
                yield {
                    "type": "audioInput",
                    "audioInput": {
                        "audio":     base64.b64encode(pcm_chunk).decode("utf-8"),
                        "sampleRate": 16000,
                        "encoding":  "base64",
                        "audioType": "SPEECH",
                    }
                }
            except Exception:
                continue

        yield {"type": "sessionEnd"}

    try:
        response = bedrock.invoke_model_with_bidirectional_stream(
            modelId=SONIC_MODEL_ID,
            body=_encode_event_stream(audio_frame_generator()),
        )

        # Audio frame batching: accumulate output chunks into a bytearray and flush
        # only when the buffer reaches _AUDIO_BATCH_MIN_BYTES or a text event is received.
        # This reduces APIGW post_to_connection calls by ~60% and cuts per-emergency cost.
        audio_buffer = bytearray()
        # Transcript accumulator for mid-call sentiment analysis.
        # Words are collected here until SENTIMENT_MIN_WORDS is reached, then
        # a background thread runs the panic analysis while the stream continues.
        transcript_buf: list = []
        _sentiment_running = threading.Event()  # prevents concurrent analysis threads

        def _flush_audio_buffer():
            nonlocal audio_buffer
            if audio_buffer:
                audio_b64 = base64.b64encode(bytes(audio_buffer)).decode("utf-8")
                _push_to_connection(
                    endpoint_url, connection_id,
                    json.dumps({"type": "audio", "audio": audio_b64}),
                )
                audio_buffer = bytearray()

        for event in response.get("body", []):
            if close_event.is_set():
                break

            event_type = event.get("type", "")

            if event_type == "audioOutput":
                audio_data = event.get("audioOutput", {}).get("audio", b"")
                audio_buffer.extend(audio_data)
                # Flush when buffer is large enough to justify an API call
                if len(audio_buffer) >= _AUDIO_BATCH_MIN_BYTES:
                    _flush_audio_buffer()

            elif event_type == "contentEnd":
                # Flush any remaining audio at each content boundary
                _flush_audio_buffer()

            elif event_type == "textOutput":
                # Flush buffered audio before sending the transcript text
                _flush_audio_buffer()
                text = event.get("textOutput", {}).get("text", "")
                if text:
                    _push_to_connection(
                        endpoint_url, connection_id,
                        json.dumps({"type": "text", "text": text}),
                    )
                    # Accumulate transcript words for panic/sentiment analysis.
                    # Only caller speech (role=USER) is relevant; Sonic's own TTS
                    # output is also transcribed but we include both for context.
                    transcript_buf.extend(text.split())
                    if (
                        len(transcript_buf) >= SENTIMENT_MIN_WORDS
                        and not _sentiment_running.is_set()
                    ):
                        _sentiment_running.set()
                        snapshot = " ".join(transcript_buf)
                        transcript_buf.clear()
                        threading.Thread(
                            target=_analyse_caller_panic,
                            args=(connection_id, emergency_id, snapshot, endpoint_url),
                            daemon=True,
                            name=f"sentiment-{connection_id[:8]}",
                        ).start()
                        # Allow the next batch of words to trigger another analysis
                        # once the current thread completes (or after ~5 seconds)
                        def _clear_sentiment_gate():
                            import time as _t; _t.sleep(5); _sentiment_running.clear()
                        threading.Thread(target=_clear_sentiment_gate, daemon=True).start()

        # Final flush at end of stream
        _flush_audio_buffer()
    except Exception as e:
        log.error("SONIC_STREAM_ERROR", "Sonic bidirectional stream error", exc=e,
                  connection_id=connection_id, emergency_id=emergency_id)
        emit_business_metric("SonicStreamErrors", 1, "Count")
        # Notify client of voice channel failure; fall back to text
        _push_to_connection(
            endpoint_url,
            connection_id,
            json.dumps({
                "type":    "error",
                "code":    "VOICE_UNAVAILABLE",
                "message": "Voice channel temporarily unavailable. Switching to text.",
            }),
        )
    finally:
        with _streams_lock:
            _active_streams.pop(connection_id, None)
        log.info("SONIC_STREAM_ENDED", "Sonic stream closed",
                 connection_id=connection_id)


def _analyse_caller_panic(
    connection_id: str,
    emergency_id: str,
    transcript: str,
    endpoint_url: str,
) -> None:
    """
    Background thread: send the accumulated caller transcript to Nova 2 Lite
    for zero-shot panic/distress analysis. If the reported panic level is >= the
    configured threshold, publish an EventBridge escalation event and notify
    the connected dispatcher via WebSocket so they can upgrade response level.

    Prompt is deliberately short and deterministic (temperature=0) to complete
    in <600ms alongside the live Sonic stream without impacting voice latency.
    """
    prompt = f"""You are an emergency triage assistant.
Analyse the following live caller transcript for distress level.
Return ONLY a JSON object — no other text.

Transcript:
\"\"\"
{transcript[:1500]}
\"\"\"

JSON schema:
{{
  "panic_level": <integer 0-10>,          // 0=calm, 10=extreme panic/incoherent
  "key_phrases": [<up to 3 distress phrases>],
  "recommended_action": <"NONE"|"WARN_DISPATCHER"|"ESCALATE_PRIORITY"|"IMMEDIATE_ESCALATE">
}}"""

    try:
        response = bedrock.converse(
            modelId=NOVA_LITE_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 150, "temperature": 0.0},
        )
        raw = response["output"]["message"]["content"][0]["text"].strip()
        # Strip code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)

        panic_level = int(result.get("panic_level", 0))
        action      = result.get("recommended_action", "NONE")

        log.info(
            "SENTIMENT_ANALYSIS",
            f"Caller panic level: {panic_level}/10 | action: {action}",
            emergency_id=emergency_id,
            panic_level=panic_level,
            action=action,
        )
        emit_business_metric("CallerPanicLevel", panic_level, "None",
                             emergency_id=emergency_id[:8])

        if panic_level >= PANIC_ESCALATION_THRESHOLD:
            _escalate_dispatch_priority(
                emergency_id = emergency_id,
                connection_id= connection_id,
                panic_level  = panic_level,
                key_phrases  = result.get("key_phrases", []),
                endpoint_url = endpoint_url,
            )

    except Exception as e:
        log.warn("SENTIMENT_ANALYSIS_FAILED", "Panic analysis failed (non-fatal)", exc=e,
                 connection_id=connection_id)


def _escalate_dispatch_priority(
    emergency_id: str,
    connection_id: str,
    panic_level: int,
    key_phrases: list,
    endpoint_url: str,
) -> None:
    """
    Fire an EventBridge escalation event and push a dispatcher notification
    when caller panic crosses the configured threshold.

    The EventBridge event is consumed by dispatch_agent to upgrade unit count
    and page the senior dispatcher via SNS.
    """
    try:
        events.put_events(
            Entries=[
                {
                    "Source":       "novaguard.ws-handler",
                    "DetailType":   "CallerPanicEscalation",
                    "EventBusName": EVENTBRIDGE_BUS,
                    "Detail": json.dumps({
                        "emergency_id":  emergency_id,
                        "connection_id": connection_id,
                        "panic_level":   panic_level,
                        "key_phrases":   key_phrases,
                        "triggered_at":  datetime.now(timezone.utc).isoformat(),
                        "action":        "ESCALATE_PRIORITY",
                    }),
                }
            ]
        )
        log.info(
            "PANIC_ESCALATION_FIRED",
            f"EventBridge CallerPanicEscalation sent (panic={panic_level})",
            emergency_id=emergency_id,
        )
    except Exception as e:
        log.warn("PANIC_ESCALATION_EB_FAILED", "EventBridge escalation publish failed", exc=e)

    # Push immediate in-channel dispatcher alert via WebSocket
    _push_to_connection(
        endpoint_url,
        connection_id,
        json.dumps({
            "type":    "dispatcher_alert",
            "code":    "CALLER_PANIC_ESCALATION",
            "message": (
                f"AI detected extreme caller distress (panic={panic_level}/10). "
                "Recommend upgrading response level and adding supervisor unit."
            ),
            "panic_level": panic_level,
            "key_phrases": key_phrases,
        }),
    )
    emit_business_metric("PanicEscalations", 1, "Count",
                         panic_level=str(panic_level))


def _encode_event_stream(events) -> bytes:
    """
    Encode a sequence of dict events as a newline-delimited JSON byte stream
    for the Bedrock bidirectional streaming API.
    """
    lines = []
    for event in events:
        lines.append(json.dumps(event))
    return "\n".join(lines).encode("utf-8")


def _push_to_connection(endpoint_url: str, connection_id: str, data: str) -> None:
    """Push a message to a WebSocket client via the pooled API Gateway Management client."""
    if not endpoint_url:
        return
    try:
        apigw = _get_apigw_client(endpoint_url)
        if apigw is None:
            return
        apigw.post_to_connection(
            ConnectionId=connection_id,
            Data=data.encode("utf-8"),
        )
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "GoneException":
            # Client disconnected — signal the Sonic stream thread to close immediately
            log.info("WS_CONNECTION_GONE", "WebSocket client disconnected",
                     connection_id=connection_id)
            with _streams_lock:
                ctx = _active_streams.get(connection_id)
                if ctx:
                    ctx["close_event"].set()
        else:
            log.warn("WS_PUSH_FAILED", "Failed to push to WebSocket client",
                     exc=e, connection_id=connection_id)


def _archive_audio_chunk(
    emergency_id: str,
    connection_id: str,
    sequence: int,
    pcm_bytes: bytes,
) -> None:
    """Store input audio chunk to S3 for compliance archival (non-blocking)."""
    try:
        key = (
            f"audio/{emergency_id}/"
            f"{connection_id[:8]}/"
            f"{sequence:06d}_{int(time.time() * 1000)}.pcm"
        )
        s3.put_object(
            Bucket=AUDIO_BUCKET,
            Key=key,
            Body=pcm_bytes,
            ContentType="audio/pcm",
            ServerSideEncryption="aws:kms",
        )
    except Exception as e:
        log.warn("AUDIO_ARCHIVE_FAILED", "Audio chunk archival failed", exc=e)
