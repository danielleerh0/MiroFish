# ScenarioIQ

Scenario simulation for crisis and policy planning. Upload a briefing, describe a decision, and ScenarioIQ builds a simulated population of the parties in the document, runs the scenario over many rounds, and writes up the result. You can then question the analyst or any single simulated party.

## Status

v2 in development. English only. Single user.

Planned v2 changes:

1. Store the seed landscape (ontology, knowledge graph reference, agent profiles) in SQLite, so you build it once and reuse it.
2. Run secondary simulations that branch from a stored landscape, to test the effect of one change against a baseline.

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
