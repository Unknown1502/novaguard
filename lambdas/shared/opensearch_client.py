"""
NovaGuard OpenSearch Client

Thin wrapper for Amazon OpenSearch Serverless (AOSS) using IAM/SigV4 auth.
Provides kNN similarity search and document upsert utilities for:
  - Emergency protocol retrieval (used by triage agent)
  - Duplicate incident detection (used by intake agent)
  - Historical incident indexing (used by post-incident summary agent)

Authentication: AWS SigV4 via requests_aws4auth (bundled in Lambda layer).
All writes use the OpenSearch _bulk API for throughput.
All kNN reads use the HNSW approximate search (ef_search=100).

Environment variables required:
  OPENSEARCH_ENDPOINT   — AOSS collection endpoint (https://...)
  REGION                — AWS region (for SigV4)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import boto3
import requests

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("POWERTOOLS_LOG_LEVEL", "INFO"))

REGION              = os.environ.get("REGION", "us-east-1")
OPENSEARCH_ENDPOINT = os.environ.get("OPENSEARCH_ENDPOINT", "")
# SSM fallback if env var not set at container init time
_OPENSEARCH_SSM_PARAM = os.environ.get("OPENSEARCH_ENDPOINT_PARAM", "/novaguard/opensearch-endpoint")

_endpoint_cache: Optional[str] = None
_ssm_client     = None


def _get_endpoint() -> str:
    """Return the OpenSearch endpoint, lazy-loading from SSM if needed."""
    global _endpoint_cache
    if _endpoint_cache:
        return _endpoint_cache
    ep = OPENSEARCH_ENDPOINT
    if not ep:
        global _ssm_client
        if _ssm_client is None:
            _ssm_client = boto3.client("ssm", region_name=REGION)
        try:
            ep = _ssm_client.get_parameter(Name=_OPENSEARCH_SSM_PARAM)["Parameter"]["Value"]
        except Exception as e:
            logger.warning("Could not load OpenSearch endpoint from SSM: %s", e)
            ep = ""
    _endpoint_cache = ep.rstrip("/")
    return _endpoint_cache


def _get_auth_headers(method: str, url: str, body: str) -> Dict[str, str]:
    """
    Generate AWS SigV4 Authorization headers for an OpenSearch Serverless request.

    Uses boto3 Credentials to sign the request inline — avoids importing
    requests_aws4auth (which may not always be available) by using botocore
    directly.
    """
    import botocore.auth
    import botocore.awsrequest
    import botocore.credentials

    session    = boto3.Session()
    creds      = session.get_credentials().get_frozen_credentials()
    signer     = botocore.auth.SigV4Auth(creds, "aoss", REGION)
    aws_request = botocore.awsrequest.AWSRequest(
        method=method,
        url=url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    signer.add_auth(aws_request)
    return dict(aws_request.headers)


def _os_request(
    method: str,
    path: str,
    body: Optional[Dict] = None,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """
    Fire a signed HTTP request to OpenSearch Serverless.

    Raises on HTTP errors. Returns parsed JSON response body.
    """
    endpoint = _get_endpoint()
    if not endpoint:
        raise RuntimeError("OpenSearch endpoint not configured — set OPENSEARCH_ENDPOINT env var")

    url        = f"{endpoint}/{path.lstrip('/')}"
    body_str   = json.dumps(body) if body else ""
    headers    = _get_auth_headers(method, url, body_str)

    response = requests.request(
        method,
        url,
        headers=headers,
        data=body_str.encode("utf-8") if body_str else None,
        timeout=timeout,
    )

    if response.status_code >= 400:
        logger.error(
            "OpenSearch error: %s %s → %d: %s",
            method, path, response.status_code, response.text[:500],
        )
        response.raise_for_status()

    return response.json()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_knn(
    index: str,
    embedding: List[float],
    field: str = "embedding",
    k: int = 5,
    min_score: float = 0.0,
    extra_filter: Optional[Dict] = None,
) -> List[Dict[str, Any]]:
    """
    Run a kNN (approximate nearest-neighbour) search against an HNSW index.

    Args:
        index:        OpenSearch index name (e.g. "historical-incidents")
        embedding:    Query vector (must match index dimensionality: 1024)
        field:        Index field that stores the vector
        k:            Number of nearest neighbours to retrieve
        min_score:    Minimum cosine similarity score (0.0–1.0) to include in results
        extra_filter: Optional OpenSearch bool filter appended to the query

    Returns:
        List of hit dicts, each containing _id, _score, and _source fields.
    """
    knn_query: Dict[str, Any] = {
        "size": k,
        "query": {
            "knn": {
                field: {
                    "vector": embedding,
                    "k":      k,
                },
            },
        },
        "min_score": min_score,
    }

    if extra_filter:
        knn_query["query"] = {
            "bool": {
                "must":   [{"knn": {field: {"vector": embedding, "k": k}}}],
                "filter": [extra_filter],
            }
        }

    result = _os_request("GET", f"{index}/_search", knn_query)
    hits   = result.get("hits", {}).get("hits", [])
    logger.info("kNN search on %s returned %d hits (k=%d, min_score=%.3f)",
                index, len(hits), k, min_score)
    return hits


def upsert_embedding_document(
    index: str,
    doc_id: str,
    embedding: List[float],
    metadata: Dict[str, Any],
    image_embedding: Optional[List[float]] = None,
) -> str:
    """
    Upsert a document with its embedding vector into an OpenSearch index.

    Uses the _doc endpoint with upsert semantics (PUT with ?refresh=false).

    Args:
        index:           Target index name
        doc_id:          Unique document ID (e.g. "{emergency_id}-embed")
        embedding:       Primary text/multimodal embedding vector
        metadata:        Arbitrary key→value metadata to store alongside embedding
        image_embedding: Optional separate 1024-dim image embedding

    Returns:
        doc_id on success.
    """
    doc: Dict[str, Any] = {
        "embedding":    embedding,
        "indexed_at":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **metadata,
    }
    if image_embedding:
        doc["image_embedding"] = image_embedding

    _os_request("PUT", f"{index}/_doc/{doc_id}?refresh=false", doc)
    logger.info("Upserted document %s into index %s", doc_id, index)
    return doc_id


def search_recent_open_incidents(
    index: str,
    embedding: List[float],
    k: int = 3,
    min_score: float = 0.88,
    open_statuses: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Convenience wrapper: search for open incidents similar to the given embedding.

    Filters by status != RESOLVED and != CANCELLED so we only merge into
    active emergencies (deduplication guard in intake_agent).

    Returns list of hits with _score >= min_score, sorted descending by score.
    """
    statuses = open_statuses or [
        "PENDING_INTAKE", "TRIAGING", "DISPATCHING", "DISPATCHED",
        "CALLER_NOTIFIED", "ESCALATED",
    ]
    # Build a terms filter so we only match open emergencies
    status_filter = {"terms": {"status.keyword": statuses}}

    hits = search_knn(
        index      = index,
        embedding  = embedding,
        field      = "embedding",
        k          = k,
        min_score  = min_score,
        extra_filter = status_filter,
    )
    return hits
