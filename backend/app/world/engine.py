"""Deterministic simulation slice: proposed action -> validation -> state change -> event log.

Agents (scripted in P0, OASIS/LLM in P1) only *propose* actions. This engine decides
whether an action is possible against world truth, mutates truth when it is, and
records every step as an immutable event with a provenance tag.

Three stores stay separate:
- truth:      seed facts (F0) + applied state changes (S0) + rule-derived facts (F1)
- knowledge:  per-agent view of the world (may be stale or wrong)
- events:     the append-only log
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import rules as rules_mod
from .memory import KnowledgeSink, NullSink
from .models import (
    AgentProfile,
    Entity,
    Event,
    Fact,
    InitialKnowledge,
    KnowledgeKind,
    KnowledgeUpdate,
    Provenance,
    ReviewStatus,
    Rule,
    ScenarioVersion,
    Simulation,
    StateChange,
    VersionStatus,
)

ENGINE_VERSION = "p0-slice-1"
ACTOR_KINDS = {"organisation", "agent"}


class EngineError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Action schema (the contract OASIS / LLM agents will map into in P1)
# ---------------------------------------------------------------------------


class ProposedAction(BaseModel):
    round: int = Field(ge=1)
    actor: str  # entity key
    type: str  # REQUEST_ASSET | RELEASE_ASSET
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: Optional[str] = None  # agent's stated reason (A1)
    model: Optional[str] = None  # model that produced the proposal, if any
    prompt_version: Optional[str] = None


@dataclass
class Outcome:
    valid: bool
    reason: str
    changes: list[tuple[str, str, Any]] = field(default_factory=list)  # (entity, attr, new)
    # (agent, subject, attr, value, kind)
    learned: list[tuple[str, str, str, Any, KnowledgeKind]] = field(default_factory=list)
    provenance: Provenance = Provenance.S0


# ---------------------------------------------------------------------------
# World state
# ---------------------------------------------------------------------------


class WorldState:
    def __init__(
        self,
        entities: dict[str, dict[str, Any]],
        seed_facts: dict[tuple[str, str], Any],
        rule_defs: list[tuple[str, dict[str, Any]]],
        profiles: dict[str, dict[str, Any]],
        knowledge: dict[str, dict[tuple[str, str], tuple[Any, KnowledgeKind]]],
    ):
        self.entities = entities  # key -> {id, kind, name}
        self.base = dict(seed_facts)  # F0 + applied S0 changes
        self.rule_defs = rule_defs
        self.profiles = profiles  # actor key -> {authority: [...], ...}
        self.knowledge = knowledge
        self.truth: dict[tuple[str, str], Any] = {}
        self.derivation = rules_mod.DerivationResult()
        self.rederive()

    @property
    def kinds(self) -> dict[str, str]:
        return {k: v["kind"] for k, v in self.entities.items()}

    def rederive(self) -> None:
        self.truth, self.derivation = rules_mod.derive(self.base, self.kinds, self.rule_defs)

    def get(self, entity: str, attr: str, default: Any = None) -> Any:
        return self.truth.get((entity, attr), default)

    def has(self, entity: str, attr: str) -> bool:
        return (entity, attr) in self.truth

    def belief(self, agent: str, subject: str, attr: str) -> tuple[Any, Optional[KnowledgeKind]]:
        entry = self.knowledge.get(agent, {}).get((subject, attr))
        return (entry[0], entry[1]) if entry else (None, None)


def load_world(session: Session, version_id: str) -> WorldState:
    version = session.get(ScenarioVersion, version_id)
    if version is None:
        raise EngineError(f"Version not found: {version_id}")
    if version.status != VersionStatus.APPROVED:
        raise EngineError("Simulations run only on approved versions")

    approved = ReviewStatus.APPROVED
    ents = session.scalars(
        select(Entity).where(Entity.version_id == version_id, Entity.review_status == approved)
    ).all()
    by_id = {e.id: e for e in ents}
    entities = {e.key: {"id": e.id, "kind": e.kind.value, "name": e.name} for e in ents}

    facts = {}
    for f in session.scalars(
        select(Fact).where(Fact.version_id == version_id, Fact.review_status == approved)
    ):
        if f.entity_id in by_id:
            facts[(by_id[f.entity_id].key, f.attribute)] = f.value

    rule_defs = [
        (r.key, r.definition)
        for r in session.scalars(
            select(Rule).where(Rule.version_id == version_id, Rule.review_status == approved)
        )
    ]

    profiles = {}
    for p in session.scalars(
        select(AgentProfile).where(
            AgentProfile.version_id == version_id, AgentProfile.review_status == approved
        )
    ):
        if p.entity_id in by_id:
            profiles[by_id[p.entity_id].key] = {"authority": list(p.authority or []), "role": p.role}

    knowledge: dict[str, dict[tuple[str, str], tuple[Any, KnowledgeKind]]] = {}
    for k in session.scalars(
        select(InitialKnowledge).where(
            InitialKnowledge.version_id == version_id,
            InitialKnowledge.review_status == approved,
        )
    ):
        if k.agent_entity_id in by_id and k.subject_entity_id in by_id:
            agent = by_id[k.agent_entity_id].key
            subject = by_id[k.subject_entity_id].key
            knowledge.setdefault(agent, {})[(subject, k.attribute)] = (k.value, k.kind)

    return WorldState(entities, facts, rule_defs, profiles, knowledge)


# ---------------------------------------------------------------------------
# Action handlers (validation is pure: it reads state, returns an Outcome)
# ---------------------------------------------------------------------------


def _require_actor(state: WorldState, a: ProposedAction) -> Optional[str]:
    ent = state.entities.get(a.actor)
    if ent is None:
        return f"Unknown actor '{a.actor}'"
    if ent["kind"] not in ACTOR_KINDS:
        return f"'{a.actor}' is a {ent['kind']} and cannot act"
    profile = state.profiles.get(a.actor)
    if profile is None:
        # Default deny: authority must be stated and approved, never assumed.
        return f"{ent['name']} has no approved agent profile, so no authority to {a.type}"
    if a.type not in profile["authority"]:
        return f"{ent['name']} has no authority to {a.type}"
    return None


def _require_asset(state: WorldState, key: Optional[str]) -> Optional[str]:
    if not key:
        return "Missing parameter 'asset'"
    ent = state.entities.get(key)
    if ent is None:
        return f"Unknown asset '{key}'"
    if ent["kind"] not in {"asset", "vehicle", "facility"}:
        return f"'{key}' is a {ent['kind']}, not an allocatable asset"
    return None


def _request_asset(state: WorldState, a: ProposedAction) -> Outcome:
    asset = a.params.get("asset")
    purpose = a.params.get("purpose", "general")
    for err in (_require_actor(state, a), _require_asset(state, asset)):
        if err:
            return Outcome(False, err)
    name = state.entities[asset]["name"]

    if purpose == "medical":
        if not state.has(asset, "medical_transport_eligible"):
            return Outcome(
                False,
                f"Medical eligibility of {name} cannot be determined from the world model",
                provenance=Provenance.U0,
            )
        if state.get(asset, "medical_transport_eligible") is not True:
            reasons = [
                d for d in state.derivation.derivations
                if d.entity == asset and d.attribute == "medical_transport_eligible"
            ]
            why = (f" (rule {reasons[0].rule_key}: {json.dumps(reasons[0].inputs, sort_keys=True)})"
                   if reasons else "")
            return Outcome(
                False,
                f"{name} is not eligible for medical transport{why}",
                learned=[(a.actor, asset, "medical_transport_eligible", False, KnowledgeKind.KNOWS)],
            )

    holder = state.get(asset, "committed_to")
    if holder not in (None, a.actor):
        holder_name = state.entities.get(holder, {}).get("name", holder)
        return Outcome(
            False,
            f"{name} is committed to {holder_name}",
            learned=[(a.actor, asset, "committed_to", holder, KnowledgeKind.KNOWS)],
        )
    assignee = state.get(asset, "assigned_to")
    if assignee not in (None, a.actor):
        return Outcome(
            False,
            f"{name} is already assigned to {state.entities.get(assignee, {}).get('name', assignee)}",
            learned=[(a.actor, asset, "assigned_to", assignee, KnowledgeKind.KNOWS)],
        )

    return Outcome(
        True,
        f"{name} assigned to {state.entities[a.actor]['name']} for {purpose} use",
        changes=[(asset, "committed_to", a.actor), (asset, "assigned_to", a.actor)],
        learned=[
            (a.actor, asset, "committed_to", a.actor, KnowledgeKind.KNOWS),
            (a.actor, asset, "assigned_to", a.actor, KnowledgeKind.KNOWS),
        ],
    )


def _release_asset(state: WorldState, a: ProposedAction) -> Outcome:
    asset = a.params.get("asset")
    for err in (_require_actor(state, a), _require_asset(state, asset)):
        if err:
            return Outcome(False, err)
    name = state.entities[asset]["name"]
    if state.get(asset, "committed_to") != a.actor:
        return Outcome(False, f"{state.entities[a.actor]['name']} does not hold a commitment on {name}")
    changes = [(asset, "committed_to", None)]
    if state.get(asset, "assigned_to") == a.actor:
        changes.append((asset, "assigned_to", None))
    return Outcome(
        True,
        f"{state.entities[a.actor]['name']} released {name}",
        changes=changes,
        learned=[(a.actor, asset, attr, None, KnowledgeKind.KNOWS) for _, attr, _ in changes],
    )


HANDLERS: dict[str, Callable[[WorldState, ProposedAction], Outcome]] = {
    "REQUEST_ASSET": _request_asset,
    "RELEASE_ASSET": _release_asset,
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class _Log:
    def __init__(self, session: Session, sim: Simulation, state: WorldState):
        self.session = session
        self.sim = sim
        self.state = state
        self.seq = 0

    def add(self, round_: int, kind: str, provenance: Provenance, **kw: Any) -> Event:
        self.seq += 1
        actor = kw.pop("actor", None)
        ev = Event(
            simulation_id=self.sim.id,
            seq=self.seq,
            round=round_,
            kind=kind,
            provenance=provenance,
            actor_entity_id=self.state.entities[actor]["id"] if actor in self.state.entities else None,
            **kw,
        )
        self.session.add(ev)
        self.session.flush()
        return ev


def _log_derivations(log: _Log, round_: int, before: dict, caused_by: Optional[str]) -> None:
    for d in log.state.derivation.derivations:
        if before.get((d.entity, d.attribute), object()) != d.value:
            log.add(
                round_, "derivation", Provenance.F1,
                payload={"rule": d.rule_key, "entity": d.entity, "attribute": d.attribute,
                         "value": d.value, "inputs": d.inputs},
                caused_by_event_id=caused_by,
            )
    if round_ == 0:
        for s in log.state.derivation.skips:
            log.add(0, "derivation_skipped", Provenance.U0,
                    payload={"rule": s.rule_key, "binding": s.binding, "missing": s.missing,
                             "error": s.error},
                    reason=s.reason)


def run_simulation(
    session: Session,
    version_id: str,
    actions: list[ProposedAction | dict],
    *,
    name: str = "Scripted run",
    seed: int = 0,
    sink: Optional[KnowledgeSink] = None,
) -> Simulation:
    sink = sink or NullSink()
    state = load_world(session, version_id)
    parsed = [a if isinstance(a, ProposedAction) else ProposedAction(**a) for a in actions]
    parsed.sort(key=lambda a: a.round)  # stable: keeps script order within a round

    sim = Simulation(
        version_id=version_id,
        name=name,
        seed=seed,
        status="running",
        run_config={
            "engine_version": ENGINE_VERSION,
            "rules_hash": rules_mod.rules_hash(state.rule_defs),
            "seed": seed,
            "script": [a.model_dump() for a in parsed],
        },
    )
    session.add(sim)
    session.flush()
    log = _Log(session, sim, state)

    # Round 0: record F1 facts derived from the seed world.
    _log_derivations(log, 0, {}, None)

    for a in parsed:
        # What the actor believed about the target when deciding (A1).
        target = a.params.get("asset")
        if target:
            beliefs = {}
            for attr in ("committed_to", "assigned_to", "medical_transport_eligible"):
                value, kind = state.belief(a.actor, target, attr)
                if kind is not None:
                    beliefs[attr] = {"value": value, "kind": kind.value,
                                     "matches_truth": value == state.get(target, attr)}
            log.add(a.round, "observation", Provenance.A1, actor=a.actor,
                    payload={"subject": target, "beliefs": beliefs, "rationale": a.rationale})

        act_ev = log.add(a.round, "action", Provenance.A0, actor=a.actor, action_type=a.type,
                         payload={"params": a.params, "rationale": a.rationale},
                         model=a.model, prompt_version=a.prompt_version)

        handler = HANDLERS.get(a.type)
        outcome = handler(state, a) if handler else Outcome(False, f"Unsupported action type {a.type}")
        val_ev = log.add(a.round, "validation", outcome.provenance, actor=a.actor,
                         action_type=a.type, valid=outcome.valid, reason=outcome.reason,
                         caused_by_event_id=act_ev.id)

        if outcome.valid and outcome.changes:
            before = dict(state.truth)
            for entity, attr, new in outcome.changes:
                old = state.base.get((entity, attr))
                state.base[(entity, attr)] = new
                sc_ev = log.add(a.round, "state_change", Provenance.S0, actor=a.actor,
                                payload={"entity": entity, "attribute": attr, "old": old, "new": new},
                                caused_by_event_id=val_ev.id)
                session.add(StateChange(simulation_id=sim.id, event_id=sc_ev.id, round=a.round,
                                        entity_id=state.entities[entity]["id"], attribute=attr,
                                        old_value=old, new_value=new))
            state.rederive()
            _log_derivations(log, a.round, before, val_ev.id)

        for agent, subject, attr, value, kind in outcome.learned:
            prev, prev_kind = state.belief(agent, subject, attr)
            state.knowledge.setdefault(agent, {})[(subject, attr)] = (value, kind)
            if prev_kind is not None and prev == value:
                continue
            ku_ev = log.add(a.round, "knowledge_update", Provenance.S0, actor=agent,
                            payload={"subject": subject, "attribute": attr,
                                     "old_belief": prev, "new": value, "kind": kind.value},
                            caused_by_event_id=val_ev.id)
            session.add(KnowledgeUpdate(simulation_id=sim.id, event_id=ku_ev.id, round=a.round,
                                        agent_entity_id=state.entities[agent]["id"],
                                        subject_entity_id=state.entities[subject]["id"],
                                        attribute=attr, value=value, kind=kind))
            sink.publish(sim.id, agent, subject, attr, value, kind.value, a.round)

    sim.status = "completed"
    sim.run_config = {**copy.deepcopy(sim.run_config), "final_truth": _truth_snapshot(state)}
    session.flush()
    return sim


def _truth_snapshot(state: WorldState) -> dict[str, Any]:
    return {f"{e}.{a}": v for (e, a), v in sorted(state.truth.items())}


def replay(session: Session, simulation_id: str, *, sink: Optional[KnowledgeSink] = None) -> Simulation:
    """Re-run a recorded script on the same approved version. Deterministic by design."""
    orig = session.get(Simulation, simulation_id)
    if orig is None:
        raise EngineError(f"Simulation not found: {simulation_id}")
    return run_simulation(
        session, orig.version_id, orig.run_config.get("script", []),
        name=f"Replay of {orig.name}", seed=orig.seed, sink=sink,
    )
