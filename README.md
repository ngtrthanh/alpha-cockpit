# Alpha Cockpit

A Vietnamese practical stock-investing handbook plus an explainable decision-support cockpit.

## What is included

- 38-section practical handbook.
- SQLite transaction/target/benchmark/market/fundamental/score/audit schema.
- Deterministic 0–100 Decision Index with separate confidence.
- Portfolio policy: 400M equity sleeve, 60% ETF core, individual-stock caps, margin 0, derivatives 0, 8.8% deposit hurdle.
- Hermes / Codex / Claude bridge points.
- Demo data for UI development only.
- No broker order submission code.

## Run locally

```bash
python app.py
```

Open `http://localhost:6868`.

## Run with Docker

```bash
docker compose up --build
```

Open `http://localhost:6868`.

The Docker image uses Python 3.15.1. The application itself intentionally uses only Python standard-library modules, including `sqlite3`, to keep deployment lean.

## External agents

Optional environment variables:

```text
HERMES_WEBHOOK_URL=
CODEX_REVIEW_COMMAND=
CLAUDE_REVIEW_COMMAND=
```

If absent, the local deterministic engine remains fully usable. External agents must not receive broker credentials with write permission.

## Production data adapters

Replace demo fundamentals/market snapshots with verified adapters. Each record should retain `source`, timestamp and raw value provenance. The numeric score should be recomputed from deterministic rules; LLM review is stored alongside it, not injected invisibly.
