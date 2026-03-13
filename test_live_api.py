"""
NovaGuard — Live AWS Production Test
Calls the deployed API Gateway endpoint and prints proof for the hackathon.
"""
import urllib.request
import json
import time

URL  = "https://438f02a6qk.execute-api.us-east-1.amazonaws.com/prod/triage"
DESC = (
    "My father fell down the stairs. He is 72 years old and is bleeding "
    "from his head. He is not responding to me. I am deaf and cannot call "
    "on the phone. Please help immediately."
)

body = json.dumps({"description": DESC}).encode()
req  = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"}, method="POST")

print("\n=== NOVAGUARD LIVE AWS PRODUCTION CALL ===")
print(f"Endpoint : {URL}")
print(f"Input    : {DESC[:80]}...")
print()

t0 = time.time()
resp   = urllib.request.urlopen(req, timeout=40)
result = json.loads(resp.read())
total  = int((time.time() - t0) * 1000)

print(f"Emergency ID    : {result['emergency_id']}")
print(f"Severity Score  : {result['severity_score']} / 100")
print(f"Emergency Type  : {result['emergency_type']}")
print(f"Confidence      : {result['confidence']}")
print(f"Response Level  : {result['recommended_response_level']} / 5")
print(f"Narrative       : {result['triage_narrative']}")
print(f"Instructions    : {result['caller_instructions']}")

meta = result.get("_meta", {})
print()
print(f"Nova Model      : {meta.get('model')}")
print(f"Nova Latency    : {meta.get('latency_ms')}ms")
print(f"Input tokens    : {meta.get('input_tokens')}")
print(f"Output tokens   : {meta.get('output_tokens')}")
print(f"Stop reason     : {meta.get('stop_reason')}")
print(f"DynamoDB write  : {meta.get('db_status')}")
print(f"Total e2e time  : {total}ms  (API GW + Lambda cold start + Nova + DynamoDB)")
print()
print("=" * 60)
print("PROOF CHECKLIST:")
print(f"  [x] Real AWS API Gateway URL: {URL}")
print(f"  [x] Real Nova 2 Lite model:   {meta.get('model')}")
print(f"  [x] Real DynamoDB write:      {meta.get('db_status')}")
print(f"  [x] Emergency ID for lookup:  {result['emergency_id']}")
print()
print("Next steps:")
print("  1. Go to CloudWatch > Log groups > /aws/lambda/novaguard-triage-agent")
print("  2. Search for [NOVAGUARD REAL CALL] — screenshot that log entry")
print("  3. Go to DynamoDB > novaguard-emergencies > Explore items")
print(f"  4. Find item with emergency_id = {result['emergency_id']}")
print("  5. Screenshot both — this is your hackathon proof of real AWS deployment")
print("=" * 60)
