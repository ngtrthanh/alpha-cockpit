from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from runtime_db import save_agent_report, set_service_status

HERMES_URL = os.getenv("HERMES_URL", "http://hermes:8642").rstrip("/")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")

CODEX_PROVIDER = os.getenv("CODEX_PROVIDER", "openai-codex")
CODEX_MODEL = os.getenv("CODEX_MODEL", "")
CLAUDE_PROVIDER = os.getenv("CLAUDE_PROVIDER", "anthropic")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "")


@dataclass
class AgentResult:
    role: str
    provider: str
    model: str | None
    content: str
    usage: dict[str, Any]
    raw: dict[str, Any]


class HermesClient:
    def __init__(self, base_url: str = HERMES_URL, api_key: str = HERMES_API_KEY):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, path: str, payload: dict[str, Any] | None = None, timeout: int = 180) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"Hermes HTTP {e.code}: {body[:1000]}") from e
        except Exception as e:
            raise RuntimeError(f"Hermes unavailable: {e}") from e

    def health(self) -> dict[str, Any]:
        try:
            data = self._request("/health", timeout=10)
            status = "ok" if data.get("status") == "ok" else "degraded"
            set_service_status("hermes", status, detail=data)
            return data
        except Exception as e:
            set_service_status("hermes", "error", error=str(e))
            raise

    def chat(
        self,
        role: str,
        messages: list[dict[str, str]],
        *,
        provider: str | None = None,
        model: str | None = None,
        ticker: str | None = None,
        timeout: int = 300,
    ) -> AgentResult:
        payload: dict[str, Any] = {
            "model": model or "hermes-agent",
            "messages": messages,
            "stream": False,
        }
        if provider:
            payload["provider"] = provider
        raw = self._request("/v1/chat/completions", payload, timeout=timeout)
        content = raw["choices"][0]["message"]["content"]
        usage = raw.get("usage") or {}
        result = AgentResult(role, provider or "hermes-default", model, content, usage, raw)
        save_agent_report(role, result.provider, model, "ok", content, ticker=ticker, usage=usage)
        return result

    def supervisor(self, prompt: str, *, ticker: str | None = None) -> AgentResult:
        return self.chat(
            "hermes-supervisor",
            [
                {"role": "system", "content": "You are the Alpha Cockpit supervisor. Coordinate analysis, expose uncertainty, preserve source provenance, and never place brokerage orders."},
                {"role": "user", "content": prompt},
            ],
            ticker=ticker,
        )

    def codex(self, prompt: str, *, ticker: str | None = None) -> AgentResult:
        if not CODEX_MODEL:
            raise RuntimeError("CODEX_MODEL is not configured")
        return self.chat(
            "codex-executor",
            [
                {"role": "system", "content": "You are the Codex executor for Alpha Cockpit. Validate calculations, data transformations, portfolio rules and code-level logic. Return concise findings; do not issue brokerage orders."},
                {"role": "user", "content": prompt},
            ],
            provider=CODEX_PROVIDER,
            model=CODEX_MODEL,
            ticker=ticker,
        )

    def claude(self, prompt: str, *, ticker: str | None = None) -> AgentResult:
        if not CLAUDE_MODEL:
            raise RuntimeError("CLAUDE_MODEL is not configured")
        return self.chat(
            "claude-reviewer",
            [
                {"role": "system", "content": "You are the independent Claude reviewer for Alpha Cockpit. Challenge the thesis, identify missing risks, stale evidence and contradictory facts. Do not issue brokerage orders."},
                {"role": "user", "content": prompt},
            ],
            provider=CLAUDE_PROVIDER,
            model=CLAUDE_MODEL,
            ticker=ticker,
        )

    def committee(self, context: dict[str, Any], *, ticker: str | None = None) -> dict[str, Any]:
        packed = json.dumps(context, ensure_ascii=False, default=str)
        plan = self.supervisor(
            "Review this investment snapshot and define the exact validation questions for the executor and reviewer.\n\n" + packed,
            ticker=ticker,
        )
        codex = self.codex(plan.content + "\n\nValidate against this snapshot:\n" + packed, ticker=ticker)
        claude = self.claude(plan.content + "\n\nIndependently challenge this snapshot and the proposed checks:\n" + packed, ticker=ticker)
        final = self.supervisor(
            "Synthesize an investment-committee review from the snapshot and two independent reviews. "
            "Return: status (OK/WATCH/REVIEW/ALERT), thesis_delta, key_evidence, key_risks, missing_data, and next_review_trigger. "
            "Do not output an executable order.\n\nSNAPSHOT:\n"
            + packed
            + "\n\nCODEX:\n"
            + codex.content
            + "\n\nCLAUDE:\n"
            + claude.content,
            ticker=ticker,
        )
        return {
            "supervisor_plan": plan.content,
            "codex_review": codex.content,
            "claude_review": claude.content,
            "committee": final.content,
        }
