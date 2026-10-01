# ScenarioIQ v2 — P0: World model, provenance, event log

Status: implemented on branch `p0-world-model`. The legacy pipeline (ontology → Zep graph → OASIS → report) is unchanged and runs beside it.

## Decisions

| Area | Decision |
|---|---|
| Runtime | Wrap OASIS (P1). P0 uses scripted proposals through the same action schema. |
| World truth | SQLite via SQLAlchemy 2 + Alembic. Set `WORLD_DB_URL` to move to PostgreSQL. |
| Zep | Agent knowledge and beliefs only. P0 ships the `KnowledgeSink` interface and a no-op sink. |
| World source | The LLM drafts from a redacted briefing. A human approves each row. Only approved rows are F0. |
| Rules | Declarative JSON (`backend/app/world/rules.py`). No code runs from rule data. |
| Sensitive text | Redacted before any model call. The placeholder map stays in the local database. |

## Data model

Separate tables keep three things apart:

- **Truth:** `facts` (initial state, one attribute per row), plus `state_changes` during a run.
- **Knowledge:** `initial_knowledge` (what each actor knows or believes at the start), plus `knowledge_updates` during a run.
- **Events:** `events`, append-only. Each event has a kind, actor, round, provenance, a causal link (`caused_by_event_id`), and model and prompt version fields for P1.

A `scenario_versions` row is a world snapshot. An approved version is frozen. To change it, use `new-version`, which copies it into a new draft with `parent_id` set. Branching in P2 builds on this.

Mutable state lives in facts, for example `rt-05.committed_to = "greenmart"`. Relationships describe the initial graph for display and, later, dependency analysis.

## Provenance

| Tag | Meaning | Where it comes from |
|---|---|---|
| F0 | Seed fact | A row a human approved |
| F1 | Derived fact | Rules engine output, logged with the rule and its inputs |
| A0 | Agent action | A proposed action |
| A1 | Agent belief | Initial knowledge; the observation logged before each action |
| S0 | Simulation outcome | Validation result, state change, knowledge update |
| U0 | Unverified | A draft row, a skipped rule, an undeterminable check |

## Rules

```json
{
  "for_each": {"kind": "vehicle"},
  "when": {"all": [
    {"op": "==", "left": {"fact": ["$self", "refrigerated"]}, "right": true},
    {"op": "==", "left": {"fact": [{"fact": ["$self", "operator"]}, "gdp_certified"]}, "right": true}
  ]},
  "then": [{"set": ["$self", "medical_transport_eligible"], "value": true}],
  "else": [{"set": ["$self", "medical_transport_eligible"], "value": false}]
}
```

- Rules run to a fixpoint (at most 10 passes), in key order.
- A rule cannot overwrite a seed fact.
- A rule that reads a missing fact, or fails on the data (a null comparison, a wrong type, division by zero), is skipped. The skip is logged as U0. The engine does not guess.
- Version approval runs the approved rules over the approved facts once. A conflict blocks approval and does not fail later, mid-run.

## Action contract (the P1 OASIS mapping target)

```json
{"round": 1, "actor": "mla", "type": "REQUEST_ASSET",
 "params": {"asset": "rt-05", "purpose": "medical"},
 "rationale": "RT-05 appears available", "model": null, "prompt_version": null}
```

P0 action types:

- Authority is default-deny. An actor with no approved agent profile cannot act.
- `REQUEST_ASSET`: checks the actor's authority, that the asset exists, medical eligibility (when `purpose` is `medical`), and that no other party holds a commitment or assignment.
- `RELEASE_ASSET`: the actor must hold the commitment.

A rejected action teaches the actor the fact that blocked it. World truth does not change.

## Reproducibility

Each simulation records `engine_version`, `rules_hash`, `seed`, the full action script and the final truth. `POST /api/world/simulations/<id>/replay` re-runs the script. Tests assert that the replay event trace is identical.

## API (`/api/world`)

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/scenarios` | List scenarios; create a scenario (with draft v1) |
| GET | `/versions/<id>` | Full world payload and simulation list |
| POST | `/redact-preview` | Show the redacted text. Stores nothing; calls no model |
| POST | `/versions/<id>/documents` | Save a redacted briefing (multipart file or JSON text) |
| POST | `/versions/<id>/extract` | LLM draft, entered as proposed rows |
| POST | `/versions/<id>/import` | Manual World JSON import, entered as proposed rows |
| PATCH | `/rows/<table>/<id>` | `approve`, `reject` or `edit` a row |
| POST | `/versions/<id>/approve` | Freeze the version (no proposed rows, no dangling references) |
| POST | `/versions/<id>/new-version` | Copy into a new draft |
| POST | `/versions/<id>/simulations` | Run a scripted slice |
| GET/POST | `/simulations/<id>`, `/simulations/<id>/replay` | Read the event log; replay |
| POST | `/fixtures/rt05` | Load the synthetic RT-05 case (`{"review": true}` to review it yourself) |

UI: `/world` (linked from the home page).

## Configuration

- `WORLD_DB_URL`: default `sqlite:///<repo>/data/scenarioiq.db`. In Docker, `./data` is a volume.
- `WORLD_EXTRACTION_MODEL`: model for drafting. Default `LLM_MODEL_NAME`. This is the first tier-1 hook for the P1 router.
- Migrations run at app start. To run them by hand: `cd backend && uv run alembic upgrade head`.

## Known limits

- Redaction catches only the terms you list, plus emails, Singapore phone numbers and NRIC/FIN numbers (any case). An 8-digit number followed by a unit (pallets, kg, %, and so on) is treated as a quantity, not a phone number. Check the preview before extraction.
- Re-running an import or extraction on the same version skips rows that already exist. It does not update them.
- Actors are not notified of other actors' actions. In the RT-05 baseline, the Authority still believes Greenmart holds RT-05 when it succeeds in round 3. The log shows this. P1 needs a `NOTIFY`/channel mechanism.
- Extraction is a single call capped at 60,000 characters. It does not chunk long briefings yet.
- `quote_verified` confirms that the quote exists in the source. It does not confirm that the quote supports the value.
