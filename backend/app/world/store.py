"""World model persistence: scenarios, versions, import, review and approval.

World JSON (the single interchange format for fixtures, manual import and LLM drafts):

    {
      "entities":      [{"key", "kind", "name", "description?", "source_quote?"}],
      "facts":         [{"entity", "attribute", "value", "unit?", "source_quote?"}],
      "relationships": [{"source", "target", "type", "attributes?", "source_quote?"}],
      "rules":         [{"key", "description", "definition", "source_quote?"}],
      "profiles":      [{"entity", "role", "objectives?", "incentives?", "authority?",
                         "constraints?", "risk_tolerance?", "decision_style?", "channels?"}],
      "knowledge":     [{"agent", "subject", "attribute", "value", "kind?": "knows|believes"}]
    }

Relationships describe the initial graph. Mutable state (for example which
organisation an asset is committed to) lives in facts, so the engine can change it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Type

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import rules as rules_mod
from .redaction import unredact_text
from .models import (
    AgentProfile,
    Base,
    Entity,
    EntityKind,
    Event,
    Fact,
    InitialKnowledge,
    KnowledgeKind,
    Origin,
    Provenance,
    Relationship,
    ReviewStatus,
    Rule,
    Scenario,
    ScenarioVersion,
    Simulation,
    SourceDocument,
    VersionStatus,
)


class StoreError(ValueError):
    pass


REVIEWABLE: dict[str, Type[Base]] = {
    "entities": Entity,
    "facts": Fact,
    "relationships": Relationship,
    "rules": Rule,
    "profiles": AgentProfile,
    "knowledge": InitialKnowledge,
}

# Fields a reviewer may edit per table (identity/foreign keys are not editable).
EDITABLE: dict[str, set[str]] = {
    "entities": {"name", "description", "kind"},
    "facts": {"value", "unit", "attribute"},
    "relationships": {"type", "attributes"},
    "rules": {"description", "definition"},
    "profiles": {"role", "objectives", "incentives", "authority", "constraints",
                 "risk_tolerance", "decision_style", "channels"},
    "knowledge": {"value", "kind", "attribute"},
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_found(quote: Optional[str], source: Optional[str]) -> Optional[bool]:
    if not quote or source is None:
        return None
    return _norm(quote) in _norm(source)


# ---------------------------------------------------------------------------
# Scenarios and versions
# ---------------------------------------------------------------------------


def create_scenario(session: Session, name: str, description: str | None = None) -> ScenarioVersion:
    scenario = Scenario(name=name, description=description)
    session.add(scenario)
    session.flush()
    version = ScenarioVersion(scenario_id=scenario.id, number=1, status=VersionStatus.DRAFT)
    session.add(version)
    session.flush()
    return version


def get_version(session: Session, version_id: str) -> ScenarioVersion:
    v = session.get(ScenarioVersion, version_id)
    if v is None:
        raise StoreError(f"Version not found: {version_id}")
    return v


def _require_draft(version: ScenarioVersion) -> None:
    if version.status != VersionStatus.DRAFT:
        raise StoreError("Approved versions are frozen. Create a new version to edit.")


def add_document(session: Session, version_id: str, filename: str, sha256: str,
                 redacted_text: str, redaction_map: dict[str, str]) -> SourceDocument:
    _require_draft(get_version(session, version_id))
    doc = SourceDocument(version_id=version_id, filename=filename, sha256=sha256,
                         redacted_text=redacted_text, redaction_map=redaction_map)
    session.add(doc)
    session.flush()
    return doc


def import_world(
    session: Session,
    version_id: str,
    spec: dict[str, Any],
    *,
    origin: Origin,
    approve: bool = False,
    document: Optional[SourceDocument] = None,
) -> dict[str, int]:
    """Insert a world spec into a draft version. Returns counts per table.

    approve=True is only for hand-authored fixtures; LLM drafts always enter as PROPOSED.
    Quotes are checked against the redacted source text when a document is given.
    """
    version = get_version(session, version_id)
    _require_draft(version)
    if origin == Origin.LLM and approve:
        raise StoreError("LLM drafts cannot be auto-approved")

    status = ReviewStatus.APPROVED if approve else ReviewStatus.PROPOSED
    prov = Provenance.F0 if approve else Provenance.U0
    # Quotes are checked against the original text, rebuilt locally from the redaction map.
    source_text = unredact_text(document.redacted_text, document.redaction_map) if document else None

    def common(item: dict[str, Any]) -> dict[str, Any]:
        q = item.get("source_quote")
        return dict(review_status=status, provenance=prov, origin=origin,
                    source_document_id=document.id if document else None,
                    source_quote=q, quote_verified=quote_found(q, source_text))

    existing = {e.key: e for e in session.scalars(select(Entity).where(Entity.version_id == version_id))}
    counts: dict[str, int] = {k: 0 for k in REVIEWABLE}
    counts["skipped_duplicates"] = 0

    # Natural keys already in this version. Re-running an import or extraction must
    # not violate unique constraints; existing rows (and their review state) win.
    def seen(model, *cols):
        return {tuple(row) for row in session.execute(
            select(*[getattr(model, c) for c in cols]).where(model.version_id == version_id))}

    have_facts = seen(Fact, "entity_id", "attribute")
    have_rels = seen(Relationship, "source_id", "target_id", "type")
    have_rules = {k for (k,) in seen(Rule, "key")}
    have_profiles = {k for (k,) in seen(AgentProfile, "entity_id")}
    have_knowledge = seen(InitialKnowledge, "agent_entity_id", "subject_entity_id", "attribute")

    def dup(natural_key, bucket) -> bool:
        if natural_key in bucket:
            counts["skipped_duplicates"] += 1
            return True
        bucket.add(natural_key)
        return False

    for item in spec.get("entities", []):
        key = _slug(item["key"])
        if key in existing:
            counts["skipped_duplicates"] += 1
            continue
        try:
            kind = EntityKind(item["kind"])
        except ValueError as exc:
            raise StoreError(f"Unknown entity kind '{item['kind']}' for {key}") from exc
        ent = Entity(version_id=version_id, key=key, kind=kind, name=item.get("name") or key,
                     description=item.get("description"), **common(item))
        session.add(ent)
        existing[key] = ent
        counts["entities"] += 1
    session.flush()

    def ent_id(key: str, where: str) -> str:
        k = _slug(key)
        if k not in existing:
            raise StoreError(f"{where} references unknown entity '{key}'")
        return existing[k].id

    for item in spec.get("facts", []):
        eid = ent_id(item["entity"], "fact")
        if dup((eid, item["attribute"]), have_facts):
            continue
        session.add(Fact(version_id=version_id, entity_id=eid,
                         attribute=item["attribute"], value=item.get("value"),
                         unit=item.get("unit"), **common(item)))
        counts["facts"] += 1

    for item in spec.get("relationships", []):
        src, tgt = ent_id(item["source"], "relationship"), ent_id(item["target"], "relationship")
        if dup((src, tgt, item["type"]), have_rels):
            continue
        session.add(Relationship(version_id=version_id, source_id=src, target_id=tgt,
                                 type=item["type"], attributes=item.get("attributes") or {},
                                 **common(item)))
        counts["relationships"] += 1

    for item in spec.get("rules", []):
        rules_mod.validate_rule(item["definition"])
        rkey = _slug(item["key"])
        if dup(rkey, have_rules):
            continue
        session.add(Rule(version_id=version_id, key=rkey,
                         description=item.get("description", ""),
                         definition=item["definition"], **common(item)))
        counts["rules"] += 1

    for item in spec.get("profiles", []):
        eid = ent_id(item["entity"], "profile")
        if dup(eid, have_profiles):
            continue
        session.add(AgentProfile(
            version_id=version_id, entity_id=eid,
            role=item.get("role", ""), objectives=item.get("objectives", []),
            incentives=item.get("incentives", []), authority=item.get("authority", []),
            constraints=item.get("constraints", []), risk_tolerance=item.get("risk_tolerance"),
            decision_style=item.get("decision_style"), channels=item.get("channels", []),
            **common(item)))
        counts["profiles"] += 1

    for item in spec.get("knowledge", []):
        aid, sid = ent_id(item["agent"], "knowledge"), ent_id(item["subject"], "knowledge")
        if dup((aid, sid, item["attribute"]), have_knowledge):
            continue
        session.add(InitialKnowledge(
            version_id=version_id, agent_entity_id=aid, subject_entity_id=sid,
            attribute=item["attribute"], value=item.get("value"),
            kind=KnowledgeKind(item.get("kind", "believes")),
            **{**common(item), "provenance": Provenance.A1 if approve else Provenance.U0}))
        counts["knowledge"] += 1

    session.flush()
    return counts


def _slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(key).lower()).strip("-")


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


def _row(session: Session, table: str, row_id: str) -> Any:
    model = REVIEWABLE.get(table)
    if model is None:
        raise StoreError(f"Unknown table '{table}'")
    row = session.get(model, row_id)
    if row is None:
        raise StoreError(f"{table} row not found: {row_id}")
    _require_draft(get_version(session, row.version_id))
    return row


def review_row(session: Session, table: str, row_id: str, decision: str,
               edits: Optional[dict[str, Any]] = None) -> Any:
    """Approve, reject or edit a row. Editing implies human authorship."""
    row = _row(session, table, row_id)
    if edits:
        bad = set(edits) - EDITABLE[table]
        if bad:
            raise StoreError(f"Fields not editable on {table}: {sorted(bad)}")
        if table == "rules" and "definition" in edits:
            rules_mod.validate_rule(edits["definition"])
        if table == "entities" and "kind" in edits:
            edits = {**edits, "kind": EntityKind(edits["kind"])}
        if table == "knowledge" and "kind" in edits:
            edits = {**edits, "kind": KnowledgeKind(edits["kind"])}
        _check_unique_after_edit(session, table, row, edits)
        for k, v in edits.items():
            setattr(row, k, v)
        row.origin = Origin.HUMAN
    if decision == "approve":
        row.review_status = ReviewStatus.APPROVED
        row.provenance = Provenance.A1 if table == "knowledge" else Provenance.F0
    elif decision == "reject":
        row.review_status = ReviewStatus.REJECTED
        row.provenance = Provenance.U0
    elif decision != "edit":
        raise StoreError(f"Unknown decision '{decision}'")
    session.flush()
    return row


_UNIQUE_ON_EDIT = {
    "facts": ("entity_id", "attribute"),
    "knowledge": ("agent_entity_id", "subject_entity_id", "attribute"),
    "relationships": ("source_id", "target_id", "type"),
}


def _check_unique_after_edit(session: Session, table: str, row: Any, edits: dict[str, Any]) -> None:
    """Reject an edit that would duplicate a natural key, before anything is flushed."""
    cols = _UNIQUE_ON_EDIT.get(table)
    if not cols or not (set(cols) & set(edits)):
        return
    model = REVIEWABLE[table]
    target = {c: edits.get(c, getattr(row, c)) for c in cols}
    with session.no_autoflush:
        clash = session.scalar(select(model.id).where(
            model.version_id == row.version_id, model.id != row.id,
            *[getattr(model, c) == v for c, v in target.items()]))
    if clash:
        raise StoreError(f"Edit conflicts with an existing {table} row in this version")


def approve_version(session: Session, version_id: str) -> ScenarioVersion:
    """Freeze a version. Every row must be reviewed and every reference must be approved."""
    version = get_version(session, version_id)
    _require_draft(version)
    pending = sum(
        session.scalar(select(func.count()).select_from(m).where(
            m.version_id == version_id, m.review_status == ReviewStatus.PROPOSED)) or 0
        for m in REVIEWABLE.values()
    )
    if pending:
        raise StoreError(f"{pending} rows are still proposed. Approve or reject them first.")

    approved_ids = set(session.scalars(select(Entity.id).where(
        Entity.version_id == version_id, Entity.review_status == ReviewStatus.APPROVED)))
    dangling = []
    for model, cols in ((Fact, ["entity_id"]), (Relationship, ["source_id", "target_id"]),
                        (AgentProfile, ["entity_id"]),
                        (InitialKnowledge, ["agent_entity_id", "subject_entity_id"])):
        for row in session.scalars(select(model).where(
                model.version_id == version_id, model.review_status == ReviewStatus.APPROVED)):
            if any(getattr(row, c) not in approved_ids for c in cols):
                dangling.append(f"{model.__tablename__}:{row.id}")
    if dangling:
        raise StoreError(f"Approved rows point to rejected entities: {dangling[:5]}")

    # Run the approved rules once over the approved facts. Collisions (a rule that
    # would overwrite a seed fact) and non-converging rules must fail here, not mid-run.
    ents = {e.id: e for e in session.scalars(select(Entity).where(
        Entity.version_id == version_id, Entity.review_status == ReviewStatus.APPROVED))}
    facts = {(ents[f.entity_id].key, f.attribute): f.value
             for f in session.scalars(select(Fact).where(
                 Fact.version_id == version_id, Fact.review_status == ReviewStatus.APPROVED))}
    rule_defs = [(r.key, r.definition) for r in session.scalars(select(Rule).where(
        Rule.version_id == version_id, Rule.review_status == ReviewStatus.APPROVED))]
    try:
        rules_mod.derive(facts, {e.key: e.kind.value for e in ents.values()}, rule_defs)
    except rules_mod.RuleError as exc:
        raise StoreError(f"Rules conflict with approved facts: {exc}") from exc

    version.status = VersionStatus.APPROVED
    version.approved_at = datetime.now(timezone.utc)
    session.flush()
    return version


def new_version_from(session: Session, version_id: str, note: str | None = None) -> ScenarioVersion:
    """Copy a version (all non-rejected rows, same review state) into a new draft."""
    src = get_version(session, version_id)
    number = (session.scalar(select(func.max(ScenarioVersion.number)).where(
        ScenarioVersion.scenario_id == src.scenario_id)) or 0) + 1
    dst = ScenarioVersion(scenario_id=src.scenario_id, number=number, parent_id=src.id,
                          status=VersionStatus.DRAFT, note=note)
    session.add(dst)
    session.flush()

    doc_map: dict[str, str] = {}
    for d in session.scalars(select(SourceDocument).where(SourceDocument.version_id == src.id)):
        nd = SourceDocument(version_id=dst.id, filename=d.filename, sha256=d.sha256,
                            redacted_text=d.redacted_text, redaction_map=dict(d.redaction_map))
        session.add(nd)
        session.flush()
        doc_map[d.id] = nd.id

    ent_map: dict[str, str] = {}
    skip_cols = {"id", "version_id", "created_at"}
    fk_cols = {"entity_id", "source_id", "target_id", "agent_entity_id", "subject_entity_id"}
    for table in ("entities", "facts", "relationships", "rules", "profiles", "knowledge"):
        model = REVIEWABLE[table]
        for row in session.scalars(select(model).where(
                model.version_id == src.id, model.review_status != ReviewStatus.REJECTED)):
            data = {c.name: getattr(row, c.name) for c in model.__table__.columns if c.name not in skip_cols}
            refs = fk_cols & data.keys()
            if any(data[c] not in ent_map for c in refs):
                continue  # points at a rejected entity; it cannot be valid in the new version
            for c in refs:
                data[c] = ent_map[data[c]]
            if data.get("source_document_id"):
                data["source_document_id"] = doc_map.get(data["source_document_id"])
            new = model(version_id=dst.id, **data)
            session.add(new)
            session.flush()
            if table == "entities":
                ent_map[row.id] = new.id
    return dst


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _ser(row: Any) -> dict[str, Any]:
    out = {}
    for c in row.__table__.columns:
        v = getattr(row, c.name)
        if hasattr(v, "value"):
            v = v.value
        elif isinstance(v, datetime):
            v = v.isoformat()
        out[c.name] = v
    return out


def version_payload(session: Session, version_id: str) -> dict[str, Any]:
    v = get_version(session, version_id)
    ents = session.scalars(select(Entity).where(Entity.version_id == version_id)).all()
    keys = {e.id: e.key for e in ents}
    data: dict[str, Any] = {
        "version": _ser(v),
        "scenario": _ser(v.scenario),
        "documents": [
            {k: val for k, val in _ser(d).items() if k not in ("redaction_map",)}
            | {"redacted_terms": len(d.redaction_map or {})}
            for d in session.scalars(select(SourceDocument).where(SourceDocument.version_id == version_id))
        ],
    }
    for table, model in REVIEWABLE.items():
        rows = []
        for r in session.scalars(select(model).where(model.version_id == version_id)):
            item = _ser(r)
            for col in ("entity_id", "source_id", "target_id", "agent_entity_id", "subject_entity_id"):
                if col in item:
                    item[col.replace("_id", "_key")] = keys.get(item[col])
            rows.append(item)
        data[table] = sorted(rows, key=lambda x: (x.get("entity_key") or x.get("key") or x.get("source_key") or x.get("agent_key") or "", x.get("attribute") or x.get("type") or ""))
    data["simulations"] = [
        _ser(s) | {"run_config": {k: s.run_config.get(k) for k in ("engine_version", "rules_hash", "seed")}}
        for s in session.scalars(select(Simulation).where(Simulation.version_id == version_id)
                                 .order_by(Simulation.created_at))
    ]
    return data


def simulation_payload(session: Session, simulation_id: str) -> dict[str, Any]:
    sim = session.get(Simulation, simulation_id)
    if sim is None:
        raise StoreError(f"Simulation not found: {simulation_id}")
    keys = dict(session.execute(select(Entity.id, Entity.key).where(Entity.version_id == sim.version_id)).all())
    events = []
    for e in session.scalars(select(Event).where(Event.simulation_id == simulation_id).order_by(Event.seq)):
        item = _ser(e)
        item["actor_key"] = keys.get(e.actor_entity_id)
        events.append(item)
    return {"simulation": _ser(sim), "events": events}


def list_scenarios(session: Session) -> list[dict[str, Any]]:
    out = []
    for s in session.scalars(select(Scenario).order_by(Scenario.created_at.desc())):
        out.append(_ser(s) | {"versions": [
            {"id": v.id, "number": v.number, "status": v.status.value, "note": v.note}
            for v in s.versions
        ]})
    return out
