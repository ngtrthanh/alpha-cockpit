# Alpha Cockpit

A human-in-the-loop investment cockpit for Vietnamese equities and ETFs. Market data is pluggable and currently disabled by default.

Alpha Cockpit is **not a robo-trader**. It runs real market-data ingestion, a real Hermes supervisor, Codex/Claude review routes, deterministic scoring, and two paper-trading tracks. No brokerage write API is implemented.

## Runtime

```text
market-data adapter (optional)
          |
          v
   market_events / SQLite WAL
          |
          v
  deterministic score engine
          |
          v
     Hermes supervisor
      /           \
 Codex executor   Claude reviewer
      \           /
       committee review
          |
          v
  Track A: BUY-only paper
  Track B: full-cycle paper
```

Services in `compose.yaml`:

- `hermes`: official `nousresearch/hermes-agent:stable`, internal API on port 8642.
- `alpha-cockpit`: web/API on port **6868**.
- `worker`: research refresh, scoring, agent committee, optional market-data adapter, and paper fills.

The Hermes API is not exposed to the public host. Alpha Cockpit reaches it through the internal Docker network.

## 1. Configure environment

```bash
cp .env.example .env
openssl rand -hex 32
```

Put the generated value into:

```text
HERMES_API_KEY=<generated secret>
```

Market data is currently disabled by default:

```text
MARKET_DATA_PROVIDER=none
PAPER_ENABLED=false
```

This lets Hermes/Codex/Claude research and scoring run without SSI. Paper trading remains disabled until a real market-data provider is connected.

Configure the model routes after Hermes setup:

```text
CODEX_PROVIDER=openai-codex
CODEX_MODEL=<model visible in your Hermes setup>

CLAUDE_PROVIDER=anthropic
CLAUDE_MODEL=<model visible in your Hermes setup>
```

Model names are configuration, not hard-coded guesses.

## 2. Configure Hermes

Create the persistent data directory:

```bash
mkdir -p hermes-data
```

Run the official setup wizard:

```bash
docker compose run --rm -it hermes setup
```

For the simplest fully tooled supervisor, Hermes also documents Nous Portal setup:

```bash
docker compose run --rm -it hermes setup --portal
```

Configure/add the Codex subscription/provider and Anthropic credentials through Hermes:

```bash
docker compose run --rm -it hermes model
docker compose run --rm -it hermes auth add anthropic
```

Hermes persists configuration, OAuth state, memory, and skills under `./hermes-data`, which is gitignored.

The repository mounts `hermes/skills/alpha-cockpit/SKILL.md` read-only into the Hermes skill directory.

## 3. Start the real stack

```bash
docker compose pull hermes
docker compose up -d --build
docker compose ps
```

Open:

```text
http://localhost:6868
```

Check logs:

```bash
docker compose logs -f hermes
docker compose logs -f worker
docker compose logs -f alpha-cockpit
```

The **Runtime** tab shows real service heartbeats. Market data shows `disabled` when no provider is selected; the system does not substitute synthetic quotes.

## 4. Verify live mode

```bash
curl -s http://localhost:6868/api/health
curl -s http://localhost:6868/api/runtime
curl -s http://localhost:6868/api/paper
```

Expected health includes:

```json
{
  "ok": true,
  "mode": "live",
  "demo_data": false,
  "broker_write_access": false
}
```

## Data paths

- `./data/investment.db`: portfolio, market events, research, signals, paper orders/fills, agent reports.
- `./hermes-data`: Hermes config/auth/memory. Never commit this directory.

SQLite runs in WAL mode and is shared only across the local Docker services on the same host filesystem.

## Live data policy

There is no synthetic-data runtime mode.

In live mode:

- no hard-coded FPT/HPG/MBB/ETF prices exist in the runtime;
- no synthetic fundamentals are used;
- scores appear only after live research has been stored;
- paper orders require actual records from the market-event table;
- service failures remain visible instead of being hidden.

## Market data

Market data is a pluggable adapter. The default is:

```text
MARKET_DATA_PROVIDER=none
```

No SSI credential is required. Without a market-data provider, research, scoring and committee review still run, but paper orders/fills remain inactive.

## Fundamental research

Until a dedicated licensed structured-fundamental provider is configured, Hermes performs evidence-based research and stores:

- raw KPIs it found;
- normalized component scores;
- evidence/source list;
- missing data;
- data completeness and freshness.

The weighted Decision Index itself remains deterministic once the component payload has been stored. Agent-derived components remain distinguishable by `source=hermes-research`.

## Agent committee

For a ticker with fundamental research data:

1. Hermes supervisor frames validation questions.
2. Codex validates calculations/data transformations/policy logic through an explicit Hermes provider route.
3. Claude independently challenges thesis/risk through an explicit Hermes provider route.
4. Hermes synthesizes the committee review.

All reports are written to `agent_reports`.

If Codex or Claude credentials/model names are missing, the committee run fails visibly. It does not fake a second opinion.

## Paper trading

Two independent accounts start with 400M VND each:

- `A_BUY_ONLY`: BUY orders only.
- `B_FULL_CYCLE`: BUY and SELL.

Paper order state:

```text
OPEN -> PARTIAL -> FILLED
```

A signal is not a fill. Paper trading is disabled by default. When a real market-data adapter is later enabled, orders are filled only when a recorded market event crosses the limit price. Duplicate open orders are suppressed and position size is capped by portfolio max-weight policy.

## Self-deploy to OCI

Production self-deploy is documented in [docs/SELF_DEPLOY.md](docs/SELF_DEPLOY.md).

The release path is: successful CI on main -> multi-arch GHCR image tagged with the exact commit SHA -> dedicated OCI ARM64 self-hosted runner -> SQLite backup -> exact-image deploy -> app/worker health gate -> automatic rollback on failure.

The normal production deploy path uses --no-build on OCI, so the server runs the image produced for the tested commit rather than rebuilding an untracked variant locally.

## Development

```bash
docker build -t alpha-cockpit:test .
docker run --rm alpha-cockpit:test python -m unittest discover -s tests -v
HERMES_API_KEY=ci-hermes-key-123456 docker compose config
```

## Safety boundary

There is deliberately no broker-order endpoint in this repository.

```text
analysis -> review -> paper order -> paper fill
```

does not become:

```text
analysis -> real brokerage order
```

Real-money execution remains manual and outside Alpha Cockpit.
