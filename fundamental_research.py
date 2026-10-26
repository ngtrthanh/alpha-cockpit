from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from agent_runtime import HermesClient
from runtime_db import connect, now_iso, set_service_status


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith(fence):
            text = text.rstrip()[:-3].rstrip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def research_ticker(client: HermesClient, ticker: str) -> dict[str, Any]:
    prompt = f"""
Use the alpha-cockpit skill. Research {ticker} using current primary or high-quality sources.
Prefer official company financial reports/disclosures, exchange disclosures, official ETF factsheets,
and current SSI/market information when available. Do not invent missing KPIs.

Return ONLY valid JSON with this schema:
{{
  "ticker": "{ticker}",
  "as_of": "ISO-8601 or report period",
  "raw_kpis": {{"name": {{"value": null, "unit": "text", "period": "text", "source": "url-or-title"}}}},
  "components": {{
    "quality": 0,
    "growth": 0,
    "valuation": 0,
    "momentum": 0,
    "financial_strength": 0,
    "risk": 0,
    "liquidity": 0,
    "macro": 0
  }},
  "data_completeness": 0.0,
  "freshness_hours": 0,
  "evidence": [{{"source": "title", "url": "url if available", "claim": "what it supports"}}],
  "missing_data": ["..."]
}}

Replace example numeric zeros with evidence-based 0-100 scores. Risk score is higher when risk is lower.
Component scores are normalized research judgments, not trade commands.
"""
    result = client.supervisor(prompt, ticker=ticker)
    parsed = _extract_json(result.content)
    components = parsed.get("components") or {}
    required = {"quality", "growth", "valuation", "momentum", "financial_strength", "risk", "liquidity", "macro"}
    if not required.issubset(components):
        raise ValueError(f"research result missing components: {sorted(required - set(components))}")
    metrics = {k: float(components[k]) for k in required}
    metrics["data_completeness"] = float(parsed.get("data_completeness", 0))
    metrics["freshness_hours"] = float(parsed.get("freshness_hours", 9999))
    evidence = parsed.get("evidence") or []
    source = "hermes-research"
    ts = now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO fundamentals(ticker,sector,payload,updated_at,source)
               VALUES(?,?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 payload=excluded.payload,updated_at=excluded.updated_at,source=excluded.source""",
            (ticker.upper(), "", json.dumps(metrics, ensure_ascii=False), ts, source),
        )
        conn.execute(
            "INSERT INTO research_snapshots(ticker,ts,source,payload,evidence_json,confidence) VALUES(?,?,?,?,?,?)",
            (
                ticker.upper(),
                ts,
                source,
                json.dumps(parsed, ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False),
                metrics["data_completeness"],
            ),
        )
    set_service_status(
        "fundamental_research",
        "ok",
        detail={"ticker": ticker, "as_of": parsed.get("as_of"), "evidence_count": len(evidence)},
    )
    return parsed


def needs_refresh(ticker: str, max_age_hours: float = 24.0) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT updated_at,source FROM fundamentals WHERE ticker=? AND source<>'demo'",
            (ticker.upper(),),
        ).fetchone()
    if not row:
        return True
    try:
        dt = datetime.fromisoformat(row["updated_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return age >= max_age_hours
    except Exception:
        return True
