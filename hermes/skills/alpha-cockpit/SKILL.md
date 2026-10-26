---
name: alpha-cockpit
description: Review Vietnamese equity/ETF evidence for Alpha Cockpit without placing orders.
version: 1.0.0
author: ngtrthanh
license: MIT
platforms: [linux]
---

# Alpha Cockpit

Use this skill when reviewing a ticker, ETF, portfolio snapshot, financial statement, market-data anomaly, or weekly investment committee package for Alpha Cockpit.

## Rules

1. Never place, modify, cancel, or suggest an executable brokerage order.
2. Treat market data and financial data as separate evidence streams; report timestamp and source freshness.
3. Prefer primary evidence: audited/quarterly company reports, exchange/company disclosures, official fund factsheets, SSI market data. Use secondary research only as corroboration.
4. Distinguish facts from inference. Do not invent missing KPIs.
5. A high score does not override portfolio policy, concentration limits, data confidence, or the 8.8% deposit hurdle.
6. When the task asks for a review, return a compact structured result with:
   - status: OK | WATCH | REVIEW | ALERT
   - thesis_delta
   - key_evidence
   - key_risks
   - missing_data
   - next_review_trigger
7. Codex is the calculation/execution reviewer; Claude is the independent thesis/risk reviewer. The Hermes supervisor reconciles disagreements.
8. Brokerage access is read-only by design.
