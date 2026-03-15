# NovaGuard Future Roadmap

This document tracks post-hackathon execution priorities for turning NovaGuard from a prototype into a pilot-ready emergency accessibility platform.

## Guiding Principles

1. Reliability before feature expansion.
2. Human-in-the-loop safety for every high-risk decision.
3. Interoperability with existing PSAP/CAD systems.
4. Measurable accessibility and response-time outcomes.

## Phased Plan

| Phase | Timeline | Primary Goal | Key Deliverables | Success Metrics |
|---|---|---|---|---|
| Phase 1: Platform Hardening | 0-30 days | Stabilize production behavior | Retry/backoff standardization, endpoint error handling, canary release workflow, infra validation checks | API uptime >= 99.9%, pipeline failure rate < 1% |
| Phase 2: Safety and Governance | 30-60 days | Increase operational trust | Escalation policy matrix, confidence thresholds by incident type, review queue for low-confidence triage, immutable decision audit views | 100% traceable decisions, human escalation turnaround < 5 min |
| Phase 3: Accessibility Expansion | 60-90 days | Improve multimodal access | Better TTY/ASL routing, multilingual templates, caller guidance prompts by channel, fallback comms workflows | 10+ language paths validated, reduced caller abandonment |
| Phase 4: Field Pilot Enablement | 90-180 days | Pilot with real agencies | CAD adapter interface, role-based dashboard permissions, onboarding runbooks, incident replay and QA tooling | 2-3 pilot agencies onboarded |
| Phase 5: Predictive Operations | 6-12 months | Move from reactive to proactive support | Prediction retraining pipeline, shift-level resource recommendation scoring, closed-loop post-incident learning | Improved ETA accuracy and dispatch efficiency |

## Workstreams

### 1) Reliability and SRE
- Introduce synthetic checks for `/triage`, `/dispatch`, `/proof`, `/nova-act/latest`.
- Add SLA monitors per workflow stage (intake, triage, dispatch, comms).
- Implement controlled degradation mode when downstream services fail.

### 2) Safety and Responsible AI
- Expand triage rubric validation tests across edge-case incident prompts.
- Add confidence calibration tracking per model and per scenario class.
- Enforce mandatory human escalation for policy-defined critical conditions.

### 3) Accessibility and UX
- Add channel-specific communication scripts (voice, SMS, TTY, ASL relay).
- Improve narrator and guided-flow reliability for demo and training use.
- Support clearer fallback guidance when live integrations are temporarily unavailable.

### 4) Integrations
- Define CAD integration contract (event schema, status callbacks, ack semantics).
- Harden Medicare.gov retrieval path with retries and stale-cache policy.
- Extend location/ETA data quality checks for high-latency scenarios.

### 5) Compliance and Security
- Maintain strict least-privilege IAM boundaries for each agent.
- Expand audit exports for incident review and post-mortem workflows.
- Document HIPAA/CJIS control mapping for pilot readiness.

## Milestone Exit Criteria

### M1 Exit (30 days)
- Stable end-to-end guided flow with no uncaught client-side errors.
- Endpoint-level dashboards and alarms live.

### M2 Exit (60 days)
- Safety policy matrix implemented and tested.
- Human escalation path verified in integration tests.

### M3 Exit (90 days)
- Accessibility channels validated with test scripts and acceptance criteria.
- Pilot onboarding package finalized.

### M4 Exit (180 days)
- At least one live pilot running with weekly review cadence.
- Measured improvements reported for response support quality.

## Open Risks

- External website dependencies (for Nova Act browser automation).
- Variability in local PSAP/CAD integration constraints.
- Different network/security policies across pilot agencies.

## Ownership Suggestion

- Platform/Infra: CDK + reliability tracks
- AI/Safety: triage policy + evaluation loops
- Accessibility: caller channel design and validation
- Integrations: CAD/PSAP interfaces and pilot onboarding
