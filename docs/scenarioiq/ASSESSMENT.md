# ScenarioIQ — Phase 0 Assessment

Baseline commit: `dbcdebb` on `claude/dazzling-mendel-5bvluz` (114 commits; history is upstream 666ghj/MiroFish plus a handful of fork edits to `Home.vue`, `vite.config.js`, `index.js`).
Baseline tests: backend `129 passed` (`uv run pytest backend/tests`), root `28 passed` (star-history scripts). Frontend has **no** tests and no test runner.

Confidence tags: **[V]** verified in code this session, **[I]** inferred, not exercised at runtime.

---

## A. Current architecture (actual data flow)

```
Browser (Vue 3, vite dev server :3000, proxies /api -> :5001)
 ├─ Home.vue ── upload seed docs + "requirement" ──► POST /api/graph/ontology/generate
 │                                                    graph.py → OntologyGenerator (LLM) → ProjectManager (files)
 ├─ MainView/Process → Step1GraphBuild ──► POST /api/graph/build
 │                                          graph.py: TaskManager(mem) + daemon Thread → GraphBuilderService
 │                                          → TextProcessor chunks → Zep Cloud (create graph, batch add) ; poll GET /api/graph/task/<id>
 ├─ SimulationView → Step2EnvSetup ──► POST /api/simulation/create, /prepare (daemon Thread + TaskManager)
 │                                      ZepEntityReader → OasisProfileGenerator (LLM, ThreadPool) → SimulationConfigGenerator (LLM)
 │                                      writes uploads/simulations/<sim_id>/{state.json, *_profiles.*, simulation_config.json}
 ├─ SimulationRunView → Step3Simulation ──► POST /api/simulation/start
 │                                          SimulationRunner.start_simulation: Popen(run_parallel_simulation.py, start_new_session)
 │                                          + monitor daemon Thread tails twitter|reddit/actions.jsonl → run_state.json (+ in-mem dict)
 │                                          + ZepGraphMemoryUpdater daemon Thread → Zep Cloud
 │                                          browser polls GET /run-status (2s) and /run-status/detail (3s)
 ├─ ReportView → Step4Report ──► POST /api/report/generate (daemon Thread + TaskManager) → ReportAgent (LLM + ZepTools)
 └─ InteractionView → Step5Interaction ──► /api/simulation/interview/* → simulation_ipc (files) → OASIS subprocess env
```

## B. Current run lifecycle — why the window must stay open

The server *does* own the OASIS subprocess; the **browser owns the decision to start (and restart) it**, and several UI paths kill it.

| # | Evidence | Effect |
|---|---|---|
| 1 | `Step3Simulation.vue:691` `onMounted → doStartSimulation()` [V] | Merely visiting `/simulation/:id/start` (refresh, back button, bookmark, re-entry) issues a start. |
| 2 | `Step3Simulation.vue:401` hard-codes `force: true` [V] | Every mount stops the live run (`SimulationRunner.stop_simulation`, `api/simulation.py:1634`), **deletes its logs** (`cleanup_simulation_logs`, `:1658`) and restarts. A refresh mid-run destroys the run. |
| 3 | `api/simulation.py:1600-1678` [V] | Even **without** force, a COMPLETED/STOPPED/FAILED sim is reset to READY and restarted; `start_simulation` reopens `simulation.log` with mode `'w'` and rewrites `run_state.json`. Completed runs are silently overwritten by revisiting. |
| 4 | `SimulationRunView.vue:152-198` `handleGoBack` [V] | Navigating back stops the running sim (`closeSimulationEnv` → fallback `stopSimulation`). |
| 5 | `SimulationRunner.register_cleanup` (`:1554`), `cleanup_all_simulations` (`:1449`) [V] | SIGTERM/SIGINT/atexit of the *API process* stops every sim. Restarting the API (deploy, Flask reloader, `docker restart`) ends all runs. The API and the run share a fate. |
| 6 | `SimulationRunner._processes/_monitor_threads/_run_states` class-level dicts [V] | Process handles + monitor are in API memory. After a hard crash (SIGKILL/OOM) `run_state.json` still says `RUNNING`; nothing re-attaches a monitor or checks the pid (no `os.kill(pid,0)` anywhere) → **zombie "running" forever** [V by grep; behaviour I]. |
| 7 | `models/task.py:64` `TaskManager._tasks: Dict` singleton [V] | Graph-build / prepare / report progress lives in process memory. Restart ⇒ `GET .../task/<id>` returns 404 while the project still says `graph_building`. |
| 8 | Daemon threads: `graph.py:819`, `simulation.py:646`, `report.py:295`, `graph_builder.py:112`, `zep_graph_memory_updater.py:304`, `simulation_runner.py:554` [V] | All long work except the OASIS subprocess dies with the API process. |
| 9 | `axios` timeout 5 min, polling only while component mounted (`onUnmounted → stopPolling`) [V] | Observation stops on leave; there is no "adopt" path on return because #1/#2 restart instead. |

`GET /<id>/run-status` is already a pure read of `run_state.json`/memory and returns `idle` when no run exists [V] — it is the right primitive to build adoption on.
`POST /start` when a run is active and `force=false` returns **HTTP 400** `simRunningForceHint` [V] — an error, not adoption.

## C. State / persistence map

| Item | Where | Class | Notes |
|---|---|---|---|
| Project | `uploads/projects/<project_id>/project.json` + `files/` | FILE | `project_id` joined into paths unvalidated |
| Graph | Zep Cloud graph `mirofish_<hex16>` | CLOUD | `Project.graph_id`, `zep_batch_*` in project.json |
| Task (build/prepare/report) | `TaskManager._tasks` | **MEMORY** | lost on restart; `Project.graph_build_task_id` dangles |
| Simulation config/state | `uploads/simulations/<sim_id>/state.json`, `simulation_config.json`, profiles | FILE | |
| Run state | `run_state.json` **and** `SimulationRunner._run_states` | FILE + MEMORY (dual truth) | file is authoritative on cold start |
| Run process | `SimulationRunner._processes` (`Popen`) | **PROCESS/MEMORY** | pid also in run_state.json, never verified |
| Actions | `<sim>/twitter|reddit/actions.jsonl`, OASIS sqlite `*.db` | FILE | overwritten on restart (force) |
| Agent state | OASIS sqlite + subprocess memory; Zep episodes | FILE/PROCESS/CLOUD | interview via file IPC to live env |
| Report | `uploads/reports/<report_id>/` (ReportManager) | FILE | |
| Logs | `simulation.log`, `logs/*.log` (logger) | FILE | |
| Users / ownership | — | none | |

## D. File-by-file transformation map (priority: P0 first PR · P1 next 3 PRs · P2 later)

| File | Current role | Problem | ScenarioIQ change | Pri |
|---|---|---|---|---|
| `frontend/src/components/Step3Simulation.vue` | Run UI; auto-starts | `onMounted` start, `force:true`, no adoption, no explicit restart | attach-or-offer-run; explicit confirmed restart; terminal states render from server | **P0** |
| `frontend/src/views/SimulationRunView.vue` | Host of Step3 | back button stops run | back never stops; leave = detach | **P0** |
| `frontend/src/api/simulation.js` | axios client | — | `adopted` flag, no default force | P0 |
| `backend/app/api/simulation.py` (`/start`, `/run-status`) | start/stop/status | terminal runs restarted without force; active → 400; `traceback` in 500s | idempotent start (adopt); reconcile dead pid; safe errors | **P0/P1** |
| `backend/app/services/simulation_runner.py` | in-API Popen + monitor | process tied to API lifetime; no pid check; class-level memory | P0: `reconcile_run_state`; P2: move into worker | P0→P1 |
| `backend/app/models/task.py` | in-memory tasks | volatile | replace with SQLAlchemy `Job` | P1 |
| `backend/app/models/project.py` | file-backed project | unvalidated ids, no owner | repository + ids + owner | P1/P2 |
| `backend/app/services/simulation_manager.py` | sim state files, `enable_twitter/reddit` | platform concepts in domain | wrap → `SimulationEngine` | P2 |
| `backend/app/services/graph_builder.py` | Zep build + daemon thread | thread, Zep-coupled, `mirofish_` ids | job + `GraphStore` | P1/P2 |
| `zep_entity_reader.py`, `zep_tools.py`, `zep_graph_memory_updater.py` | Zep reads/writes | vendor lock-in | `ZepGraphStore` adapter behind interface | P2 |
| `oasis_profile_generator.py`, `simulation_config_generator.py` | persona/config via LLM | invents attributes unmarked | Stakeholder model with evidence/assumption tags | P2 |
| `report_agent.py`, `api/report.py` | LLM report | free-text "prediction" report; daemon thread | structured Decision Brief; job | P2 |
| `backend/app/__init__.py` | app factory | `CORS "*"`, no auth, request-body debug logging | restricted CORS, auth abstraction, request ids | P1 |
| `backend/app/config.py` | config | default `SECRET_KEY='mirofish-secret-key'`, Zep required | fail closed in prod, optional Zep | P1 |
| `Dockerfile`, `docker-compose.yml` | deploy | compose pulls **upstream** `ghcr.io/666ghj/mirofish`; container runs `npm run dev` (vite+flask) | build local; api/worker/web split; prod server | P1 |
| `.github/workflows/docker-image.yml` | publishes `ghcr.io/<owner>/mirofish` | old name; no tests/scans | add CI: tests, lint, audit, secret scan, SBOM | P1 |
| `.github/workflows/update-star-history.yml`, `scripts/*star*`, `tests/test_local_star_*` | upstream star chart | irrelevant to ScenarioIQ; gated to `666ghj/MiroFish` | REMOVE later (keep attribution in NOTICE) | P2 |
| `frontend/src/views/Home.vue` | marketing landing (already softened, still "MiroFish") | not a dashboard | dashboard: running / recent / needs attention | P2 |
| `frontend/index.html`, `locales/*.json`, `package.json`, `pyproject.toml` | titles/metadata | "MiroFish – 预测万物" | rebrand | P1 |
| `frontend/vite.config.js` | dev server | hard-coded `allowedHosts` to a personal VPS host; `open:true` | env-driven | P1 |
| `Step2EnvSetup.vue`, `Step4Report.vue`, `Step5Interaction.vue`, `GraphPanel.vue`, `HistoryDatabase.vue` | Steps 2/4/5 | Twitter/Reddit in UI | progressive disclosure | P2 |

## E. Keep / Wrap / Refactor / Replace / Remove

| Component | Verdict | Reason |
|---|---|---|
| `TextProcessor`, `file_parser`, `llm_client`, `retry`, `openai_chat_compat` | KEEP | generic utilities, tested |
| `OntologyGenerator` | WRAP | good for unstructured text only; structured records must bypass the LLM |
| OASIS scripts (`run_*_simulation.py`, `action_logger`) | WRAP | behind `SimulationEngine`; Twitter/Reddit stay internal |
| `simulation_ipc` | KEEP (P2 review) | file IPC works for a worker sharing a volume; revisit for multi-host |
| `SimulationRunner` | REFACTOR | keep finalization/Zep-barrier logic (well tested); relocate process ownership to worker |
| `SimulationManager`, `ProjectManager` | REFACTOR | repositories over DB; keep file trees as blob store |
| `TaskManager` | REPLACE | durable `Job` |
| Zep classes | WRAP → replace default | `GraphStore` + `ZepGraphStore`, then self-hosted store |
| `OasisProfileGenerator` | REFACTOR | entity ≠ stakeholder model; mark assumptions |
| `ReportAgent` (+`ZepTools`) | REFACTOR | keep retrieval tools, change output contract |
| `GraphPanel.vue` | KEEP + add derived views | |
| Step1–5 UI, `Home.vue` | REPLACE incrementally | workflow + dashboard |
| Star-history workflow/scripts/tests | REMOVE (later) | not product |
| Upstream image reference in compose | REPLACE | P1 |

## F. Security findings

| Sev | Finding | Where |
|---|---|---|
| High | No authentication or authorization on any route; no object ownership [V: no auth code found] | all blueprints |
| High | `CORS(..., origins="*")` [V] | `app/__init__.py:37` |
| High | Compose deploys **third-party image** with your `.env` (LLM + Zep keys) mounted [V] | `docker-compose.yml` |
| High | Container runs dev servers (`vite --host` + Flask) exposing 3000/5001 [V] | `Dockerfile`, `package.json` |
| Med | 55 sites return `traceback.format_exc()` to clients [V] | `api/*.py` |
| Med | IDs (`project_id`, `simulation_id`, `report_id`, `graph_id`) joined into paths with no validation; 34 `os.path.join` sites [V] — traversal *possible* [I, not exploited] | `ProjectManager`, `SimulationManager`, `SimulationRunner`, `ReportManager` |
| Med | Default `SECRET_KEY='mirofish-secret-key'`; `debug` env-toggled; `host=0.0.0.0` [V] | `config.py`, `run.py` |
| Med | Request bodies logged at DEBUG (may contain proprietary text) [V] | `app/__init__.py:59` |
| Med | Proprietary document text is sent to Zep Cloud and the LLM provider by default [V] | `graph_builder`, `zep_graph_memory_updater` |
| Med | Vite `allowedHosts` hard-coded to a personal hostname [V] | `vite.config.js` |
| Low | No CI security tooling (no audit, secret scan, SBOM, SAST) [V] | `.github/workflows` |
| Info | Frontend does not embed API keys (only `VITE_API_BASE_URL`) [V] | |

## G. UI/UX findings

- Mental model is "build a graph → simulate a social platform → read a report"; Step names, `[Plaza]`/`[Community]` logs, POST/REPOST/LIKE badges and `twitter_*`/`reddit_*` counters leak implementation into the default view [V].
- Home is a landing/explainer page; the user's own work (running, completed, failed) is not the first thing shown. (`HistoryDatabase.vue` exists but is secondary.)
- Failure states: `FAILED` is handled in the poll loop, but a run that vanishes server-side (zombie/`idle`) leaves the UI on "Running".
- Run page is a single-visit experience; there is no "return to this run" affordance.

## H. Target architecture

```
Vue SPA ──► Flask API (stateless, authn/z, ids, safe errors)
              │  enqueue + persist
              ▼
        SQL (SQLite→Postgres): Project, Scenario, Run, Job, Report, User
              │
              ▼
        Redis + RQ queue ──► scenarioiq-worker(s)
                               ├─ GraphBuild  ─► GraphStore (Zep adapter | self-hosted)
                               ├─ RunSimulation ─► SimulationEngine (OasisSimulationEngine)
                               └─ GenerateBrief ─► ReportAgent + Tool Gateway
        blob volume: evidence files, run artifacts (actions.jsonl, sqlite)
```
RQ vs Celery: this codebase's units of work are long, single-shot Python functions with their own progress reporting; RQ's model (function + Redis) is sufficient, has one dependency, and can be replaced later. Risk: RQ workers `fork` per job (fine on Linux/Docker, not native Windows) and there is no built-in retry policy beyond `Retry`. **[I — will verify against OASIS's asyncio + subprocess model in PR 3 before adding the dependency.]**

## I. Migration plan (each step independently shippable and reversible)

0. **Baseline** (this doc) ✅
1. **Run lifecycle safety** (frontend + `/start`, `/run-status`) — no new deps
2. **Durable Job model** (SQLAlchemy + SQLite; `TaskManager` becomes a facade over it, same API) — new dep: `sqlalchemy`
3. **Worker split** (RQ + Redis; simulation/graph-build/report as jobs; run survives API restart; remove SIGTERM-kills-runs) — new deps: `rq`, `redis`
4. **Deployment** (compose builds locally; api/worker/web/redis; nginx or `vite build` static; dev vs prod)
5. **Security baseline** (identifiers, safe errors, CORS, auth abstraction + owner columns, secrets)
6. **CI** (tests, lint, pip-audit/npm audit, gitleaks, SBOM, CodeQL)
7. **Rebrand** (A/B/C/D/E classification; id compat `mirofish_*` read, `scenarioiq_*` write)
8. **Scenario/Run/Findings domain + dashboard**
9. `GraphStore` → provenance/trust → deterministic dependency analysis → `SimulationEngine` → stakeholder model → Decision Brief → web enrichment → evals.

## J. First PR

**"Make simulation runs browser-independent and safe to revisit"** — confirmed as the right first step; no more-urgent prerequisite found. It is the only change that stops *data destruction* today, needs no new dependency, and defines the adopt-vs-restart invariant that the durable-job work (PR 2–3) inherits.

Invariants:
1. Loading/refreshing/re-entering a run page never starts, stops or restarts a run.
2. `POST /start` without `force` never mutates an existing run (active **or** terminal): it returns it with `adopted: true`.
3. Only an explicit, confirmed user action sends `force: true`.
4. Leaving the run page never stops the run.
5. A run recorded as active whose process is gone is reported `FAILED` (`SIMULATION_WORKER_LOST`), never left `RUNNING`.
6. Terminal runs (completed / failed / stopped) render from server state on return.

Out of scope for PR 1 (documented risk): the run is still a child of the API process, so an API restart still ends it (SIGTERM cleanup) — resolved by PR 3. A surviving orphan after SIGKILL is not re-adopted.
