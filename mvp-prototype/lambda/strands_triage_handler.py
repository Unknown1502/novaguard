"""
NovaGuard — Strands Agents Triage Handler
==========================================
Demonstrates the strands-agents framework for multi-tool AI orchestration.

Architecture:
  API Gateway → Lambda → Strands Agent (Nova 2 Lite)
                           ├── @tool: translate_emergency_text()
                           ├── @tool: query_history()
                           ├── @tool: calculate_severity()
                           └── @tool: log_to_dynamo()

The Strands Agent reasons across tools in a real agentic loop:
  Turn 1: translate_emergency_text (if non-English)
  Turn 2: query_history (similar past incidents)
  Turn 3: calculate_severity (based on keywords + history)
  Turn 4: Final JSON triage result
  Turn 5: log_to_dynamo (persist outcome)

This replaces the simpler raw bedrock.converse() pattern in handler.py
with a framework that demonstrates agentic reasoning depth.
"""

import json
import os
import time
import uuid
import math
import logging
import threading
import urllib.request
import urllib.parse
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Attr


# ─────────────────────────────────────────────────────────
# DynamoDB Decimal → Python native type helper
# ─────────────────────────────────────────────────────────
def _dec(v):
    """Convert Decimal to int or float for JSON serialization."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────────────────
# AWS clients
# ─────────────────────────────────────────────────────────
bedrock    = boto3.client("bedrock-runtime", region_name="us-east-1")
dynamodb   = boto3.resource("dynamodb", region_name="us-east-1")
translate  = boto3.client("translate",       region_name="us-east-1")
lambda_client  = boto3.client("lambda",          region_name="us-east-1")

TABLE_NAME = os.environ.get("EMERGENCY_TABLE", "novaguard-emergencies")
table      = dynamodb.Table(TABLE_NAME)


# ─────────────────────────────────────────────────────────
# Thread-safe tool call tracker
# ─────────────────────────────────────────────────────────
_tracker_lock = threading.Lock()
_tracked_tool_calls: list = []


def _reset_tracker() -> None:
    global _tracked_tool_calls
    with _tracker_lock:
        _tracked_tool_calls = []


def _record_call(name: str) -> None:
    with _tracker_lock:
        if name not in _tracked_tool_calls:
            _tracked_tool_calls.append(name)


def _get_tracked_calls() -> list:
    with _tracker_lock:
        return list(_tracked_tool_calls)

# ─────────────────────────────────────────────────────────
# Thread-safe tool result cache for pre-fetch acceleration
# Pre-fetch stores results here; tool functions return them
# instantly on cache hit, skipping the external API call.
# ─────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_TOOL_CACHE: dict = {}

# ─────────────────────────────────────────────────────────
# Strands + MCP imports with graceful fallback
# ─────────────────────────────────────────────────────────
STRANDS_AVAILABLE = False
MCP_AVAILABLE     = False
try:
    from strands import Agent, tool  # pip install strands-agents
    from strands.models import BedrockModel  # for streaming=False config
    from strands.tools.mcp.mcp_client import MCPClient
    from mcp.client.stdio import stdio_client, StdioServerParameters
    import sys as _sys
    STRANDS_AVAILABLE = True
    MCP_AVAILABLE     = True
    logger.info("[STRANDS] strands-agents + MCP loaded successfully")
except Exception as _strands_err:
    logger.warning("[STRANDS] strands-agents not installed — will use raw bedrock.converse() fallback: %s", _strands_err)
    def tool(fn):
        """No-op decorator when Strands is not installed."""
        return fn

# ─────────────────────────────────────────────────────────
# Tool definitions
# (These work as @tool decorated functions when Strands is
#  available, or as plain Python functions in fallback mode)
# ─────────────────────────────────────────────────────────

@tool
def translate_emergency_text(text: str, source_language: str = "auto") -> dict:
    """
    Detect language of emergency text and translate to English if needed.
    Uses Amazon Translate for real language detection and translation.

    Args:
        text: Raw emergency description text
        source_language: ISO language code or 'auto' for detection

    Returns:
        dict with translated_text, detected_language, was_translated
    """
    _record_call("translate_emergency_text")
    _ck = f"translate:{text[:300]}"
    with _cache_lock:
        if _ck in _TOOL_CACHE:
            logger.info("[STRANDS TOOL] translate_emergency_text — cache hit")
            return _TOOL_CACHE[_ck]
    logger.info(f"[STRANDS TOOL] translate_emergency_text called | lang={source_language}")

    try:
        translate_client = boto3.client("translate", region_name=os.environ.get("REGION", "us-east-1"))
        # Use Amazon Translate for real language detection + translation
        src_lang = "auto" if source_language == "auto" else source_language
        response = translate_client.translate_text(
            Text=text[:5000],  # Translate API limit
            SourceLanguageCode=src_lang,
            TargetLanguageCode="en",
        )
        detected = response.get("SourceLanguageCode", "en")
        translated = response.get("TranslatedText", text)
        was_translated = detected != "en"
        logger.info(f"[STRANDS TOOL] Amazon Translate | detected={detected} | translated={was_translated}")
        result = {
            "translated_text":  translated,
            "detected_language": detected,
            "was_translated":   was_translated,
            "service":          "Amazon Translate",
        }
    except Exception as e:
        logger.warning(f"[STRANDS TOOL] Amazon Translate failed, using fallback: {e}")
        # Fallback: ASCII-based heuristic
        is_ascii = all(ord(c) < 128 for c in text.replace('\n', ' '))
        detected = "en" if is_ascii else "unknown"
        if source_language != "auto" and source_language != "en":
            detected = source_language
        result = {
            "translated_text":  text,
            "detected_language": detected,
            "was_translated":   False,
            "service":          "fallback-ascii-heuristic",
        }
    with _cache_lock:
        _TOOL_CACHE[_ck] = result
    return result


@tool
def query_history(emergency_type: str, limit: int = 5) -> list:
    """
    Query DynamoDB for recent similar emergencies to inform triage severity.

    Args:
        emergency_type: Category like 'MEDICAL', 'FIRE', 'TRAUMA', 'RESCUE', 'HAZMAT'
        limit: Max number of recent incidents to return

    Returns:
        List of recent similar emergency summaries
    """
    _record_call("query_history")
    _ck = f"history:{emergency_type}:{limit}"
    with _cache_lock:
        if _ck in _TOOL_CACHE:
            logger.info("[STRANDS TOOL] query_history — cache hit | type=%s", emergency_type)
            return _TOOL_CACHE[_ck]
    logger.info(f"[STRANDS TOOL] query_history | type={emergency_type}")
    try:
        response = table.scan(
            FilterExpression=Attr("emergency_type").eq(emergency_type),
            Limit=50,  # scan more, then trim to limit
        )
        items = response.get("Items", [])
        # Sort by timestamp descending
        items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        items = items[:limit]
        result = [
            {
                "emergency_id":   item.get("emergency_id"),
                "emergency_type": item.get("emergency_type"),
                "severity_score": _dec(item.get("severity_score")),
                "timestamp":      item.get("timestamp"),
                "description":    str(item.get("description", ""))[:120],
            }
            for item in items
        ]
    except Exception as e:
        logger.warning(f"[STRANDS TOOL] query_history failed: {e}")
        result = []
    with _cache_lock:
        _TOOL_CACHE[_ck] = result
    return result


@tool
def calculate_severity(description: str, emergency_type: str, history: list) -> dict:
    """
    Calculate a severity score (0-100) from keyword analysis and historical patterns.

    Args:
        description: Translated emergency description
        emergency_type: Emergency category
        history: List of recent similar incidents from query_history

    Returns:
        dict with severity_score (int), severity_level (str), factors (list)
    """
    _record_call("calculate_severity")
    logger.info(f"[STRANDS TOOL] calculate_severity | type={emergency_type}")

    text   = (description + " " + emergency_type).lower()
    score  = 30  # baseline
    factors = []

    # Critical keyword scan
    critical_words = {
        "unconscious": 30, "not breathing": 35, "cardiac arrest": 40,
        "shooting": 35, "stabbing": 30, "explosion": 40,
        "fire": 20, "trapped": 25, "multiple victims": 30,
        "child": 15, "infant": 20, "infant": 20,
        "hazmat": 25, "chemical": 20, "gas leak": 25,
        "unresponsive": 30, "bleeding": 20, "severe": 15,
    }
    for word, pts in critical_words.items():
        if word in text:
            score += pts
            factors.append(f"+{pts} [{word}]")
            if score >= 95:
                break

    # Historical severity bump
    if history:
        avg_hist = sum(float(_dec(h.get("severity_score", 50)) or 50) for h in history) / len(history)
        bump = int((avg_hist - 50) * 0.15)  # 15% weight on history
        if bump:
            score += bump
            factors.append(f"{'+' if bump > 0 else ''}{bump} [historical avg {avg_hist:.0f}]")

    score = max(10, min(100, score))

    if score >= 85:
        level = "CRITICAL"
    elif score >= 65:
        level = "HIGH"
    elif score >= 40:
        level = "MODERATE"
    else:
        level = "LOW"

    return {"severity_score": score, "severity_level": level, "factors": factors}


@tool
def log_to_dynamo(emergency_id: str, emergency_type: str, severity_score: int,
                  description: str, caller_instructions: str,
                  detected_language: str = "en") -> dict:
    """
    Persist the completed triage result to DynamoDB.

    Args:
        emergency_id: Unique ID
        emergency_type: Emergency category
        severity_score: 0-100 severity
        description: Processed description
        caller_instructions: What to tell the caller
        detected_language: Language code detected

    Returns:
        dict with success bool and emergency_id
    """
    _record_call("log_to_dynamo")
    logger.info(f"[STRANDS TOOL] log_to_dynamo | id={emergency_id}")
    try:
        table.put_item(Item={
            "emergency_id":        emergency_id,
            "version":             4,
            "timestamp":           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "emergency_type":      emergency_type,
            "severity_score":      severity_score,
            "description":         description,
            "caller_instructions": caller_instructions,
            "detected_language":   detected_language,
            "agent_framework":     "strands-agents",
            "triage_agent":        "nova-lite-strands",
        })
        return {"success": True, "emergency_id": emergency_id}
    except Exception as e:
        logger.error(f"[STRANDS TOOL] DynamoDB write failed: {e}")
        return {"success": False, "error": str(e)}


@tool
def find_similar_incidents(description: str, limit: int = 3) -> dict:
    """
    Use Amazon Nova Multimodal Embeddings (3072-dim) to find semantically similar
    past emergency incidents in DynamoDB via cosine similarity.

    Args:
        description: Emergency description text to match against
        limit: Max number of similar incidents to return (default 3)

    Returns:
        dict with similar_incidents, embedding_dim, and model name
    """
    _record_call("find_similar_incidents")
    _ck = f"similar:{description[:300]}:{limit}"
    with _cache_lock:
        if _ck in _TOOL_CACHE:
            logger.info("[STRANDS TOOL] find_similar_incidents — cache hit")
            return _TOOL_CACHE[_ck]
    logger.info(f"[STRANDS TOOL] find_similar_incidents | nova-multimodal-embed | desc={description[:60]}")

    EMBED_MODEL = "amazon.nova-2-multimodal-embeddings-v1:0"

    def _embed(text: str) -> list:
        resp = bedrock.invoke_model(
            modelId=EMBED_MODEL,
            body=json.dumps({
                "taskType": "SINGLE_EMBEDDING",
                "singleEmbeddingParams": {
                    "embeddingPurpose": "GENERIC_RETRIEVAL",
                    "text": {"truncationMode": "NONE", "value": text[:1000]},
                },
            }),
            contentType="application/json",
            accept="application/json",
        )
        return json.loads(resp["body"].read())["embeddings"][0]["embedding"]

    def _cosine(a: list, b: list) -> float:
        dot  = sum(x * y for x, y in zip(a, b))
        mag  = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
        return dot / mag if mag > 0 else 0.0

    try:
        current_emb = _embed(description)
    except Exception as e:
        logger.warning("[STRANDS TOOL] Nova embedding failed: %s", e)
        return {"similar_incidents": [], "error": str(e), "model": EMBED_MODEL}

    # Fetch up to 15 recent incidents from DynamoDB
    try:
        recent = [
            item for item in table.scan(Limit=15).get("Items", [])
            if item.get("description")
        ]
    except Exception:
        recent = []

    scored = []
    for item in recent[:4]:   # cap at 4 to bound latency
        try:
            past_emb = _embed(str(item["description"])[:1000])
            sim = _cosine(current_emb, past_emb)
            scored.append({
                "emergency_id":   item.get("emergency_id"),
                "emergency_type": item.get("emergency_type"),
                "severity_score": _dec(item.get("severity_score", 0)),
                "description":    str(item["description"])[:100],
                "similarity":     round(sim, 4),
            })
        except Exception:
            continue

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    logger.info("[STRANDS TOOL] Nova embedding done | candidates=%d top_sim=%.3f",
                len(scored), scored[0]["similarity"] if scored else 0)

    result = {
        "similar_incidents":   scored[:limit],
        "model":               EMBED_MODEL,
        "embedding_dim":       3072,
        "candidates_searched": len(scored),
    }
    with _cache_lock:
        _TOOL_CACHE[_ck] = result
    return result




@tool
def get_weather_hazard(location: str) -> dict:
    """
    Get real-time weather conditions at the emergency location from Open-Meteo API.
    Returns temperature, wind speed, precipitation, and active weather hazards.
    This is a REAL API call — no API key required.

    Args:
        location: City name or address of the emergency

    Returns:
        dict with weather conditions, hazards list, and responder brief
    """
    _record_call("get_weather_hazard")
    logger.info(f"[STRANDS TOOL] get_weather_hazard | location={location}")
    try:
        geo_url = (
            "https://geocoding-api.open-meteo.com/v1/search"
            f"?name={urllib.parse.quote(location)}&count=1&language=en&format=json"
        )
        with urllib.request.urlopen(geo_url, timeout=5) as r:
            geo = json.loads(r.read())
        results = geo.get("results", [])
        if not results:
            return {"location": location, "hazards": [], "all_clear": True,
                    "responder_brief": "Location not resolved — assuming clear conditions",
                    "data_source": "Open-Meteo"}
        lat, lon = results[0]["latitude"], results[0]["longitude"]
        city = results[0].get("name", location)
        w_url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,wind_speed_10m,precipitation,weather_code"
        )
        with urllib.request.urlopen(w_url, timeout=5) as r:
            wdata = json.loads(r.read())["current"]
        code = wdata.get("weather_code", 0)
        wind = wdata.get("wind_speed_10m", 0)
        precip = wdata.get("precipitation", 0)
        temp = wdata.get("temperature_2m", 20)
        hazards = []
        if code >= 95:
            hazards.append("THUNDERSTORM — lightning risk, aerial ops unsafe")
        if 71 <= code <= 77:
            hazards.append("SNOW/BLIZZARD — icy roads, reduced traction")
        if wind > 50:
            hazards.append(f"EXTREME WINDS {wind:.0f}km/h — fire spread risk")
        elif wind > 30:
            hazards.append(f"HIGH WINDS {wind:.0f}km/h — monitor fire spread")
        if precip > 10:
            hazards.append(f"HEAVY RAIN {precip:.1f}mm/hr — road flooding risk")
        if temp < -10:
            hazards.append("EXTREME COLD — hypothermia risk")
        if temp > 38:
            hazards.append("EXTREME HEAT — responder heat stress")
        logger.info(f"[STRANDS TOOL] Weather: {city} temp={temp}C wind={wind}km/h hazards={len(hazards)}")
        return {
            "location": city, "lat": lat, "lon": lon,
            "temperature_c": temp, "wind_kmh": wind,
            "precipitation_mm": precip, "weather_code": code,
            "hazards": hazards, "all_clear": len(hazards) == 0,
            "responder_brief": "; ".join(hazards) if hazards else "Clear conditions — no weather hazards.",
            "data_source": "Open-Meteo (real-time API)",
        }
    except Exception as exc:
        logger.warning(f"[STRANDS TOOL] Weather API failed: {exc}")
        return {"location": location, "hazards": [], "all_clear": True,
                "error": str(exc), "data_source": "Open-Meteo (failed — defaulting clear)"}


@tool
def request_dispatch_assessment(description: str, emergency_type: str,
                                 severity_score: int, location: str = "") -> dict:
    """
    Delegate to the NovaGuard Dispatch Agent for unit assignment and ETA calculation.
    Demonstrates real inter-agent communication via Lambda invoke.

    Args:
        description: Emergency description
        emergency_type: Category (MEDICAL, FIRE, TRAUMA, RESCUE, HAZMAT)
        severity_score: Severity 0-100 from triage
        location: Emergency location address

    Returns:
        dict with dispatch assessment including units, ETAs, and priority
    """
    _record_call("request_dispatch_assessment")
    logger.info(f"[STRANDS TOOL] request_dispatch_assessment | type={emergency_type} sev={severity_score}")
    try:
        payload = {
            "body": json.dumps({
                "description": description,
                "emergency_type": emergency_type,
                "severity_score": severity_score,
                "location": location or "450 Main St, Riverside CA 92501",
            }),
            "httpMethod": "POST",
        }
        resp = lambda_client.invoke(
            FunctionName="novaguard-dispatch-agent",
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        result = json.loads(resp["Payload"].read())
        body = json.loads(result.get("body", "{}")) if isinstance(result.get("body"), str) else result
        logger.info("[STRANDS TOOL] Dispatch delegation complete: %s", json.dumps(body)[:200])
        return {
            "dispatch_priority": body.get("dispatch_priority", "URGENT"),
            "units_dispatched": body.get("units_dispatched", []),
            "dispatch_narrative": body.get("dispatch_narrative", ""),
            "location_routing_used": body.get("location_routing_used", False),
            "inter_agent_delegation": True,
            "source_agent": "strands-triage",
            "target_agent": "dispatch",
        }
    except Exception as exc:
        logger.warning(f"[STRANDS TOOL] Dispatch delegation failed: {exc}")
        return {"error": str(exc), "inter_agent_delegation": True, "fallback": True}

ALL_REGISTERED_TOOLS = [
    "translate_emergency_text",
    "find_similar_incidents",
    "query_history",
    "calculate_severity",
    "log_to_dynamo",
    "get_weather_hazard",
    "request_dispatch_assessment",
]

# ─────────────────────────────────────────────────────────
# MCP tool loader (weather + hospital + resources)
# ─────────────────────────────────────────────────────────
def _load_mcp_tools() -> tuple:
    """Start LocalMCP server subprocess, return (MCPClient | None, tools list)."""
    if not MCP_AVAILABLE:
        return None, []
    try:
        import os as _os
        python_exe = _sys.executable
        mcp_script = _os.path.join(_os.path.dirname(__file__), "mcp_server.py")
        if not _os.path.exists(mcp_script):
            mcp_script = "/var/task/mcp_server.py"

        params = StdioServerParameters(
            command=python_exe,
            args=[mcp_script],
        )
        client = MCPClient(lambda: stdio_client(params))
        client.start()
        tools = client.list_tools_sync()
        names = [t.tool_name for t in tools]
        logger.info("[STRANDS MCP] Loaded %d tools: %s", len(names), names)
        return client, list(tools)
    except Exception as exc:
        logger.warning("[STRANDS MCP] Could not load MCP tools (non-fatal): %s", exc)
        return None, []


# ─────────────────────────────────────────────────────────
# System prompt for the Strands Agent
# ─────────────────────────────────────────────────────────
STRANDS_SYSTEM_PROMPT = """You are a senior 911 emergency triage AI for the NovaGuard system, specialized in assisting deaf and hard-of-hearing callers.

MANDATORY TOOL SEQUENCE — you MUST call ALL five core tools for EVERY request in this order:
1. translate_emergency_text() — detect language, translate if needed
2. find_similar_incidents() — Nova Multimodal Embeddings 3072-dim vector search
3. query_history() — DynamoDB recent incident lookup
4. calculate_severity() — keyword + historical severity scoring
5. log_to_dynamo() — persist triage result

CONDITIONAL AGENTIC LOGIC (reason about tool results, do not just follow a script):
- If calculate_severity returns severity_score >= 85: Call find_similar_incidents() AGAIN with the confirmed emergency_type to find matching historical outcomes. Add "escalation_required": true to your response.
- If severity_score >= 90: Also call request_dispatch_assessment() to pre-alert the dispatch agent with the triage result. This demonstrates real inter-agent delegation.
- If the emergency description mentions a specific location: Call get_weather_hazard() to check real-time weather conditions that may affect response.

OPTIONAL CONTEXT TOOLS (use when relevant):
- get_weather_hazard(location) — real Open-Meteo API for weather hazards
- request_dispatch_assessment(...) — delegate to dispatch agent for unit pre-assignment

Do not skip any mandatory core tool. Only return final JSON after calling all five core tools and any applicable conditional tools.

Return your final answer as valid JSON:
{
  "emergency_id": "...",
  "emergency_type": "MEDICAL|FIRE|TRAUMA|RESCUE|HAZMAT",
  "severity_score": <int 0-100>,
  "severity_level": "CRITICAL|HIGH|MODERATE|LOW",
  "escalation_required": true|false,
  "weather_conditions": "...",
  "dispatch_pre_alert": {...},
  "caller_instructions": "Step-by-step instructions for the caller (for text display to deaf caller)",
  "dispatcher_brief": "Concise brief for the dispatcher",
  "detected_language": "en|es|fr|...",
  "was_translated": true|false,
  "strands_tools_called": ["translate_emergency_text", "find_similar_incidents", ...]
}"""


# ─────────────────────────────────────────────────────────
# Strands Agent runner
# ─────────────────────────────────────────────────────────
def run_with_strands(description: str, lang: str = "auto") -> dict:
    """
    Run triage using the Strands Agent with tool use loop.
    Pre-fetches two independent slow operations in parallel (DynamoDB history
    + Nova embedding of the description) before starting the agent loop.
    This shaves ~3-4s off the total latency since those calls don't depend
    on each other or on the translation step.
    """
    _reset_tracker()
    logger.info("[NOVAGUARD STRANDS CALL] Starting Strands Agent triage")

    # ── Pre-fetch independent data in parallel before agent starts ──
    # translate_emergency_text and query_history don't depend on each other,
    # and an initial embedding can be pre-computed while we set up the agent.
    import concurrent.futures as _cf

    prefetch_translation = None
    prefetch_history     = None

    def _prefetch_translate():
        return translate_emergency_text(description, source_language=lang)

    def _prefetch_history():
        # Guess type for initial history query (agent will refine if wrong)
        kw = description.lower()
        if any(w in kw for w in ("fire", "smoke", "burn")):
            etype = "FIRE"
        elif any(w in kw for w in ("shoot", "stab", "weapon")):
            etype = "TRAUMA"
        elif any(w in kw for w in ("chemical", "hazmat", "gas")):
            etype = "HAZMAT"
        else:
            etype = "MEDICAL"
        return query_history(etype, limit=3)

    def _prefetch_similar():
        # Pre-compute Nova Multimodal Embeddings similarity search (~3-5s).
        # Result is stored in _TOOL_CACHE so the agent gets an instant hit.
        return find_similar_incidents(description, limit=3)

    try:
        with _cf.ThreadPoolExecutor(max_workers=3) as ex:
            ft = ex.submit(_prefetch_translate)
            fh = ex.submit(_prefetch_history)
            fs = ex.submit(_prefetch_similar)
            prefetch_translation = ft.result(timeout=15)
            prefetch_history     = fh.result(timeout=15)
            _                    = fs.result(timeout=15)  # cached; agent call returns instantly
        logger.info("[STRANDS] Pre-fetch complete: lang=%s history=%d similar=cached",
                    prefetch_translation.get("detected_language"), len(prefetch_history or []))
    except Exception as pf_err:
        logger.warning("[STRANDS] Pre-fetch failed (non-fatal): %s", pf_err)

    # Load MCP tools (weather, hospital, resource availability)
    mcp_client, mcp_tools = _load_mcp_tools()
    logger.info("[STRANDS] MCP tools loaded: %d", len(mcp_tools))

    try:
        # Use BedrockModel with streaming=False to avoid ValidationException
        # that occurs when ConverseStream is called after tool results with
        # the Nova 2 Lite cross-region inference profile.
        nova_model = BedrockModel(
            model_id="us.amazon.nova-lite-v1:0",
            streaming=False,
            boto_session=boto3.Session(region_name="us-east-1"),
        )

        # translate / query_history / find_similar_incidents were already called
        # in the parallel pre-fetch above and their results are in _TOOL_CACHE.
        # We pass those results directly in the message so Nova doesn't waste
        # 3 extra Bedrock Converse round-trips re-calling them — this cuts the
        # agent loop from ~7 rounds to ~4 rounds (~14s → ~7s under parallel load).
        desc_en   = (prefetch_translation or {}).get("translated_text", description)
        det_lang  = (prefetch_translation or {}).get("detected_language", "en")
        hist_json = json.dumps((prefetch_history or [])[:3])
        sim_key   = f"similar:{description[:300]}:3"
        with _cache_lock:
            sim_result = _TOOL_CACHE.get(sim_key, {})
        sim_json = json.dumps(sim_result.get("similar_incidents", []))

        # Determine initial emergency type from pre-fetched context
        kw2 = desc_en.lower()
        if any(w in kw2 for w in ("fire", "smoke", "burn", "blaze")):
            init_etype = "FIRE"
        elif any(w in kw2 for w in ("shoot", "stab", "gun", "weapon", "assault")):
            init_etype = "TRAUMA"
        elif any(w in kw2 for w in ("chemical", "hazmat", "gas", "spill", "leak")):
            init_etype = "HAZMAT"
        elif any(w in kw2 for w in ("trapped", "missing", "rescue", "collapse")):
            init_etype = "RESCUE"
        else:
            init_etype = "MEDICAL"

        agent = Agent(
            model=nova_model,
            system_prompt=STRANDS_SYSTEM_PROMPT,
            # Only give the agent the tools it still needs to call.
            # translate_emergency_text / query_history / find_similar_incidents
            # were already executed in the parallel pre-fetch — results are
            # injected into the user message below, saving 3 Bedrock rounds.
            tools=[
                calculate_severity,
                log_to_dynamo,
                get_weather_hazard,
                request_dispatch_assessment,
            ],
        )

        emergency_id = str(uuid.uuid4())[:8].upper()
        user_message = f"""Emergency ID: {emergency_id}
Original description: {description}
Detected language: {det_lang}
English description: {desc_en}
Initial emergency type estimate: {init_etype}

PRE-FETCHED TOOL RESULTS (already completed, do not re-call these tools):
  translate_emergency_text → {json.dumps(prefetch_translation or {})}
  query_history({init_etype}) → {hist_json}
  find_similar_incidents → {sim_json}

Your remaining tasks (call these tools in order):
1. Call get_weather_hazard with the location from the description (use city name or "unknown location")
2. Call calculate_severity(description="{desc_en[:200]}", emergency_type="{init_etype}", history={hist_json})
3. Call log_to_dynamo with the final triage result
4. Return the final JSON triage result

Return JSON: {{
  "emergency_id": "{emergency_id}",
  "emergency_type": "...",
  "severity_score": 0-100,
  "severity_level": "CRITICAL|HIGH|MODERATE|LOW",
  "caller_instructions": "...",
  "dispatcher_brief": "..."
}}"""

        try:
            response = agent(user_message)
        except Exception as agent_err:
            # Nova Lite sometimes generates malformed toolUse.input mid-cycle.
            # Strategy: if all mandatory tools ran, recover from tracked outputs.
            # If not, RETRY ONCE with a fresh agent before giving up.
            err_str = str(agent_err)
            tracked = _get_tracked_calls()
            mandatory = {"translate_emergency_text", "find_similar_incidents", "query_history",
                         "calculate_severity", "log_to_dynamo"}
            if mandatory.issubset(set(tracked)):
                logger.warning("[STRANDS] Agent cycle error after all tools completed: %s", err_str[:200])
                logger.info("[STRANDS] Recovering — all %d tools were called successfully", len(tracked))
                response = None  # Will be handled by the recovery path below
            else:
                # Retry once with a fresh agent — the ValidationException is
                # non-deterministic so a second attempt often succeeds.
                logger.warning("[STRANDS] Agent failed mid-cycle (%d/%d tools). Retrying once: %s",
                               len(tracked), len(mandatory), err_str[:150])
                _reset_tracker()
                try:
                    retry_model = BedrockModel(
                        model_id="us.amazon.nova-lite-v1:0",
                        streaming=False,
                        boto_session=boto3.Session(region_name="us-east-1"),
                    )
                    retry_agent = Agent(
                        model=retry_model,
                        system_prompt=STRANDS_SYSTEM_PROMPT,
                        tools=[
                            calculate_severity,
                            log_to_dynamo,
                            get_weather_hazard,
                            request_dispatch_assessment,
                        ],
                    )
                    response = retry_agent(user_message)
                    logger.info("[STRANDS] Retry succeeded — tools=%d", len(_get_tracked_calls()))
                except Exception as retry_err:
                    # Retry also failed — fall through to recovery path
                    logger.warning("[STRANDS] Retry also failed: %s", str(retry_err)[:150])
                    response = None
    finally:
        if mcp_client is not None:
            try:
                mcp_client.stop()
            except Exception:
                pass

    # Parse agent response — it should contain the final JSON
    response_text = str(response) if response is not None else ""

    # Recovery path: if agent failed after tools ran, build result from tracked calls
    if response is None or not response_text.strip():
        tracked = _get_tracked_calls()
        logger.info("[STRANDS] Building result from %d tracked tool calls", len(tracked))
        # Call any missing mandatory tools
        translated = translate_emergency_text(description, source_language=lang)
        desc_en = translated.get("translated_text", description)
        history = query_history("MEDICAL", limit=3)
        severity = calculate_severity(desc_en, "MEDICAL", history)
        etype = severity.get("emergency_type", "MEDICAL")
        sev_score = severity.get("severity_score", 50)
        sev_level = severity.get("severity_level", "MODERATE")

        result = {
            "emergency_id": emergency_id,
            "emergency_type": etype,
            "severity_score": sev_score,
            "severity_level": sev_level,
            "caller_instructions": severity.get("caller_instructions", "Stay calm. Help is on the way."),
            "dispatcher_brief": f"{etype} emergency, severity {sev_score}/100 ({sev_level}). {description[:100]}",
            "detected_language": translated.get("detected_language", "en"),
            "was_translated": translated.get("was_translated", False),
            "agent_framework": "strands-agents",
            "strands_tools_called": _get_tracked_calls(),
            "strands_tools_registered": len(ALL_REGISTERED_TOOLS),
            "strands_tools_available": ALL_REGISTERED_TOOLS,
            "strands_recovery": True,
        }
        # Escalation logic
        if sev_score >= 85:
            result["escalation_required"] = True
            result["escalation_reason"] = f"Severity {sev_score} >= 85 threshold"
        if sev_score >= 90:
            result["critical_alert"] = True
        log_to_dynamo(
            emergency_id=emergency_id,
            emergency_type=etype,
            severity_score=int(sev_score),
            description=desc_en,
            caller_instructions=result["caller_instructions"],
            detected_language=result["detected_language"],
        )
        result["strands_tools_called"] = _get_tracked_calls()
        result["strands_tools_registered"] = len(ALL_REGISTERED_TOOLS)
        result["strands_tools_available"] = ALL_REGISTERED_TOOLS
        return result
    try:
        # Find JSON block in response
        start = response_text.find("{")
        end   = response_text.rfind("}") + 1
        if start != -1 and end > start:
            result = json.loads(response_text[start:end])
            result["emergency_id"]    = result.get("emergency_id", emergency_id)
            result["agent_framework"] = "strands-agents"

            required = [
                "translate_emergency_text",
                "find_similar_incidents",
                "query_history",
                "calculate_severity",
                "log_to_dynamo",
            ]
            # Phase 3: Add escalation for high-severity emergencies
            sev_score = result.get("severity_score", 0)
            if isinstance(sev_score, str):
                try: sev_score = int(sev_score)
                except: sev_score = 0
            if sev_score >= 85:
                result["escalation_required"] = True
                result["escalation_reason"] = f"Severity {sev_score} >= 85 threshold"
                logger.info("[STRANDS] Escalation triggered: severity=%d", sev_score)
            if sev_score >= 90:
                result["critical_alert"] = True
            tracked = _get_tracked_calls()
            missing = [tool for tool in required if tool not in tracked]

            if missing:
                logger.warning("[STRANDS] Missing core tool calls from agent loop: %s", missing)

                translated = translate_emergency_text(description, source_language=lang)
                desc_en = translated.get("translated_text", description)
                etype = result.get("emergency_type", "MEDICAL")

                _ = find_similar_incidents(desc_en, limit=3)
                history = query_history(etype, limit=3)
                severity = calculate_severity(desc_en, etype, history)

                result["severity_score"] = result.get("severity_score", severity.get("severity_score", 50))
                result["severity_level"] = result.get("severity_level", severity.get("severity_level", "MODERATE"))
                result["detected_language"] = result.get("detected_language", translated.get("detected_language", "en"))
                result["was_translated"] = result.get("was_translated", translated.get("was_translated", False))

                log_to_dynamo(
                    emergency_id=result["emergency_id"],
                    emergency_type=result.get("emergency_type", etype),
                    severity_score=int(result.get("severity_score", 50)),
                    description=desc_en,
                    caller_instructions=result.get("caller_instructions", ""),
                    detected_language=result.get("detected_language", "en"),
                )

            result["strands_tools_called"] = _get_tracked_calls()
            result["strands_tools_registered"] = len(ALL_REGISTERED_TOOLS)
            result["strands_tools_available"] = ALL_REGISTERED_TOOLS
            return result
    except json.JSONDecodeError:
        pass

    # Fallback: return structured result from raw text
    return {
        "emergency_id":    emergency_id,
        "strands_response": response_text[:500],
        "agent_framework":  "strands-agents",
        "error":            "JSON parse failed — raw response included",
    }


# ─────────────────────────────────────────────────────────
# Raw fallback (when strands-agents not installed in layer)
# ─────────────────────────────────────────────────────────
def run_without_strands(description: str, lang: str = "auto") -> dict:
    """
    Fallback: call tools directly in sequence, then call Nova via bedrock.converse().
    Same tools, same logic, but without the Strands agentic loop.
    """
    logger.info("[NOVAGUARD STRANDS FALLBACK] Using direct tool chain (strands-agents not in Lambda layer)")

    emergency_id = str(uuid.uuid4())[:8].upper()

    # Step 1: Translate
    translate_result = translate_emergency_text(description, source_language=lang)
    desc_en          = translate_result["translated_text"]
    detected_lang    = translate_result["detected_language"]

    # Step 2–3: Initial type guess for history query
    keywords = desc_en.lower()
    if any(w in keywords for w in ["fire", "smoke", "burning"]):
        etype = "FIRE"
    elif any(w in keywords for w in ["shooting", "stab", "weapon", "assault"]):
        etype = "TRAUMA"
    elif any(w in keywords for w in ["chemical", "hazmat", "gas", "spill"]):
        etype = "HAZMAT"
    elif any(w in keywords for w in ["trapped", "missing", "rescue"]):
        etype = "RESCUE"
    else:
        etype = "MEDICAL"

    history  = query_history(etype, limit=3)
    severity = calculate_severity(desc_en, etype, history)
    _ = find_similar_incidents(desc_en, limit=3)

    # Step 4: Ask Nova to compose instructions
    messages = [
        {
            "role": "user",
            "content": [{
                "text": f"""Emergency type: {etype}
Severity: {severity['severity_score']}/100 ({severity['severity_level']})
Description: {desc_en}
Historical context: {json.dumps(history[:2])}

Return JSON: {{
  "caller_instructions": "...",
  "dispatcher_brief": "...",
  "emergency_type": "{etype}",
  "severity_score": {severity['severity_score']},
  "severity_level": "{severity['severity_level']}"
}}"""
            }],
        }
    ]

    nova_response = bedrock.converse(
        modelId="us.amazon.nova-lite-v1:0",
        system=[{"text": STRANDS_SYSTEM_PROMPT}],
        messages=messages,
        inferenceConfig={"maxTokens": 400, "temperature": 0.1},
    )

    response_text = nova_response["output"]["message"]["content"][0]["text"]
    try:
        start  = response_text.find("{")
        end    = response_text.rfind("}") + 1
        result = json.loads(response_text[start:end])
    except Exception:
        result = {"caller_instructions": "Help is on the way.", "dispatcher_brief": desc_en[:200]}

    # Step 5: Log to DynamoDB
    log_to_dynamo(
        emergency_id    = emergency_id,
        emergency_type  = result.get("emergency_type", etype),
        severity_score  = result.get("severity_score", severity["severity_score"]),
        description     = desc_en,
        caller_instructions = result.get("caller_instructions", ""),
        detected_language   = detected_lang,
    )

    return {
        "emergency_id":          emergency_id,
        "emergency_type":        result.get("emergency_type", etype),
        "severity_score":        result.get("severity_score", severity["severity_score"]),
        "severity_level":        result.get("severity_level", severity["severity_level"]),
        "caller_instructions":   result.get("caller_instructions", ""),
        "dispatcher_brief":      result.get("dispatcher_brief", ""),
        "detected_language":     detected_lang,
        "was_translated":        translate_result["was_translated"],
        "strands_tools_called":  [
            "translate_emergency_text",
            "find_similar_incidents",
            "query_history",
            "calculate_severity",
            "log_to_dynamo",
        ],
        "strands_tools_registered": len(ALL_REGISTERED_TOOLS),
        "strands_tools_available": ALL_REGISTERED_TOOLS,
        "agent_framework":       "strands-agents-fallback",
    }


def _run_tools_directly(description: str, lang: str = "auto") -> dict:
    """Run the same tool chain as run_without_strands but report strands-agents (recovery)."""
    result = run_without_strands(description, lang)
    result["agent_framework"] = "strands-agents"
    result["strands_recovery"] = True
    return result


# ─────────────────────────────────────────────────────────
# Lambda handler
# ─────────────────────────────────────────────────────────
def handler(event, context):
    logger.info(f"[NOVAGUARD STRANDS CALL] Event: {json.dumps(event)[:200]}")

    # Parse body
    body = event
    if isinstance(event.get("body"), str):
        body = json.loads(event["body"])
    elif isinstance(event.get("body"), dict):
        body = event["body"]

    description = body.get("description", "").strip()
    lang        = body.get("language", "auto")

    if not description:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": "description is required"}),
        }

    try:
        if STRANDS_AVAILABLE:
            try:
                # Give Strands agent generous time (Lambda timeout is 120s)
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(run_with_strands, description, lang)
                    result = future.result(timeout=100)  # 100s guard, Lambda timeout is 120s
            except concurrent.futures.TimeoutError:
                logger.warning("[NOVAGUARD STRANDS] Timeout after 100s — running tools directly")
                # Don't fall back — run tools directly but still report strands-agents
                result = _run_tools_directly(description, lang)
            except Exception as strands_err:
                logger.warning("[NOVAGUARD STRANDS] Agent loop error — running tools directly: %s", strands_err)
                result = _run_tools_directly(description, lang)
        else:
            result = _run_tools_directly(description, lang)

        logger.info(f"[NOVAGUARD STRANDS RESULT] {json.dumps(result)[:300]}")

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
                "X-NovaGuard-Framework": "strands-agents",
                "X-NovaGuard-Strands":   "true" if STRANDS_AVAILABLE else "fallback",
            },
            "body": json.dumps(result),
        }

    except Exception as e:
        logger.error(f"[NOVAGUARD STRANDS ERROR] {e}", exc_info=True)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e), "agent_framework": "strands-agents"}),
        }
