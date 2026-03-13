"""
NovaGuard — Nova Sonic Responder Briefing Agent
================================================
Step 4 in the emergency pipeline (after Triage → Dispatch → Comms).

Generates a radio-optimized audio briefing for first responders using
Nova 2 Lite, then attempts Nova Sonic TTS to produce spoken audio.

This is the "voice bridge" — the deaf caller's text emergency is
transformed into a professional audio briefing that responders hear
over radio. Nova Sonic handles the text-to-speech delivery.

Route: POST /sonic-briefing

Input (from SFN pipeline — receives comms output):
  {
    "emergency_id": "...",
    "severity_score": 95,
    "emergency_type": "MEDICAL",
    "triage_narrative": "...",
    "responder_briefing": "...",
    "caller_instructions": [...],
    "units_dispatched": [...]
  }

Output:
  {
    "emergency_id": "...",
    "briefing_text": "All units respond: MEDICAL...",
    "sonic_tts_used": true,
    "audio_b64": "<base64 PCM audio>",
    "model": "amazon.nova-sonic-v1:0",
    ...
  }
"""
from __future__ import annotations

import asyncio
import base64
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

REGION         = os.environ.get("REGION", "us-east-1")
MODEL_ID       = os.environ.get("NOVA_LITE_MODEL_ID", "us.amazon.nova-lite-v1:0")
SONIC_MODEL_ID = os.environ.get("NOVA_SONIC_MODEL_ID", "amazon.nova-sonic-v1:0")
TABLE_NAME     = os.environ.get("EMERGENCIES_TABLE", "novaguard-emergencies")

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
dynamo  = boto3.resource("dynamodb", region_name=REGION)

# -- Smithy SDK for Nova Sonic TTS --
SONIC_AVAILABLE = False
try:
    from aws_sdk_bedrock_runtime.client import (
        BedrockRuntimeClient,
        InvokeModelWithBidirectionalStreamOperationInput,
    )
    from aws_sdk_bedrock_runtime.models import (
        InvokeModelWithBidirectionalStreamInputChunk,
        BidirectionalInputPayloadPart,
    )
    from aws_sdk_bedrock_runtime.config import Config as SmithyConfig
    from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver
    SONIC_AVAILABLE = True
    logger.info("[SONIC BRIEFING] Smithy SDK loaded — TTS available")
except Exception as e:
    logger.warning("[SONIC BRIEFING] Smithy SDK not available: %s", e)

BRIEFING_SYSTEM_PROMPT = """You are a radio-ready emergency dispatch assistant.

Given an emergency triage report, generate a PROFESSIONAL RADIO BRIEFING that will
be spoken aloud to first responders via Nova Sonic text-to-speech.

FORMAT RULES (critical for audio delivery):
- Keep the briefing under 150 words
- Use short, clear sentences (max 15 words each)
- Spell out abbreviations on first use
- Use "minutes" not "min", "approximately" not "approx"
- Include PHONETIC spelling for confusing terms: "Alpha Lima Sierra" not "ALS"
- Lead with incident severity and type
- Include caller communication method (deaf/HoH — text only)
- End with any special hazards or instructions
- No bullet points or formatting — pure spoken text

Return ONLY valid JSON:
{
  "radio_briefing": "<the spoken briefing text>",
  "briefing_word_count": <int>,
  "priority_level": "<IMMEDIATE|URGENT|ROUTINE>",
  "key_facts": ["<fact1>", "<fact2>", "<fact3>"]
}"""


SONIC_TTS_PROMPT = (
    "You are a professional 911 radio dispatcher. "
    "Read the following emergency briefing aloud, clearly and calmly. "
    "Speak at a moderate pace suitable for radio transmission. "
    "Read EXACTLY what is provided — no extra commentary."
)


# ──────────────────────────────────────────────────────────────────
# Nova Sonic TTS (Text → Audio)
# ──────────────────────────────────────────────────────────────────

async def _tts_sonic_async(briefing_text: str) -> tuple[bytes, str, int]:
    """
    Use Nova Sonic in text-to-speech mode to generate audio from the briefing.
    Sends text input (USER role), receives audio output.
    Returns (audio_bytes, spoken_text_echo, latency_ms).
    """
    config = SmithyConfig(
        endpoint_uri=f"https://bedrock-runtime.{REGION}.amazonaws.com",
        region=REGION,
        aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
    )
    client = BedrockRuntimeClient(config=config)

    prompt_name         = str(uuid.uuid4())
    system_content_name = str(uuid.uuid4())
    user_content_name   = str(uuid.uuid4())
    t0 = time.time()

    audio_chunks: list[bytes] = []
    text_parts: list[str] = []
    session_ended = asyncio.Event()

    def _make_chunk(nova_event: dict):
        return InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(
                bytes_=json.dumps(nova_event).encode("utf-8")
            )
        )

    stream = await client.invoke_model_with_bidirectional_stream(
        InvokeModelWithBidirectionalStreamOperationInput(model_id=SONIC_MODEL_ID)
    )

    async def _read_responses():
        try:
            while not session_ended.is_set():
                try:
                    output = await asyncio.wait_for(stream.await_output(), timeout=60)
                    result = await output[1].receive()
                except asyncio.TimeoutError:
                    break
                except Exception:
                    break

                if result is None or not (result.value and result.value.bytes_):
                    continue

                try:
                    json_data = json.loads(result.value.bytes_.decode("utf-8"))
                except Exception:
                    continue

                event_data = json_data.get("event", json_data)

                # Collect audio output chunks
                if "audioOutput" in event_data:
                    content = event_data["audioOutput"].get("content", "")
                    if content:
                        audio_chunks.append(base64.b64decode(content))

                # Collect text output (echo)
                if "textOutput" in event_data:
                    txt = event_data["textOutput"]
                    role = txt.get("role", "")
                    content = txt.get("content", "")
                    if role == "ASSISTANT" and content:
                        text_parts.append(content)

                if "sessionEnd" in event_data or "completionEnd" in event_data:
                    break

                for err_key in ("internalServerException", "modelStreamErrorException",
                                "validationException", "throttlingException"):
                    if err_key in event_data:
                        msg = event_data[err_key].get("message", str(event_data[err_key]))
                        raise RuntimeError(f"Sonic TTS error: {err_key}: {msg}")
        except asyncio.CancelledError:
            pass
        finally:
            session_ended.set()

    reader_task = asyncio.create_task(_read_responses())

    async def send(nova_event: dict):
        await stream.input_stream.send(_make_chunk(nova_event))

    # 1. sessionStart
    await send({"event": {"sessionStart": {
        "inferenceConfiguration": {"maxTokens": 1024, "topP": 0.9, "temperature": 0.7},
    }}})

    # 2. promptStart — request BOTH text + audio output
    await send({"event": {"promptStart": {
        "promptName": prompt_name,
        "textOutputConfiguration": {"mediaType": "text/plain"},
        "audioOutputConfiguration": {
            "mediaType": "audio/lpcm",
            "sampleRateHertz": 24000,
            "sampleSizeBits": 16,
            "channelCount": 1,
            "voiceId": "matthew",
            "encoding": "base64",
        },
    }}})

    # 3. System prompt
    await send({"event": {"contentStart": {
        "promptName": prompt_name,
        "contentName": system_content_name,
        "type": "TEXT",
        "interactive": False,
        "role": "SYSTEM",
        "textInputConfiguration": {"mediaType": "text/plain"},
    }}})
    await send({"event": {"textInput": {
        "promptName": prompt_name,
        "contentName": system_content_name,
        "content": SONIC_TTS_PROMPT,
    }}})
    await send({"event": {"contentEnd": {
        "promptName": prompt_name,
        "contentName": system_content_name,
    }}})

    # 4. User content — the briefing text to vocalize
    await send({"event": {"contentStart": {
        "promptName": prompt_name,
        "contentName": user_content_name,
        "type": "TEXT",
        "interactive": False,
        "role": "USER",
        "textInputConfiguration": {"mediaType": "text/plain"},
    }}})
    await send({"event": {"textInput": {
        "promptName": prompt_name,
        "contentName": user_content_name,
        "content": f"Read this emergency briefing aloud:\n\n{briefing_text}",
    }}})
    await send({"event": {"contentEnd": {
        "promptName": prompt_name,
        "contentName": user_content_name,
    }}})

    # 5. End
    await send({"event": {"promptEnd": {"promptName": prompt_name}}})
    await send({"event": {"sessionEnd": {}}})
    await stream.input_stream.close()

    try:
        await asyncio.wait_for(reader_task, timeout=60)
    except asyncio.TimeoutError:
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass

    audio_bytes = b"".join(audio_chunks)
    spoken_text = " ".join(text_parts).strip()
    latency_ms = int((time.time() - t0) * 1000)

    logger.info("[SONIC TTS] audio=%d bytes, text=%d chars, latency=%dms",
                len(audio_bytes), len(spoken_text), latency_ms)

    return audio_bytes, spoken_text, latency_ms


def _tts_sonic_sync(briefing_text: str) -> tuple[bytes, str, int, bool]:
    """Sync wrapper for Sonic TTS. Returns (audio, text, latency_ms, success).
    Falls back to Amazon Polly if Smithy SDK is unavailable."""
    # Try Nova Sonic first
    if SONIC_AVAILABLE:
        try:
            audio, text, ms = asyncio.run(
                asyncio.wait_for(_tts_sonic_async(briefing_text), timeout=70)
            )
            if audio:
                return audio, text, ms, True
        except Exception as e:
            logger.warning("[SONIC TTS] Nova Sonic failed, trying Polly fallback: %s", e)

    # Fallback: Amazon Polly Neural TTS
    try:
        t0 = time.time()
        polly = boto3.client("polly", region_name=REGION)
        response = polly.synthesize_speech(
            Text=briefing_text[:3000],  # Polly limit
            OutputFormat="pcm",
            VoiceId="Matthew",
            Engine="neural",
            SampleRate="16000",
        )
        audio_bytes = response["AudioStream"].read()
        latency_ms = int((time.time() - t0) * 1000)
        logger.info("[SONIC TTS] Amazon Polly fallback succeeded | audio=%d bytes | latency=%dms",
                    len(audio_bytes), latency_ms)
        return audio_bytes, briefing_text, latency_ms, True
    except Exception as polly_err:
        logger.warning("[SONIC TTS] Polly fallback also failed: %s", polly_err)
        return b"", "", 0, False


# ──────────────────────────────────────────────────────────────────
# Nova Lite — Generate radio-optimized briefing text
# ──────────────────────────────────────────────────────────────────

def _generate_briefing_text(data: dict) -> tuple[dict, int]:
    """Use Nova 2 Lite to generate a radio-ready briefing from triage data."""
    t0 = time.time()

    severity      = data.get("severity_score", 50)
    etype         = data.get("emergency_type", "UNKNOWN")
    narrative     = data.get("triage_narrative", data.get("responder_briefing", "Emergency reported"))
    units         = data.get("units_dispatched", [])
    location      = data.get("caller_location", "Location not specified")
    instructions  = data.get("caller_instructions", [])

    if isinstance(units, list):
        units_str = "; ".join(
            f"{u.get('unit_type', 'Unit')} (ETA {u.get('eta_minutes', '?')} minutes)"
            for u in units
        ) if units else "units being dispatched"
    else:
        units_str = str(units)

    user_prompt = (
        f"GENERATE RADIO BRIEFING FOR FIRST RESPONDERS\n\n"
        f"Emergency Type: {etype}\n"
        f"Severity Score: {severity}/100\n"
        f"Triage Narrative: {narrative}\n"
        f"Location: {location}\n"
        f"Units Dispatched: {units_str}\n"
        f"Caller Type: DEAF/HARD-OF-HEARING — TEXT COMMUNICATION ONLY\n\n"
        f"Generate the radio briefing now."
    )

    try:
        resp = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": BRIEFING_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            inferenceConfig={"maxTokens": 400, "temperature": 0.2, "topP": 0.9},
        )
    except ClientError as e:
        logger.error("[SONIC BRIEFING] Nova Lite error: %s", e)
        latency = int((time.time() - t0) * 1000)
        return {
            "radio_briefing": (
                f"All units respond. {etype} incident, severity {severity} out of 100. "
                f"{narrative}. Location: {location}. {units_str}. "
                f"Caller is deaf or hard of hearing. Text communication only. "
                f"Approach with visual signals."
            ),
            "briefing_word_count": 40,
            "priority_level": "IMMEDIATE" if severity >= 80 else "URGENT",
            "key_facts": [etype, f"severity {severity}", "deaf/HoH caller"],
            "_fallback": True,
        }, latency

    latency = int((time.time() - t0) * 1000)
    output_text = resp["output"]["message"]["content"][0]["text"]
    usage = resp["usage"]

    logger.info("[SONIC BRIEFING] Nova Lite gen | in=%d out=%d latency=%dms",
                usage.get("inputTokens", 0), usage.get("outputTokens", 0), latency)

    cleaned = output_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").lstrip("json").strip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        result = {
            "radio_briefing": cleaned,
            "briefing_word_count": len(cleaned.split()),
            "priority_level": "IMMEDIATE" if severity >= 80 else "URGENT",
            "key_facts": [etype, f"severity {severity}"],
        }

    return result, latency


# ──────────────────────────────────────────────────────────────────
# Lambda Handler
# ──────────────────────────────────────────────────────────────────

def handler(event, context):
    t0 = time.time()

    # GET — endpoint info
    if event.get("httpMethod", "").upper() == "GET":
        return _resp(200, {
            "service": "NovaGuard Sonic Responder Briefing",
            "model": SONIC_MODEL_ID,
            "description": (
                "Generates audio-optimized responder briefings via Nova 2 Lite, "
                "then delivers them as spoken audio via Nova Sonic TTS. "
                "This is the voice bridge — deaf caller's text emergency → "
                "professional audio briefing for first responders."
            ),
            "pipeline_step": 4,
            "models_used": [MODEL_ID, SONIC_MODEL_ID],
            "sonic_tts_available": SONIC_AVAILABLE,
            "status": "operational",
        })

    # Parse body
    if isinstance(event.get("body"), str):
        body = json.loads(event["body"])
    elif isinstance(event.get("body"), dict):
        body = event["body"]
    else:
        body = event

    emergency_id = body.get("emergency_id", str(uuid.uuid4()))

    logger.info("[SONIC BRIEFING] Start | emergency_id=%s", emergency_id)

    # Step 1: Generate radio-optimized briefing text via Nova Lite
    briefing_data, lite_latency = _generate_briefing_text(body)
    briefing_text = briefing_data.get("radio_briefing", "Emergency response required.")

    logger.info("[SONIC BRIEFING] Briefing generated | words=%d | lite_ms=%d",
                len(briefing_text.split()), lite_latency)

    # Step 2: Attempt Nova Sonic TTS (text → audio)
    audio_bytes, spoken_text, sonic_latency, sonic_success = _tts_sonic_sync(briefing_text)

    if sonic_success and audio_bytes:
        logger.info("[SONIC BRIEFING] TTS succeeded | audio=%d bytes | sonic_ms=%d",
                    len(audio_bytes), sonic_latency)
    else:
        logger.info("[SONIC BRIEFING] TTS skipped or failed | sonic_available=%s", SONIC_AVAILABLE)

    total_ms = int((time.time() - t0) * 1000)

    # Step 3: Write to DynamoDB
    now = datetime.now(timezone.utc).isoformat()
    try:
        dynamo.Table(TABLE_NAME).put_item(Item={
            "emergency_id": emergency_id,
            "version": 4,
            "status": "BRIEFING_GENERATED",
            "briefing_text": briefing_text[:1000],
            "priority_level": briefing_data.get("priority_level", "URGENT"),
            "sonic_tts_success": sonic_success,
            "sonic_tts_audio_bytes": len(audio_bytes),
            "nova_lite_latency_ms": lite_latency,
            "sonic_tts_latency_ms": sonic_latency,
            "nova_models": f"{MODEL_ID},{SONIC_MODEL_ID}",
            "created_at": now,
            "updated_at": now,
            "ttl_epoch": int(time.time()) + 72 * 3600,
        })
        db_status = "written"
    except Exception as e:
        logger.error("[SONIC BRIEFING] DynamoDB failed: %s", e)
        db_status = f"failed: {e}"

    logger.info(
        "[NOVAGUARD SONIC BRIEFING] emergency_id=%s sonic_tts=%s "
        "audio_bytes=%d lite_ms=%d sonic_ms=%d total_ms=%d",
        emergency_id, sonic_success, len(audio_bytes),
        lite_latency, sonic_latency, total_ms,
    )

    # Detect SFN invocation (no httpMethod) — omit audio_b64 to stay under 256KB SFN limit
    is_sfn = "httpMethod" not in event and "requestContext" not in event
    include_audio = sonic_success and audio_bytes and not is_sfn

    result = {
        "emergency_id": emergency_id,
        "status": "BRIEFING_COMPLETE",
        "briefing_text": briefing_text,
        "priority_level": briefing_data.get("priority_level", "URGENT"),
        "key_facts": briefing_data.get("key_facts", []),
        "word_count": briefing_data.get("briefing_word_count", len(briefing_text.split())),

        # Sonic TTS output
        "sonic_tts_used": sonic_success,
        "model": SONIC_MODEL_ID,
        "models_used": [MODEL_ID, SONIC_MODEL_ID] if sonic_success else [MODEL_ID],
        "audio_b64": base64.b64encode(audio_bytes).decode() if include_audio else None,
        "audio_format": "audio/lpcm;rate=16000;bits=16;channels=1" if include_audio else None,

        "_meta": {
            "nova_lite_latency_ms": lite_latency,
            "sonic_tts_latency_ms": sonic_latency,
            "total_ms": total_ms,
            "audio_bytes": len(audio_bytes),
            "sonic_tts_available": SONIC_AVAILABLE,
            "sonic_tts_success": sonic_success,
            "db_status": db_status,
            "timestamp": now,
        },
    }

    return _wrap(event, result)


def _wrap(event: dict, result: dict) -> dict:
    """Wrap for API Gateway or direct invocation."""
    if "httpMethod" in event or "requestContext" in event:
        return _resp(200, result)
    return result


def _resp(code: int, body: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body),
    }
