#!/usr/bin/env python3
"""
NovaGuard — Nova 2 Lite connectivity test.

Proves that your AWS credentials, region, and Bedrock Model Access are
all working before you touch the demo or Lambda stack.

Usage:
    pip install boto3
    python test_nova.py

Expected output:
    [NOVAGIARD] Calling Nova 2 Lite in us-east-1...
    [NOVAGIARD] ✅ Response in 1847ms
    {
      "severity_score": 91,
      "emergency_type": "MEDICAL",
      ...
    }
    [NOVAGIARD REAL CALL] Nova 2 Lite invoked ...
"""

import json, time, sys, boto3
from botocore.exceptions import ClientError

# ── Config ────────────────────────────────────────────────────────────────────
REGION   = "us-east-1"
MODEL_ID = "us.amazon.nova-lite-v1:0"

DEMO_MESSAGE = (
    "My father fell down the stairs. He is 72 years old and is bleeding "
    "from his head. He is not responding to me. I am deaf and cannot call "
    "on the phone. Please help immediately."
)

SYSTEM_PROMPT = (
    "You are NovaGuard's Emergency Triage AI. "
    "Analyze the emergency report and return ONLY a valid JSON object "
    "with these exact fields: severity_score (integer 0-100), "
    "emergency_type (MEDICAL/FIRE/POLICE/UNKNOWN), "
    "confidence (float 0.0-1.0), "
    "triage_narrative (string, 1 sentence for dispatcher), "
    "caller_instructions (string, immediate first-aid to relay to caller), "
    "recommended_response_level (integer 1-5). "
    "No markdown, no explanation. Only the JSON object."
)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n[NOVAGIARD] Calling Nova 2 Lite ({MODEL_ID}) in {REGION}...")
    print(f"[NOVAGIARD] Input message ({len(DEMO_MESSAGE)} chars):")
    print(f"  \"{DEMO_MESSAGE}\"\n")

    client = boto3.client("bedrock-runtime", region_name=REGION)

    try:
        t0 = time.time()
        response = client.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": f"EMERGENCY REPORT:\n{DEMO_MESSAGE}"}],
                }
            ],
            inferenceConfig={
                "maxTokens": 512,
                "temperature": 0.0,   # deterministic — critical for emergency triage
                "topP": 1.0,
            },
        )
        latency_ms = int((time.time() - t0) * 1000)

    except ClientError as e:
        code = e.response["Error"]["Code"]  # type: ignore[index]
        msg  = e.response["Error"]["Message"]  # type: ignore[index]
        print(f"\n[NOVAGIARD] ❌ Bedrock API error: {code}")
        print(f"  {msg}")
        if code == "AccessDeniedException":
            print("\n  FIX: Bedrock Model Access UI has been retired.")
            print("  Models are auto-enabled on first invocation — this is an IAM issue.")
            print("  Check that your IAM user/role has this policy:")
            print("    bedrock:InvokeModel on arn:aws:bedrock:us-east-1::foundation-model/*")
            print("  If using an assumed role, also check any attached SCPs.")
            print(f"  Specific model ARN to allow: arn:aws:bedrock:{REGION}::foundation-model/{MODEL_ID}")
        elif code == "UnrecognizedClientException":
            print("\n  FIX: Your AWS credentials are not configured.")
            print("  → Run: aws configure")
            print("  → Enter your Access Key ID, Secret Key, and region: us-east-1")
        sys.exit(1)

    # ── Parse response ──────────────────────────────────────────────────────
    output_text = response["output"]["message"]["content"][0]["text"]  # type: ignore[index]
    stop_reason = response["stopReason"]
    usage       = response["usage"]

    print(f"[NOVAGIARD] ✅ Response received in {latency_ms}ms")
    print(f"[NOVAGIARD] Stop reason : {stop_reason}")
    print(f"[NOVAGIARD] Token usage : {usage['inputTokens']} in / {usage['outputTokens']} out\n")

    # Try to parse as JSON — Nova 2 Lite should return clean JSON at temp=0
    try:
        result = json.loads(output_text)
        print("── Triage Result ─────────────────────────────────────────────────────")
        print(json.dumps(result, indent=2))
        severity = result.get("severity_score", "?")
        etype    = result.get("emergency_type", "?")
    except json.JSONDecodeError:
        print("── Raw output (not JSON — check your MODEL_ID / model access) ─────────")
        print(output_text)
        severity = "?"
        etype    = "?"

    # ── The exact CloudWatch-style log line judges will see during the demo ─
    print(
        f"\n[NOVAGIARD REAL CALL] Nova 2 Lite invoked — "
        f"input: \"{DEMO_MESSAGE[:60]}...\" — "
        f"output: severity={severity} type={etype} — "
        f"latency: {latency_ms}ms"
    )
    print("\n SUCCESS — Nova 2 Lite is working. Run demo/backend.py next.\n")


if __name__ == "__main__":
    main()
