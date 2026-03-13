#!/usr/bin/env python3
"""
NovaGuard Live API Tests
Tests all endpoints of the deployed 3-agent pipeline.

API: https://3nf5cv59xh.execute-api.us-east-1.amazonaws.com/prod
"""
import urllib.request
import json
import base64
import time

BASE_URL = "https://3nf5cv59xh.execute-api.us-east-1.amazonaws.com/prod"

PASS = "\033[92m✅ PASS\033[0m"
FAIL = "\033[91m❌ FAIL\033[0m"


def post(path, body, timeout=90):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        elapsed = time.time() - t0
        return json.loads(r.read()), elapsed


def get(path, timeout=10):
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        elapsed = time.time() - t0
        return json.loads(r.read()), elapsed


def check(name, result, elapsed, extra=""):
    ok = result is not None
    print(f"{PASS if ok else FAIL}  {name}  ({elapsed:.1f}s)  {extra}")
    return ok


# ── Test 1: Health check ──────────────────────────────────────────────────────
print("\n=== NovaGuard Live API Tests ===\n")
print("--- Health Check ---")
try:
    r, t = get("/health")
    check("GET /health", r, t, f"status={r.get('status')}")
except Exception as e:
    print(f"{FAIL}  GET /health  {e}")

# ── Test 2: Triage (single agent) ────────────────────────────────────────────
print("\n--- Triage Agent ---")
try:
    r, t = post("/triage", {"description": "Gas leak in apartment building, smell is very strong, 10 residents inside"})
    meta = r.get("triage", r)
    check("POST /triage", r, t,
          f"severity={meta.get('severity_score')} type={meta.get('emergency_type')} model={meta.get('model','?')}")
    print(f"  narrative: {meta.get('triage_narrative','')[:80]}")
except Exception as e:
    print(f"{FAIL}  POST /triage  {e}")

# ── Test 3: Dispatch (single agent) ──────────────────────────────────────────
print("\n--- Dispatch Agent ---")
try:
    r, t = post("/dispatch", {
        "emergency_id": "test-dispatch-001",
        "severity_score": 85,
        "emergency_type": "FIRE",
        "triage_narrative": "Gas leak in apartment, 10 residents",
        "caller_location": "123 Main St, apartment 4B",
    })
    result = r.get("result", r)
    units = result.get("units_dispatched", [])
    check("POST /dispatch", r, t,
          f"units={len(units)} priority={result.get('dispatch_priority','?')}")
    for u in units:
        print(f"  → {u.get('unit_type')} {u.get('unit_id')} ETA {u.get('eta_minutes')}min")
except Exception as e:
    print(f"{FAIL}  POST /dispatch  {e}")

# ── Test 4: Full pipeline (THE demo endpoint) ─────────────────────────────────
print("\n--- Full 3-Agent Pipeline (DEMO ENDPOINT) ---")
try:
    r, t = post("/pipeline", {
        "description": "My father fell down the stairs, he is 72 years old, bleeding from his head and unconscious"
    })
    meta = r.get("_meta", {})
    check("POST /pipeline", r, t,
          f"agents={meta.get('agents_ran')} nova_calls={meta.get('nova_calls')} total_ms={meta.get('total_pipeline_ms')}")

    triage = r.get("triage", {})
    dispatch = r.get("dispatch", {})
    comms = r.get("communications", {})

    print(f"\n  emergency_id: {r.get('emergency_id')}")
    print(f"  status: {r.get('status')}")
    print(f"\n  [TRIAGE]    severity={triage.get('severity_score')} type={triage.get('emergency_type')} ({triage.get('latency_ms')}ms)")
    print(f"              {triage.get('triage_narrative','')[:80]}")
    print(f"\n  [DISPATCH]  priority={dispatch.get('dispatch_priority')} units={len(dispatch.get('units_dispatched',[]))} ({dispatch.get('latency_ms')}ms)")
    for u in dispatch.get("units_dispatched", []):
        print(f"              → {u.get('unit_type')} ETA {u.get('eta_minutes')}min")
    print(f"\n  [COMMS]     ({comms.get('latency_ms')}ms)")
    print(f"              responder: {comms.get('responder_briefing','')[:80]}")
    for i, step in enumerate(comms.get("caller_instructions", []), 1):
        print(f"              caller[{i}]: {step[:70]}")

    print(f"\n  CloudWatch proof: {meta.get('cloudwatch_proof')}")
    print(f"  Pipeline total: {meta.get('total_pipeline_ms')}ms ({t:.1f}s wall clock)")

except Exception as e:
    print(f"{FAIL}  POST /pipeline  {e}")

# ── Test 5: Nova Sonic transcription (synthetic audio) ────────────────────────
print("\n--- Nova Sonic Transcription ---")
try:
    # 1 second of silence: 16kHz, mono, 16-bit PCM = 32000 bytes
    silence = b"\x00" * 32000
    audio_b64 = base64.b64encode(silence).decode()
    r, t = post("/transcribe", {"audio_b64": audio_b64}, timeout=60)
    check("POST /transcribe", r, t,
          f"channel={r.get('input_channel')} model={r.get('model_used','?')}")
    print(f"  transcript: '{r.get('transcript','')}'")
    print(f"  note: silence → empty transcript (expected)")
except Exception as e:
    print(f"{FAIL}  POST /transcribe  {e}")

print("\n=== Done ===\n")
