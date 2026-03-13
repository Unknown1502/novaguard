#!/usr/bin/env node
/**
 * NovaGuard MVP CDK Entry Point
 *
 * Deploys a single self-contained stack:
 *   API Gateway → Triage Lambda → Nova 2 Lite → DynamoDB
 *
 * Usage:
 *   cd novaguard/mvp
 *   npm install
 *   npx cdk bootstrap    # first time only
 *   npx cdk deploy
 *
 * No context flags required — uses CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION
 * which AWS CDK auto-resolves from your current AWS credentials.
 */

import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { NovaGuardMvpStack } from "./mvp-stack";

const app = new cdk.App();

new NovaGuardMvpStack(app, "NovaGuard-MVP", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region:  process.env.CDK_DEFAULT_REGION || "us-east-1",
  },
  description: "NovaGuard MVP — Emergency AI Triage with Amazon Nova 2 Lite (Hackathon Demo)",
  tags: {
    Project:     "NovaGuard",
    Environment: "mvp",
    Purpose:     "hackathon-demo",
  },
});

app.synth();
