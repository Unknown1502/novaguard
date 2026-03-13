#!/usr/bin/env python3
"""
NovaGuard Demo Backend — Local Flask server.

Bridges demo/index.html to real Amazon Bedrock (Nova 2 Lite).
Run this on your laptop DURING the demo recording.

Install:
    pip install flask flask-cors boto3

Run:
    python demo/backend.py
    # Server starts on http://localhost:8000

Endpoints:
    GET  /health          — confirm server + Bedrock access
    POST /triage          — synchronous triage (returns full JSON)
    POST /triage-stream   — streaming SSE triage (token-by-token typewriter)
    POST /embedding-test  — confirm Nova Multimodal Embedding model access
"""

import json
import logging
import time
import os
from typing import Generator

import boto3
from botocore.exceptions import ClientError
from flask import Flask, request, jsonify, Response, stream_with_context, send_file
from flask_cors import CORS

# ── Config ────────────────────────────────────────────────────────────────────
REGION           = os.environ.get("AWS_REGION", "us-east-1")
NOVA_LITE_MODEL  = os.environ.get("NOVA_LITE_MODEL_ID", "us.amazon.nova-lite-v1:0")
NOVA_EMBED_MODEL = os.environ.get("NOVA_EMBED_MODEL_ID", "amazon.nova-2-multimodal-embeddings-v1:0")
PORT             = int(os.environ.get("NOVAGUARD_PORT", "8000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app   = Flask(__name__)
CORS(app)  # Allow demo/index.html (file://) to call localhost

bedrock = boto3.client("bedrock-runtime", region_name=REGION)

# ── Triage system prompt (matches triage_agent/handler.py exactly) ────────────
TRIAGE_SYSTEM_PROMPT = (
    "You are NovaGuard's Emergency Triage AI, a specialized reasoning system deployed "
    "in a production emergency response platform used by professional dispatchers. "
    "Analyze the emergency report and return ONLY a valid JSON object with these fields: "
    "severity_score (integer 0-100, where 100 = immediate life threat), "
    "confidence (float 0.0-1.0), "
    "emergency_type (MEDICAL|FIRE|POLICE|NATURAL_DISASTER|TRAFFIC|HAZMAT|WELFARE_CHECK|UNKNOWN), "
    "sub_type (string), "
    "triage_narrative (string — 1-2 sentences for dispatcher display), "
    "caller_instructions (string — immediate first-aid or safety instructions to relay to caller), "
    "recommended_response_level (integer 1-5), "
    "resource_requirements (object with keys AMBULANCE, FIRE_ENGINE, POLICE_UNIT — each an integer). "
    "No markdown fences, no explanation text. Return only the raw JSON object."
)


def _build_user_message(description: str) -> list:
    return [
        {
            "role": "user",
            "content": [
                {
                    "text": (
                        "EMERGENCY REPORT — NovaGuard Triage Request\n"
                        f"Caller description: {description}"
                    )
                }
            ],
        }
    ]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Quick health check — also pings Bedrock to confirm model access."""
    try:
        # Minimal Bedrock call to confirm credentials + model access
        resp = bedrock.converse(
            modelId=NOVA_LITE_MODEL,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 5, "temperature": 0.0},
        )
        model_ok = resp["stopReason"] in ("end_turn", "max_tokens")
    except Exception as e:
        return jsonify({"status": "error", "bedrock": str(e)}), 503

    return jsonify({
        "status": "ok",
        "bedrock": "reachable",
        "model": NOVA_LITE_MODEL,
        "region": REGION,
    })


@app.route("/triage", methods=["POST"])
def triage():
    """
    Synchronous triage endpoint.
    Body: { "description": "My father fell down the stairs..." }
    Returns: { "severity_score": 91, "emergency_type": "MEDICAL", ... }

    The demo calls this on 'Run Scenario' and uses the real triage result
    to populate severity score, emergency type, units panel, etc.
    """
    body = request.get_json(silent=True) or {}
    description = (body.get("description") or "").strip()

    if not description:
        return jsonify({"error": "description is required"}), 400

    log.info("[NOVAGUARD REAL CALL] Triage requested — input: %.80s", description)
    t0 = time.time()

    try:
        response = bedrock.converse(
            modelId=NOVA_LITE_MODEL,
            system=[{"text": TRIAGE_SYSTEM_PROMPT}],
            messages=_build_user_message(description),
            inferenceConfig={
                "maxTokens": 512,
                "temperature": 0.0,
            },
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]  # type: ignore[index]
        log.error("Bedrock error: %s", e)
        return jsonify({"error": f"Bedrock error: {code}", "detail": str(e)}), 502

    latency_ms = int((time.time() - t0) * 1000)
    output_text = response["output"]["message"]["content"][0]["text"]  # type: ignore[index]

    # Parse triage JSON from Nova 2 Lite
    try:
        result = json.loads(output_text)
    except json.JSONDecodeError:
        # Nova occasionally wraps JSON in markdown fences — strip them
        cleaned = output_text.strip("` \n")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            log.warning("Nova returned non-JSON: %s", output_text[:200])
            result = {"raw_output": output_text}

    result["_meta"] = {  # type: ignore[index]
        "latency_ms": latency_ms,
        "model": NOVA_LITE_MODEL,
        "region": REGION,
        "input_tokens": response["usage"]["inputTokens"],  # type: ignore[index]
        "output_tokens": response["usage"]["outputTokens"],  # type: ignore[index]
        "stop_reason": response["stopReason"],  # type: ignore[index]
    }

    log.info(
        "[NOVAGUARD REAL CALL] Nova 2 Lite invoked — "
        "input: %.60s... — output: severity=%s type=%s — latency: %dms",
        description,
        result.get("severity_score", "?"),
        result.get("emergency_type", "?"),
        latency_ms,
    )

    return jsonify(result)


@app.route("/triage-stream", methods=["POST"])
def triage_stream():
    """
    Streaming triage endpoint using Server-Sent Events (SSE).
    Body: { "description": "..." }

    The demo connects to this as an EventSource and displays each token
    as it arrives — visually proving that a real LLM is generating text.

    SSE event format:
        data: {"token": "Priority", "done": false}\n\n
        data: {"token": " one", "done": false}\n\n
        data: {"result": {...}, "done": true}\n\n
    """
    body = request.get_json(silent=True) or {}
    description = (body.get("description") or "").strip()

    if not description:
        def error_stream():
            yield 'data: {"error": "description is required"}\n\n'
        return Response(stream_with_context(error_stream()), mimetype="text/event-stream")

    def generate() -> Generator[str, None, None]:
        log.info("[NOVAGUARD STREAM] Starting streaming triage — input: %.80s", description)
        t0 = time.time()
        full_text = ""

        try:
            stream_response = bedrock.converse_stream(
                modelId=NOVA_LITE_MODEL,
                system=[{"text": TRIAGE_SYSTEM_PROMPT}],
                messages=_build_user_message(description),
                inferenceConfig={
                    "maxTokens": 512,
                    "temperature": 0.0,
                },
            )
        except ClientError as e:
            code = e.response["Error"]["Code"]  # type: ignore[index]
            err  = json.dumps({"error": f"Bedrock error: {code}", "done": True})
            yield f"data: {err}\n\n"
            return

        # Stream tokens from Bedrock converse_stream
        for event in stream_response["stream"]:
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                token = delta.get("text", "")
                if token:
                    full_text += token
                    payload = json.dumps({"token": token, "done": False})
                    yield f"data: {payload}\n\n"

        # Parse the complete accumulated JSON response
        latency_ms = int((time.time() - t0) * 1000)
        try:
            result = json.loads(full_text)
        except json.JSONDecodeError:
            cleaned = full_text.strip("` \n")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            try:
                result = json.loads(cleaned)
            except json.JSONDecodeError:
                result = {"raw_output": full_text}

        result["_meta"] = {  # type: ignore[index]
            "latency_ms": latency_ms,
            "model": NOVA_LITE_MODEL,
            "region": REGION,
        }

        log.info(
            "[NOVAGUARD REAL CALL] Nova 2 Lite invoked — "
            "input: %.60s... — output: severity=%s type=%s — latency: %dms",
            description,
            result.get("severity_score", "?"),
            result.get("emergency_type", "?"),
            latency_ms,
        )

        # Send the final parsed result as the closing SSE event
        final_payload = json.dumps({"result": result, "done": True})
        yield f"data: {final_payload}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering if behind proxy
        },
    )


@app.route("/embedding-test", methods=["POST"])
def embedding_test():
    """
    Confirms that amazon.nova-2-multimodal-embeddings-v1:0 is accessible.
    Body: { "text": "head trauma elderly patient" }
    Returns the embedding vector (1024 floats) + latency.
    Screenshot this to prove multimodal embedding access during the demo.
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "head trauma elderly patient").strip()

    log.info("[NOVAGUARD] Testing Nova Multimodal Embedding access...")
    t0 = time.time()

    try:
        response = bedrock.invoke_model(
            modelId=NOVA_EMBED_MODEL,
            body=json.dumps({"inputText": text}),
            contentType="application/json",
            accept="application/json",
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]  # type: ignore[index]
        return jsonify({
            "error": f"Bedrock error: {code}",
            "fix": (
                "Bedrock Model Access UI is retired — models are auto-enabled on first invoke. "
                "This is an IAM permissions issue. "
                f"Ensure your IAM role allows: bedrock:InvokeModel on "
                f"arn:aws:bedrock:{REGION}::foundation-model/{NOVA_EMBED_MODEL}. "
                "Check attached SCPs if using AWS Organizations."
            ),
        }), 403

    latency_ms = int((time.time() - t0) * 1000)
    result = json.loads(response["body"].read())
    embedding = result.get("embedding", [])

    return jsonify({
        "status": "ok",
        "model": NOVA_EMBED_MODEL,
        "input_text": text,
        "embedding_dimensions": len(embedding),
        "embedding_sample": embedding[:5],   # First 5 values — proof it ran
        "latency_ms": latency_ms,
    })


# ── Frontend ─────────────────────────────────────────────────────────────────
_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))

@app.route("/")
def serve_index():
    """Serve the demo frontend so it runs same-origin as the API (no CORS issues)."""
    return send_file(os.path.join(_DEMO_DIR, "index.html"))


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  NovaGuard Demo Backend")
    print(f"  Model  : {NOVA_LITE_MODEL}")
    print(f"  Region : {REGION}")
    print(f"  Port   : {PORT}")
    print("=" * 60)
    print("\n  Endpoints:")
    print(f"    GET  http://localhost:{PORT}/            ← Demo UI")
    print(f"    GET  http://localhost:{PORT}/health")
    print(f"    POST http://localhost:{PORT}/triage")
    print(f"    POST http://localhost:{PORT}/triage-stream  (SSE)")
    print(f"    POST http://localhost:{PORT}/embedding-test")
    print(f"\n  → Open in browser: http://localhost:{PORT}/\n")

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
