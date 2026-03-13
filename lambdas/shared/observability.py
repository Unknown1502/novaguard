"""
NovaGuard Observability Utilities

Structured logging and X-Ray tracing helpers.
All lambda handlers import this module for consistent correlation ID
propagation, structured log emission, and trace annotation.

Design:
- Correlation ID = Step Functions execution ID (injected via Lambda context)
- All logs are JSON-structured for CloudWatch Logs Insights querying
- X-Ray segments are annotated with emergency_id, agent_name, severity
- Timing helpers enforce the SLO budget and emit duration metrics
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, Optional

import boto3

logger = logging.getLogger(__name__)

# Module-level context — set by handler on each invocation
_correlation_id: Optional[str] = None
_emergency_id: Optional[str] = None
_agent_name: str = os.environ.get("AGENT_NAME", "unknown")


def set_correlation_context(correlation_id: str, emergency_id: Optional[str] = None):
    global _correlation_id, _emergency_id
    _correlation_id = correlation_id
    _emergency_id = emergency_id


class StructuredLogger:
    """
    JSON-structured logger compatible with CloudWatch Logs Insights.

    All log entries include:
    - timestamp (ISO-8601)
    - level (INFO/WARN/ERROR)
    - agent_name
    - correlation_id (Step Functions execution ID)
    - emergency_id (when known)
    - event (structured event name)
    - message (human-readable)
    - Any additional keyword arguments
    """

    def __init__(self, name: str):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(os.environ.get("POWERTOOLS_LOG_LEVEL", "INFO"))

    def _log(self, level: str, event: str, message: str, **kwargs):
        record = {
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "level":          level,
            "agent":          _agent_name,
            "correlation_id": _correlation_id,
            "emergency_id":   _emergency_id,
            "event":          event,
            "message":        message,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        # Strip None values for cleaner logs
        record = {k: v for k, v in record.items() if v is not None}

        if level == "ERROR":
            self._logger.error(json.dumps(record))
        elif level == "WARN":
            self._logger.warning(json.dumps(record))
        else:
            self._logger.info(json.dumps(record))

    def info(self, event: str, message: str = "", **kwargs):
        self._log("INFO", event, message, **kwargs)

    def warn(self, event: str, message: str = "", **kwargs):
        self._log("WARN", event, message, **kwargs)

    def error(self, event: str, message: str = "", exc: Optional[Exception] = None, **kwargs):
        if exc:
            kwargs["exception_type"] = type(exc).__name__
            kwargs["exception_message"] = str(exc)
            kwargs["stack_trace"] = traceback.format_exc()
        self._log("ERROR", event, message, **kwargs)


# Module-level logger instance
log = StructuredLogger(__name__)


@contextmanager
def timed_operation(operation_name: str, slo_budget_ms: Optional[int] = None) -> Generator[Dict, None, None]:
    """
    Context manager that times a code block, emits a CloudWatch metric,
    and logs a warning if the operation exceeds its SLO budget.

    Usage:
        with timed_operation("triage_inference", slo_budget_ms=1500) as timer:
            result = invoke_nova_lite(...)
        # timer["duration_ms"] is set after the block exits
    """
    timer_info: Dict[str, Any] = {"operation": operation_name, "duration_ms": 0}
    start_ns = time.perf_counter_ns()

    try:
        yield timer_info
    finally:
        elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
        timer_info["duration_ms"] = elapsed_ms

        if slo_budget_ms and elapsed_ms > slo_budget_ms:
            log.warn(
                "SLO_BREACH",
                f"Operation '{operation_name}' exceeded SLO budget",
                operation=operation_name,
                duration_ms=elapsed_ms,
                slo_budget_ms=slo_budget_ms,
                overage_ms=elapsed_ms - slo_budget_ms,
            )
        else:
            log.info(
                "OPERATION_COMPLETE",
                f"Operation '{operation_name}' completed",
                operation=operation_name,
                duration_ms=elapsed_ms,
            )

        # Emit CloudWatch metric
        try:
            boto3.client("cloudwatch", region_name=os.environ.get("REGION", "us-east-1")).put_metric_data(
                Namespace="NovaGuard/AgentMetrics",
                MetricData=[{
                    "MetricName": "OperationDurationMs",
                    "Value":      elapsed_ms,
                    "Unit":       "Milliseconds",
                    "Dimensions": [
                        {"Name": "AgentName",     "Value": _agent_name},
                        {"Name": "OperationName", "Value": operation_name},
                    ],
                }],
            )
        except Exception:
            pass  # Metric emission failure must never impact agent logic


def annotate_xray_trace(emergency_id: Optional[str], severity: Optional[int] = None, **metadata):
    """
    Add X-Ray annotations and metadata to the current trace segment.
    Annotations are indexed (filterable); metadata is stored but not indexed.
    """
    try:
        from aws_xray_sdk.core import xray_recorder
        if emergency_id:
            xray_recorder.current_segment().put_annotation("emergency_id", emergency_id)
        if severity is not None:
            xray_recorder.current_segment().put_annotation("severity_score", severity)
        xray_recorder.current_segment().put_annotation("agent_name", _agent_name)
        if metadata:
            xray_recorder.current_segment().put_metadata("agent_context", metadata)
    except Exception:
        pass  # X-Ray is best-effort; never impact agent processing


def emit_business_metric(metric_name: str, value: float = 1.0, unit: str = "Count", **dimensions):
    """Emit a custom NovaGuard business metric to CloudWatch."""
    try:
        dims = [{"Name": k, "Value": str(v)} for k, v in dimensions.items()]
        dims.append({"Name": "AgentName", "Value": _agent_name})
        boto3.client("cloudwatch", region_name=os.environ.get("REGION", "us-east-1")).put_metric_data(
            Namespace="NovaGuard/BusinessMetrics",
            MetricData=[{
                "MetricName": metric_name,
                "Value":      value,
                "Unit":       unit,
                "Dimensions": dims,
            }],
        )
    except Exception:
        pass
