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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import rules as rules_mod
from .memory import KnowledgeSink, NullSink
from .models import (
    AgentProfile,
    Entity,
    Event,
    Fact,
    InitialKnowledge,
    Injection,
    InjectionItem,
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
        select(Entity).where(Entity.version_id == version_id, Entity.review_status == approved,
                             Entity.injection_id.is_(None))
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


# ---------------------------------------------------------------------------
# Injections and run continuation
# ---------------------------------------------------------------------------


@dataclass
class InjectionParts:
    injection: Injection
    entities: dict[str, dict[str, Any]]
    rules: list[tuple[str, dict[str, Any]]]
    profiles: dict[str, dict[str, Any]]
    facts: list[dict[str, Any]]


def _injection_parts(session: Session, injection_id: str, version_id: str) -> InjectionParts:
    inj = session.get(Injection, injection_id)
    if inj is None:
        raise EngineError(f"Injection not found: {injection_id}")
    if inj.version_id != version_id:
        raise EngineError("Injection belongs to a different scenario version")
    if inj.status != "ready":
        raise EngineError("Finalize the injection before applying it")
    items = session.scalars(select(InjectionItem).where(
        InjectionItem.injection_id == inj.id, InjectionItem.review_status == ReviewStatus.APPROVED,
    ).order_by(InjectionItem.seq)).all()
    entities: dict[str, dict[str, Any]] = {}
    for i in items:
        if i.op == "add_entity":
            row = session.scalar(select(Entity).where(Entity.version_id == version_id,
                                                      Entity.key == i.payload["key"]))
            entities[row.key] = {"id": row.id, "kind": row.kind.value, "name": row.name}
    return InjectionParts(
        injection=inj,
        entities=entities,
        rules=[(i.payload["key"], i.payload["definition"]) for i in items if i.op == "add_rule"],
        profiles={i.payload["entity"]: {"authority": list(i.payload.get("authority") or []),
                                        "role": i.payload.get("role") or ""}
                  for i in items if i.op == "set_profile"},
        facts=[i.payload for i in items if i.op == "set_fact"],
    )


def _apply_structure(state: WorldState, parts: InjectionParts) -> None:
    """Entities, rules and profiles. Fact values come from recorded state changes."""
    for key, ent in parts.entities.items():
        state.entities.setdefault(key, ent)
    merged = dict(state.rule_defs)
    merged.update(dict(parts.rules))  # same key replaces the seed rule in this branch
    state.rule_defs = list(merged.items())
    state.profiles.update(parts.profiles)


def last_round(session: Session, simulation_id: str) -> int:
    return session.scalar(select(func.max(Event.round)).where(Event.simulation_id == simulation_id)) or 0


def state_at(session: Session, sim: Simulation, round_: int) -> WorldState:
    """Rebuild a run's world (truth and knowledge) after `round_`, from its recorded log.

    Nothing is re-simulated: the state is the seed (or the parent's state at the fork)
    plus every recorded state change and knowledge update up to that round.
    """
    if sim.parent_id:
        parent = session.get(Simulation, sim.parent_id)
        state = state_at(session, parent, sim.fork_round or 0)
    else:
        state = load_world(session, sim.version_id)
    if sim.injection_id and round_ > (sim.fork_round or 0):
        _apply_structure(state, _injection_parts(session, sim.injection_id, sim.version_id))

    keys = {v["id"]: k for k, v in state.entities.items()}
    changes = session.execute(
        select(StateChange).join(Event, StateChange.event_id == Event.id)
        .where(StateChange.simulation_id == sim.id, StateChange.round <= round_).order_by(Event.seq)
    ).scalars()
    for sc in changes:
        state.base[(keys[sc.entity_id], sc.attribute)] = sc.new_value
    updates = session.execute(
        select(KnowledgeUpdate).join(Event, KnowledgeUpdate.event_id == Event.id)
        .where(KnowledgeUpdate.simulation_id == sim.id, KnowledgeUpdate.round <= round_).order_by(Event.seq)
    ).scalars()
    for ku in updates:
        state.knowledge.setdefault(keys[ku.agent_entity_id], {})[
            (keys[ku.subject_entity_id], ku.attribute)] = (ku.value, ku.kind)
    state.rederive()
    return state


def _record_knowledge(log: "_Log", sink: KnowledgeSink, round_: int, agent: str, subject: str,
                      attr: str, value: Any, kind: KnowledgeKind, provenance: Provenance,
                      caused_by: Optional[str]) -> None:
    state = log.state
    prev, prev_kind = state.belief(agent, subject, attr)
    state.knowledge.setdefault(agent, {})[(subject, attr)] = (value, kind)
    if prev_kind is not None and prev == value:
        return
    ev = log.add(round_, "knowledge_update", provenance, actor=agent,
                 payload={"subject": subject, "attribute": attr, "old_belief": prev,
                          "new": value, "kind": kind.value},
                 caused_by_event_id=caused_by)
    log.session.add(KnowledgeUpdate(simulation_id=log.sim.id, event_id=ev.id, round=round_,
                                    agent_entity_id=state.entities[agent]["id"],
                                    subject_entity_id=state.entities[subject]["id"],
                                    attribute=attr, value=value, kind=kind))
    sink.publish(log.sim.id, agent, subject, attr, value, kind.value, round_)


def _record_change(log: "_Log", round_: int, entity: str, attr: str, new: Any,
                   provenance: Provenance, caused_by: Optional[str], actor: Optional[str] = None) -> None:
    state = log.state
    old = state.base.get((entity, attr))
    state.base[(entity, attr)] = new
    ev = log.add(round_, "state_change", provenance, actor=actor,
                 payload={"entity": entity, "attribute": attr, "old": old, "new": new},
                 caused_by_event_id=caused_by)
    log.session.add(StateChange(simulation_id=log.sim.id, event_id=ev.id, round=round_,
                                entity_id=state.entities[entity]["id"], attribute=attr,
                                old_value=old, new_value=new))


def _apply_injection(log: "_Log", sink: KnowledgeSink, parts: InjectionParts, round_: int) -> None:
    state = log.state
    inj = parts.injection
    head = log.add(round_, "injection", Provenance.X0,
                   payload={"injection": inj.id, "label": inj.label,
                            "new_entities": sorted(parts.entities), "fact_changes": len(parts.facts),
                            "rules": [k for k, _ in parts.rules], "profiles": sorted(parts.profiles)},
                   reason=f"Injected: {inj.label}")
    before = dict(state.truth)
    old_rules = dict(state.rule_defs)
    for key in sorted(parts.entities):
        if key not in state.entities:
            log.add(round_, "entity_added", Provenance.X0,
                    payload={"entity": key, **parts.entities[key]}, caused_by_event_id=head.id)
    for key, _ in parts.rules:
        log.add(round_, "rule_set", Provenance.X0,
                payload={"rule": key, "replaces_seed_rule": key in old_rules}, caused_by_event_id=head.id)
    for key, prof in sorted(parts.profiles.items()):
        log.add(round_, "profile_set", Provenance.X0,
                payload={"entity": key, "authority": prof["authority"]}, caused_by_event_id=head.id)
    _apply_structure(state, parts)
    for f in parts.facts:
        _record_change(log, round_, f["entity"], f["attribute"], f.get("value"), Provenance.X0, head.id)
    state.rederive()
    _log_derivations(log, round_, before, head.id)
    # Only the actors the briefing names learn the news now. Everyone else keeps their view.
    for f in parts.facts:
        for agent in f.get("informed") or []:
            _record_knowledge(log, sink, round_, agent, f["entity"], f["attribute"], f.get("value"),
                              KnowledgeKind.KNOWS, Provenance.X0, head.id)


def run_simulation(
    session: Session,
    version_id: Optional[str],
    actions: list[ProposedAction | dict],
    *,
    name: str = "Scripted run",
    seed: int = 0,
    sink: Optional[KnowledgeSink] = None,
    parent_id: Optional[str] = None,
    fork_round: Optional[int] = None,
    injection_id: Optional[str] = None,
) -> Simulation:
    """Run a script from the seed world, or continue a parent run from `fork_round`.

    A continuation never changes its parent. Several continuations of one parent are
    branches: for example, one with an escalation injection and one with de-escalation.
    """
    sink = sink or NullSink()
    parent: Optional[Simulation] = None
    if parent_id:
        parent = session.get(Simulation, parent_id)
        if parent is None:
            raise EngineError(f"Parent simulation not found: {parent_id}")
        if parent.status != "completed":
            raise EngineError("The parent run is not completed")
        version_id = parent.version_id
        parent_last = last_round(session, parent.id)
        fork_round = parent_last if fork_round is None else int(fork_round)
        if not 0 <= fork_round <= parent_last:
            raise EngineError(f"fork_round must be between 0 and {parent_last}")
        state = state_at(session, parent, fork_round)
    else:
        if injection_id:
            raise EngineError("An injection applies to a running world: continue a run to inject")
        if version_id is None:
            raise EngineError("version_id is required")
        fork_round = None
        state = load_world(session, version_id)

    parts = _injection_parts(session, injection_id, version_id) if injection_id else None
    parsed = [a if isinstance(a, ProposedAction) else ProposedAction(**a) for a in actions]
    parsed.sort(key=lambda a: a.round)  # stable: keeps script order within a round
    start = (fork_round or 0) + 1
    early = [a.round for a in parsed if a.round < start]
    if early:
        raise EngineError(f"Actions must start at round {start} or later (got round {min(early)})")

    sim = Simulation(version_id=version_id, name=name, seed=seed, status="running",
                     parent_id=parent.id if parent else None, fork_round=fork_round,
                     injection_id=injection_id)
    session.add(sim)
    session.flush()
    log = _Log(session, sim, state)

    if parent:
        log.add(fork_round, "fork", Provenance.S0,
                payload={"parent": parent.id, "parent_name": parent.name, "fork_round": fork_round},
                reason=f"Continues '{parent.name}' after round {fork_round}")
    else:
        _log_derivations(log, 0, {}, None)  # F1 facts derived from the seed world
    if parts:
        _apply_injection(log, sink, parts, start)

    sim.run_config = {
        "engine_version": ENGINE_VERSION,
        "rules_hash": rules_mod.rules_hash(state.rule_defs),
        "seed": seed,
        "parent_id": sim.parent_id,
        "fork_round": fork_round,
        "injection_id": injection_id,
        "script": [a.model_dump() for a in parsed],
    }

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
                _record_change(log, a.round, entity, attr, new, Provenance.S0, val_ev.id, actor=a.actor)
            state.rederive()
            _log_derivations(log, a.round, before, val_ev.id)

        for agent, subject, attr, value, kind in outcome.learned:
            _record_knowledge(log, sink, a.round, agent, subject, attr, value, kind, Provenance.S0, val_ev.id)

    sim.status = "completed"
    sim.run_config = {**copy.deepcopy(sim.run_config), "final_truth": _truth_snapshot(state)}
    session.flush()
    return sim


def _truth_snapshot(state: WorldState) -> dict[str, Any]:
    return {f"{e}.{a}": v for (e, a), v in sorted(state.truth.items())}


def replay(session: Session, simulation_id: str, *, sink: Optional[KnowledgeSink] = None) -> Simulation:
    """Re-run a recorded script (and, for a continuation, its fork and injection). Deterministic."""
    orig = session.get(Simulation, simulation_id)
    if orig is None:
        raise EngineError(f"Simulation not found: {simulation_id}")
    return run_simulation(
        session, orig.version_id, orig.run_config.get("script", []),
        name=f"Replay of {orig.name}", seed=orig.seed, sink=sink,
        parent_id=orig.parent_id, fork_round=orig.fork_round, injection_id=orig.injection_id,
    )
