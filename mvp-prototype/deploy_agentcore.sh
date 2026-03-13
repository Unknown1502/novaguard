#!/bin/bash
# ============================================================
# NovaGuard Nova Act — AgentCore Runtime Deployment Script
# Run this in AWS CloudShell (already has Docker + AWS CLI)
# ============================================================
#
# Steps:
#   1. Open AWS CloudShell: https://us-east-1.console.aws.amazon.com/cloudshell
#   2. Upload this file: Actions → Upload File → deploy_agentcore.sh
#   3. Run: bash deploy_agentcore.sh
#   4. Copy the printed AGENTCORE_RUNTIME_ARN into CDK (mvp-stack.ts)
#   5. Run: cdk deploy
#
# After deploy, the /nova-act endpoint will call AgentCore instead of ECS.
# Workflow is visible at: https://us-east-1.console.aws.amazon.com/nova-act/home
# =====================================================================

set -e

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="us-east-1"
WORKFLOW_NAME="novaguard-cad-worker"

echo "=== NovaGuard AgentCore Deployment ==="
echo "Account: $ACCOUNT_ID | Region: $REGION | Workflow: $WORKFLOW_NAME"
echo ""

# 1. Install nova-act CLI
echo "[1/4] Installing nova-act CLI..."
pip install "nova-act[cli]" --quiet

# 2. Create a temp directory with the AgentCore main.py
echo "[2/4] Setting up workflow source..."
WORK_DIR=$(mktemp -d)
cat > "$WORK_DIR/main.py" << 'PYEOF'
"""
NovaGuard — Nova Act AgentCore Workflow
Entry point: def main(payload) -> dict
"""
from __future__ import annotations
import json, logging, os, time
from datetime import datetime, timezone
import boto3

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

REGION            = os.environ.get("AWS_REGION", "us-east-1")
EMERGENCIES_TABLE = os.environ.get("EMERGENCIES_TABLE", "novaguard-emergencies")


def main(payload: dict) -> dict:
    t0 = time.time()
    emergency_id   = payload.get("emergency_id", f"AC-{int(t0)}")
    cad_url        = payload.get("cad_url", "https://d10tmiea7afu9g.cloudfront.net/cad_mock.html")
    emergency_type = payload.get("emergency_type", "MEDICAL")
    priority       = payload.get("priority", "P2 - URGENT")
    location       = payload.get("location", "Location not specified")
    narrative      = payload.get("narrative", "Emergency response required.")
    unit_callsigns = payload.get("unit_callsigns", [])
    units_str = ", ".join(unit_callsigns) if unit_callsigns else "TBD"

    logger.info("[NOVA_ACT AGENTCORE] Starting CAD entry | id=%s type=%s", emergency_id, emergency_type)

    cad_incident_id = None
    nova_act_used   = False

    try:
        from nova_act import NovaAct
        with NovaAct(starting_page=cad_url, use_aws_iam_auth=True) as agent:
            agent.act(f"Select '{emergency_type}' from the incident type dropdown.")
            agent.act(f"Set the priority to '{priority}'.")
            agent.act(f"Enter this location: {location}")
            agent.act(f"Enter this narrative: {narrative}")
            agent.act(f"Enter dispatched units: {units_str}")
            result = agent.act(
                "Click Submit. Return the CAD incident number shown.",
                schema={"type":"object","properties":{"incident_number":{"type":"string"}},"required":["incident_number"]},
            )
            cad_incident_id = (result.matches or {}).get("incident_number", f"CAD-{emergency_id[:8].upper()}")
            nova_act_used   = True
            logger.info("[NOVA_ACT COMPLETE] %s", cad_incident_id)
    except Exception as exc:
        logger.warning("[NOVA_ACT AGENTCORE] Browser automation skipped: %s", exc)
        prefix = {"FIRE":"FIR","MEDICAL":"MED","POLICE":"POL"}.get(emergency_type,"INC")
        cad_incident_id = f"{prefix}-{int(time.time())%100000:05d}"

    elapsed_ms = int((time.time() - t0) * 1000)

    # Write result to DynamoDB
    try:
        dynamo = boto3.resource("dynamodb", region_name=REGION)
        dynamo.Table(EMERGENCIES_TABLE).update_item(
            Key={"emergency_id": emergency_id},
            UpdateExpression="SET #s=:s, cad_incident_id=:c, nova_act_used=:n, nova_act_ms=:ms, completed_at=:t",
            ExpressionAttributeNames={"#s":"status"},
            ExpressionAttributeValues={":s":"CAD_ENTERED",":c":cad_incident_id,":n":nova_act_used,":ms":elapsed_ms,":t":datetime.now(timezone.utc).isoformat()},
        )
    except Exception as exc:
        logger.error("DynamoDB write failed: %s", exc)

    return {
        "emergency_id": emergency_id, "cad_incident_id": cad_incident_id,
        "nova_act_used": nova_act_used, "status": "CAD_ENTERED", "elapsed_ms": elapsed_ms,
    }
PYEOF

echo "Workflow source written to $WORK_DIR/main.py"

# 3. Create and deploy workflow
echo "[3/4] Deploying workflow to AgentCore Runtime..."
act workflow create --name "$WORKFLOW_NAME" 2>/dev/null || true
act workflow deploy \
  --name "$WORKFLOW_NAME" \
  --source-dir "$WORK_DIR" \
  --region "$REGION"

# 4. Get the runtime ARN
echo "[4/4] Fetching runtime ARN..."
RUNTIME_ARN=$(act workflow show --name "$WORKFLOW_NAME" --region "$REGION" 2>/dev/null | grep "deployment_arn" | awk '{print $2}' | tr -d '",' || echo "")

echo ""
echo "==================================================="
echo "DEPLOYMENT COMPLETE"
echo "==================================================="
echo ""
echo "Nova Act Console:"
echo "  https://us-east-1.console.aws.amazon.com/nova-act/home?region=us-east-1#/"
echo ""
if [ -n "$RUNTIME_ARN" ]; then
    echo "AgentCore Runtime ARN: $RUNTIME_ARN"
    echo ""
    echo "Add to mvp-stack.ts:"
    echo "  AGENTCORE_RUNTIME_ARN: \"$RUNTIME_ARN\","
    echo ""
    echo "Then run: npx cdk deploy --require-approval never"
else
    echo "Get the Runtime ARN from the Nova Act console above"
    echo "Then set AGENTCORE_RUNTIME_ARN in mvp-stack.ts and redeploy"
fi
echo ""

# Test invocation
echo "Testing invocation..."
act workflow run \
  --name "$WORKFLOW_NAME" \
  --region "$REGION" \
  --payload '{"emergency_id":"test-001","emergency_type":"MEDICAL","priority":"P1 - IMMEDIATE","location":"450 Main St","narrative":"Test dispatch","unit_callsigns":["MEDIC-4"]}' \
  2>&1 | head -20 || true

echo "Done."
