# Alpha Cockpit architecture

## Roles

- **Hermes = supervisor**: dispatches research/data/review work and merges evidence into a review package.
- **Codex = builder/executor**: maintains database/importers/calculations/rules/tests/data adapters.
- **Claude = reviewer**: second-opinion review of annual reports, financial statements, research reports and thesis/risk deltas.
- **Deterministic scoring engine** remains authoritative for the numeric Decision Index. LLM outputs do not silently alter score weights.

## Safety boundary

There is deliberately no broker order endpoint. Brokerage integration, if added, should be read-only. The app can propose a review tranche but cannot submit BUY/SELL orders.

## Decision Index

Weighted score 0–100:

- Quality 18%
- Growth 14%
- Valuation 17%
- Momentum 8%
- Financial strength 14%
- Risk 12%
- Liquidity 7%
- Macro/sector 10%

Confidence is separate from the index. It is reduced by stale/incomplete data and high disagreement among components. Low confidence forces `REVIEW_DATA` and zero suggested sizing.

Workflow statuses are intentionally limited to:

- `OK`: policy/score does not require special attention
- `WATCH`: opportunity or policy drift worth monitoring
- `REVIEW`: material opportunity, deterioration or data gap requires human review
- `ALERT`: severe risk or policy breach

A separate `allocation_bias` may be `ADD_TO_TARGET`, `HOLD_TARGET`, `REDUCE_TO_POLICY` or `DATA_GAP`. It describes direction for review, not an order.

These are workflow states, not automatic orders.

## Sizing

Suggested value is bounded by:

1. portfolio equity budget,
2. target weight,
3. max weight,
4. remaining target gap,
5. score strength,
6. confidence.

It never proposes more than 50% of the remaining target gap in one tranche. For listed HOSE-style examples, the candidate quantity is rounded down to 100-share lots. Timing is a separate state (`FAVORABLE`, `WAIT_CONFIRMATION`, `NEUTRAL`, `DEFENSIVE`, `DATA_WAIT`) derived from score, confidence and momentum; it is not an execution signal.

## Data model

- `transactions`: date, ticker, side, qty, price, fee, tax, note
- `targets`: target/min/max weights and asset type
- `benchmarks`: deposit hurdle and later VNINDEX/other benchmarks
- `market_snapshots`: timestamped market prices and source
- `fundamentals`: sector + KPI payload + source/freshness
- `score_runs` + `score_components`: reproducible scoring history
- `agent_runs`: external agent calls and status
- `audit_log`: immutable-ish operational trail for local use

SQLite uses WAL mode. Keep the DB on a local filesystem, not a network filesystem.

## Sector KPI packs

The app ships starter packs for general, bank, technology, steel and ETF. They define which raw KPIs an ingestion/calculation layer should normalize into the eight score components. A production implementation should version every normalization rule and preserve raw source values.

## Sub-agents under Hermes

Codex can run deterministic sub-agents for Quality, Growth, Valuation, Momentum, Financial Strength, Risk, Liquidity and Macro/Sector. Each produces a normalized 0–100 component plus evidence. Hermes merges the package; Claude reviews thesis/risk documents. The numeric engine uses the stored component values and fixed versioned weights.
