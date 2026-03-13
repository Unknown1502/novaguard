"""
NovaGuard Nova Sonic Transcription Lambda — Smithy SDK Edition
==============================================================
Accepts base64-encoded audio (WAV or raw PCM) from a deaf/HoH caller's device,
transcribes it using Amazon Nova Sonic (bidirectional HTTP/2 streaming),
and returns the transcript text for the triage pipeline.

Uses the official aws_sdk_bedrock_runtime (Smithy-based) Python SDK
which properly exposes invoke_model_with_bidirectional_stream. This is
the ONLY correct Python approach — boto3 does not expose this operation.

Reference: github.com/aws-samples/amazon-nova-samples/tree/main/speech-to-speech

API:
  POST /transcribe
  Body: {
    "audio_b64":    "<base64 WAV or PCM 16kHz mono 16-bit>",
    "audio_format": "wav" | "pcm"  (default: auto-detect),
    "emergency_id": "<optional>",
    "language":     "en-US"  (default)
  }

Response:
  {
    "emergency_id": "...",
    "transcript":   "...",
    "model":        "amazon.nova-sonic-v1:0",
    "sonic_used":   true,
    "_meta":        { "sonic_latency_ms": 1234, "sonic_used": true, ... }
  }

Nova Sonic event stream protocol (aws_sdk_bedrock_runtime / Smithy):
  sessionStart → promptStart → contentStart(SYSTEM,TEXT) → textInput →
  contentEnd → contentStart(USER,AUDIO) → audioInput* → contentEnd →
  promptEnd → sessionEnd
  Output: textOutput events with role="USER" contain STT transcript chunks.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import time
import uuid
from datetime import datetime, timezone

import boto3

# Smithy SDK — the ONLY Python way to call Nova Sonic bidirectional stream  
from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.models import (
    InvokeModelWithBidirectionalStreamInputChunk,
    BidirectionalInputPayloadPart,
)
from aws_sdk_bedrock_runtime.config import Config
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION         = os.environ.get("REGION", "us-east-1")
SONIC_MODEL_ID = os.environ.get("NOVA_SONIC_MODEL_ID", "amazon.nova-sonic-v1:0")
TABLE_NAME     = os.environ.get("EMERGENCIES_TABLE",    "novaguard-emergencies")

dynamo = boto3.resource("dynamodb", region_name=REGION)

SONIC_SYSTEM_PROMPT = (
    "You are a 911 emergency transcription assistant. "
    "Transcribe the caller's speech exactly and completely. "
    "Output ONLY the verbatim transcript — no commentary, no summaries."
)


# ──────────────────────────────────────────────────────────────────────
# WAV parsing helpers
# ──────────────────────────────────────────────────────────────────────

def _parse_wav(data: bytes) -> tuple[bytes, int]:
    """
    Extract raw PCM bytes and sample rate from a WAV file.
    Returns (pcm_bytes, sample_rate_hz).
    Raises ValueError if not a valid PCM WAV.
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("Not a valid WAV file")
    # Walk chunks to find fmt and data
    pos = 12
    sample_rate = 16000
    pcm_bytes = None
    while pos + 8 <= len(data):
        chunk_id   = data[pos:pos+4]
        chunk_size = struct.unpack_from("<I", data, pos+4)[0]
        pos += 8
        if chunk_id == b"fmt ":
            audio_format = struct.unpack_from("<H", data, pos)[0]
            if audio_format != 1:
                raise ValueError(f"Unsupported WAV format {audio_format} (only PCM=1 supported)")
            sample_rate = struct.unpack_from("<I", data, pos+4)[0]
        elif chunk_id == b"data":
            pcm_bytes = data[pos:pos+chunk_size]
        pos += chunk_size
    if pcm_bytes is None:
        raise ValueError("WAV file has no data chunk")
    return pcm_bytes, sample_rate


def _decode_audio(audio_b64: str, audio_format: str = "auto") -> tuple[bytes, int]:
    """
    Decode base64 audio → (raw_pcm_bytes, sample_rate_hz).
    Auto-detects WAV vs raw PCM via the RIFF magic bytes.
    """
    raw = base64.b64decode(audio_b64)
    fmt = audio_format.lower() if audio_format else "auto"
    if fmt == "pcm":
        return raw, 16000
    # Auto-detect or explicit WAV
    if fmt == "wav" or (fmt == "auto" and raw[:4] == b"RIFF"):
        try:
            return _parse_wav(raw)
        except ValueError as e:
            logger.warning("WAV parse failed (%s) — treating as raw PCM", e)
    return raw, 16000  # fall through: assume raw 16kHz PCM


# ──────────────────────────────────────────────────────────────────────
# Smithy SDK Nova Sonic async coroutine
# ──────────────────────────────────────────────────────────────────────

async def _transcribe_sonic_async(audio_bytes: bytes, sample_rate: int = 16000) -> tuple[str, int]:
    """
    Stream audio through Nova Sonic using the Smithy-based aws_sdk_bedrock_runtime client.
    This is the ONLY correct Python implementation — boto3 does NOT expose this API.

    Returns (transcript, latency_ms).
    Raises on any Sonic or connection error.
    """
    # ── Smithy client — EnvironmentCredentialsResolver reads the IAM role
    #    creds injected by Lambda as AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
    #    AWS_SESSION_TOKEN environment variables.
    config = Config(
        endpoint_uri=f"https://bedrock-runtime.{REGION}.amazonaws.com",
        region=REGION,
        aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
    )
    client = BedrockRuntimeClient(config=config)

    prompt_name        = str(uuid.uuid4())
    system_content_name = str(uuid.uuid4())
    audio_content_name  = str(uuid.uuid4())
    t0                 = time.time()
    parts: list[str]   = []
    session_ended      = asyncio.Event()

    # ── Helper: wrap JSON event → Smithy input chunk
    def _make_chunk(nova_event: dict) -> InvokeModelWithBidirectionalStreamInputChunk:
        return InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(
                bytes_=json.dumps(nova_event).encode("utf-8")
            )
        )

    # ── Open bidirectional stream
    stream = await client.invoke_model_with_bidirectional_stream(
        InvokeModelWithBidirectionalStreamOperationInput(model_id=SONIC_MODEL_ID)
    )

    # ── Background task: collect text output events
    async def _read_responses():
        try:
            while not session_ended.is_set():
                try:
                    output = await asyncio.wait_for(stream.await_output(), timeout=75)
                    result = await output[1].receive()
                except asyncio.TimeoutError:
                    logger.warning("[SONIC] await_output() timed out — ending read loop")
                    break
                except Exception:
                    break  # stream closed

                if result is None or not (result.value and result.value.bytes_):
                    continue

                try:
                    json_data = json.loads(result.value.bytes_.decode("utf-8"))
                except Exception:
                    continue

                event = json_data.get("event", json_data)

                # STT transcript
                if "textOutput" in event:
                    text_out = event["textOutput"]
                    role     = text_out.get("role", "")
                    content  = text_out.get("content", "")
                    if role == "USER" and content:
                        parts.append(content)
                        logger.info("[SONIC] STT chunk: %r", content[:80])

                # Session lifecycle end
                if "sessionEnd" in event or "completionEnd" in event:
                    logger.info("[SONIC] Session ended by Sonic")
                    break

                # Hard errors
                for err_key in ("internalServerException", "modelStreamErrorException",
                                "validationException", "throttlingException",
                                "serviceUnavailableException"):
                    if err_key in event:
                        msg = event[err_key].get("message", str(event[err_key]))
                        raise RuntimeError(f"Nova Sonic {err_key}: {msg}")

        except asyncio.CancelledError:
            pass
        finally:
            session_ended.set()

    # Start reader task BEFORE sending events
    reader_task = asyncio.create_task(_read_responses())

    # ── Send events
    async def send(nova_event: dict):
        await stream.input_stream.send(_make_chunk(nova_event))

    # sessionStart
    await send({"event": {"sessionStart": {
        "inferenceConfiguration": {"maxTokens": 1024, "topP": 0.9, "temperature": 0.0},
    }}})

    # promptStart — text output ONLY (STT mode: no audioOutputConfiguration)
    await send({"event": {"promptStart": {
        "promptName": prompt_name,
        "textOutputConfiguration": {"mediaType": "text/plain"},
    }}})

    # System message block
    await send({"event": {"contentStart": {
        "promptName":            prompt_name,
        "contentName":           system_content_name,
        "type":                  "TEXT",
        "interactive":           False,
        "role":                  "SYSTEM",
        "textInputConfiguration": {"mediaType": "text/plain"},
    }}})
    await send({"event": {"textInput": {
        "promptName":  prompt_name,
        "contentName": system_content_name,
        "content":     SONIC_SYSTEM_PROMPT,
    }}})
    await send({"event": {"contentEnd": {
        "promptName":  prompt_name,
        "contentName": system_content_name,
    }}})

    # Audio block (USER role)
    await send({"event": {"contentStart": {
        "promptName":            prompt_name,
        "contentName":           audio_content_name,
        "type":                  "AUDIO",
        "interactive":           True,
        "role":                  "USER",
        "audioInputConfiguration": {
            "mediaType":       "audio/lpcm",
            "sampleRateHertz": sample_rate,
            "sampleSizeBits":  16,
            "channelCount":    1,
            "audioType":       "SPEECH",
            "encoding":        "base64",
        },
    }}})

    # Stream audio in 100ms chunks
    CHUNK = int(sample_rate * 0.1) * 2  # 100ms frames × 2 bytes/sample
    for i in range(0, len(audio_bytes), CHUNK):
        chunk = audio_bytes[i:i + CHUNK]
        await send({"event": {"audioInput": {
            "promptName":  prompt_name,
            "contentName": audio_content_name,
            "content":     base64.b64encode(chunk).decode("utf-8"),
        }}})
        await asyncio.sleep(0)  # yield to let reader task run

    # End audio → prompt → session
    await send({"event": {"contentEnd": {
        "promptName":  prompt_name,
        "contentName": audio_content_name,
    }}})
    await send({"event": {"promptEnd":  {"promptName": prompt_name}}})
    await send({"event": {"sessionEnd": {}}})

    # Close input stream — signals Sonic we're done
    await stream.input_stream.close()

    # Wait for reader task (with timeout)
    try:
        await asyncio.wait_for(reader_task, timeout=70)
    except asyncio.TimeoutError:
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass

    transcript = " ".join(parts).strip()
    latency_ms = int((time.time() - t0) * 1000)
    logger.info("[NOVAGUARD SONIC LIVE] model=%s latency=%dms transcript_chars=%d",
                SONIC_MODEL_ID, latency_ms, len(transcript))
    return transcript, latency_ms


def _transcribe_sonic(audio_bytes: bytes, sample_rate: int = 16000) -> tuple[str, int, bool]:
    """
    Sync wrapper — runs the async Smithy SDK call via asyncio.run().
    Returns (transcript, latency_ms, sonic_used=True).
    Raises on any error (caller handles fallback reporting).
    """
    transcript, latency_ms = asyncio.run(
        asyncio.wait_for(
            _transcribe_sonic_async(audio_bytes, sample_rate),
            timeout=85,
        )
    )
    return transcript, latency_ms, True


# ──────────────────────────────────────────────────────────────────────
# Lambda entry point
# ──────────────────────────────────────────────────────────────────────

def handler(event, context):
    t0 = time.time()

    if event.get("httpMethod", "").upper() == "GET":
        return _resp(200, {
            "service": "NovaGuard Nova Sonic Transcription",
            "model":   SONIC_MODEL_ID,
            "status":  "operational",
            "usage":   "POST { audio_b64: '<base64 PCM 16kHz mono 16-bit>' }",
        })

    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event.get("body", event)
    audio_b64    = (body.get("audio_b64") or "").strip()
    audio_format = body.get("audio_format", "auto")
    emergency_id = body.get("emergency_id") or str(uuid.uuid4())

    if not audio_b64:
        return _resp(400, {"error": "Missing required field: audio_b64 (base64-encoded WAV or PCM audio)"})

    # ── Decode audio (WAV auto-detected or explicit format) ─────────
    try:
        audio_bytes, sample_rate = _decode_audio(audio_b64, audio_format)
    except Exception as e:
        return _resp(400, {"error": f"Invalid audio input: {e}"})

    logger.info("[SONIC] Start | emergency_id=%s | audio_bytes=%d | sample_rate=%dHz",
                emergency_id, len(audio_bytes), sample_rate)

    sonic_used  = False
    sonic_error = None
    transcript  = ""
    latency_ms  = 0

    # ── Real Nova Sonic bidirectional streaming (Smithy SDK) ─────────
    try:
        transcript, latency_ms, sonic_used = _transcribe_sonic(audio_bytes, sample_rate)
        logger.info(
            "[NOVAGUARD SONIC LIVE] model=%s emergency_id=%s latency=%dms "
            "transcript_chars=%d sonic_used=True",
            SONIC_MODEL_ID, emergency_id, latency_ms, len(transcript),
        )
    except Exception as e:
        sonic_error = str(e)
        logger.warning(
            "[SONIC UNAVAILABLE] Nova Sonic failed — NOT falling back to Lite. "
            "emergency_id=%s error=%s", emergency_id, sonic_error,
        )

    # ── No silent fallback. Be honest. ───────────────────────────────
    if not sonic_used:
        # Return the Sonic error transparently  
        # The caller can still get a transcript by posting to /triage (text)
        transcript  = ""
        used_model  = "none"
        db_status   = "skipped"
        total_ms    = int((time.time() - t0) * 1000)
        return _resp(503, {
            "emergency_id":  emergency_id,
            "transcript":    "",
            "model":         SONIC_MODEL_ID,
            "sonic_used":    False,
            "error":         "Nova Sonic temporarily unavailable",
            "sonic_error":   sonic_error,
            "suggestion":    "Check CloudWatch logs for the Smithy SDK error. Ensure model access for amazon.nova-sonic-v1:0 in Bedrock console (us-east-1).",
            "_meta": {
                "sonic_latency_ms": 0,
                "total_ms":         total_ms,
                "sonic_used":       False,
                "model_attempted":  SONIC_MODEL_ID,
            },
        })

    # ── Sonic succeeded — write to DynamoDB ──────────────────────────
    transcript = transcript or "[empty — no speech detected in audio]"
    now        = datetime.now(timezone.utc).isoformat()
    try:
        dynamo.Table(TABLE_NAME).put_item(Item={
            "emergency_id":     emergency_id,
            "version":          0,
            "status":           "AUDIO_TRANSCRIBED",
            "input_channel":    "VOICE_NOVA_SONIC",
            "transcript":       transcript,
            "audio_bytes":      len(audio_bytes),
            "sonic_latency_ms": latency_ms,
            "nova_model":       SONIC_MODEL_ID,
            "created_at":       now,
            "updated_at":       now,
            "ttl_epoch":        int(time.time()) + 72 * 3600,
        })
        db_status = "written"
    except Exception as e:
        logger.error("[SONIC] DynamoDB write failed: %s", e)
        db_status = f"failed: {e}"

    return _resp(200, {
        "emergency_id":  emergency_id,
        "transcript":    transcript,
        "input_channel": "VOICE_NOVA_SONIC",
        "model":         SONIC_MODEL_ID,
        "sonic_used":    True,
        "_meta": {
            "sonic_latency_ms": latency_ms,
            "total_ms":         int((time.time() - t0) * 1000),
            "audio_bytes":      len(audio_bytes),
            "sonic_used":       True,
            "db_status":        db_status,
            "timestamp":        now,
        },
        "next_step": "POST /pipeline with this transcript as the description field",
    })


def _resp(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body),
    }
