# ScenarioIQ

Scenario simulation for crisis and policy planning. Upload a briefing, describe a decision, and ScenarioIQ builds a simulated population of the parties in the document, runs the scenario over many rounds, and writes up the result. You can then question the analyst or any single simulated party.

## Status

v2 in development. English only. Single user.

- **P0 (branch `p0-world-model`):** a reviewed world model in SQLite, provenance tags, an append-only event log, truth kept separate from agent knowledge, JSON rules, and a deterministic action slice. See [`docs/v2-p0-world-model.md`](docs/v2-p0-world-model.md). The UI is at `/world`.
- **Legacy pipeline:** ontology, then Zep graph, then OASIS, then report. It is unchanged and runs beside P0.

## Run

```bash
cp .env.example .env   # add LLM and Zep keys
docker compose up -d --build
```

Frontend: port 3000. Backend API: port 5001.

Local dev without Docker:

```bash
npm run setup:all
npm run dev
```

## Stack

- Backend: Flask, OASIS (camel-oasis) multi-agent simulation, Zep Cloud knowledge graph
- Frontend: Vue 3, Vite, D3

## Licence and attribution

ScenarioIQ is a derivative of [MiroFish](https://github.com/666ghj/MiroFish) and is distributed under AGPL-3.0 (see `LICENSE`).
