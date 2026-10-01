"""Injections: new information introduced into a running world.

Flow:
    create (redacted with the scenario's shared placeholders)
    -> draft items (LLM, or a hand-written spec)
    -> review items (auto-accept only where the source text proves the item)
    -> finalize (status "ready"; frozen)
    -> apply when continuing a run (engine.run_simulation with injection_id)

Delta spec (the interchange format, LLM output and fixtures):

    {
      "new_entities":  [{"key", "kind", "name", "description?", "source_quote?"}],
      "fact_changes":  [{"entity", "attribute", "value", "unit?", "informed?": [actor keys],
                         "source_quote?"}],
      "rules":         [{"key", "description", "definition", "source_quote?"}],
      "profiles":      [{"entity", "role", "authority", ... , "source_quote?"}]
    }

A rule with the same key as a seed rule replaces it in runs that apply the injection.
A profile for an existing actor replaces that actor's profile in those runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import rules as rules_mod
from .models import (
    Entity,
    EntityKind,
    Fact,
    Injection,
    InjectionItem,
    Origin,
    Provenance,
    ReviewStatus,
    ScenarioVersion,
    SourceDocument,
    VersionStatus,
)
from .redaction import redact, unredact, unredact_text
from .store import StoreError, _ser, _slug, quote_found

PROMPT_VERSION = "inject-extract-1"
OPS = ("add_entity", "set_fact", "add_rule", "set_profile")
ACTOR_KINDS = {"organisation", "agent"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def scenario_mapping(session: Session, version_id: str, exclude: Optional[str] = None) -> dict[str, str]:
    """Union of placeholder maps across the version's documents and injections."""
    mapping: dict[str, str] = {}
    for d in session.scalars(select(SourceDocument).where(SourceDocument.version_id == version_id)):
        mapping.update(d.redaction_map or {})
    for i in session.scalars(select(Injection).where(Injection.version_id == version_id)
                             .order_by(Injection.created_at)):
        if i.id != exclude:
            mapping.update(i.redaction_map or {})
    return mapping


def _injection(session: Session, injection_id: str) -> Injection:
    inj = session.get(Injection, injection_id)
    if inj is None:
        raise StoreError(f"Injection not found: {injection_id}")
    return inj


def _require_draft(inj: Injection) -> None:
    if inj.status != "draft":
        raise StoreError("This injection is finalized. Create a new one to change it.")


def _seed_entities(session: Session, version_id: str) -> dict[str, Entity]:
    return {e.key: e for e in session.scalars(select(Entity).where(
        Entity.version_id == version_id, Entity.injection_id.is_(None),
        Entity.review_status == ReviewStatus.APPROVED))}


def _number_in_quote(value: Any, quote: Optional[str]) -> bool:
    if not quote:
        return False
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    text = quote.replace(",", "")
    candidates = {str(value)}
    if float(value).is_integer():
        candidates.add(str(int(value)))
    return any(re.search(rf"(?<![\d.]){re.escape(c)}(?![\d.])", text) for c in candidates)


def auto_acceptable(op: str, payload: dict[str, Any], quote_verified: Optional[bool], quote: Optional[str]) -> bool:
    """Accept without review only when the source text itself proves the item.

    Rules and profiles (logic and authority) always need a human.
    """
    if quote_verified is not True:
        return False
    if op == "add_entity":
        return True
    if op == "set_fact":
        return _number_in_quote(payload.get("value"), quote)
    return False


# ---------------------------------------------------------------------------
# Create, import, draft
# ---------------------------------------------------------------------------


def create_injection(session: Session, version_id: str, label: str, text: str,
                     filename: str | None = None, terms: list | None = None) -> Injection:
    version = session.get(ScenarioVersion, version_id)
    if version is None:
        raise StoreError(f"Version not found: {version_id}")
    if version.status != VersionStatus.APPROVED:
        raise StoreError("Approve the seed world before adding injections")
    if not (label or "").strip():
        raise StoreError("label is required")
    base = scenario_mapping(session, version_id)
    res = redact(text or "", terms or [], base_mapping=base)
    inj = Injection(version_id=version_id, label=label.strip(), filename=filename,
                    sha256=hashlib.sha256((text or "").encode()).hexdigest(),
                    redacted_text=res.text, redaction_map=res.mapping)
    session.add(inj)
    session.flush()
    return inj


def import_delta(session: Session, injection_id: str, spec: dict[str, Any], *,
                 origin: Origin, auto_accept: bool = True, approve_all: bool = False) -> dict[str, Any]:
    """Validate a delta spec and store it as reviewable items.

    approve_all is for hand-authored fixtures only; LLM output is never approved wholesale.
    """
    inj = _injection(session, injection_id)
    _require_draft(inj)
    if origin == Origin.LLM and approve_all:
        raise StoreError("LLM drafts cannot be auto-approved")
    source = unredact_text(inj.redacted_text, inj.redaction_map) if inj.redacted_text else None
    seed = _seed_entities(session, inj.version_id)
    existing_items = session.scalars(select(InjectionItem).where(InjectionItem.injection_id == inj.id)).all()
    new_keys = {it.payload["key"] for it in existing_items if it.op == "add_entity"}
    kinds = {k: e.kind.value for k, e in seed.items()} | {
        it.payload["key"]: it.payload["kind"] for it in existing_items if it.op == "add_entity"}
    seq = len(existing_items)
    issues: list[str] = []
    counts = {op: 0 for op in OPS} | {"auto_accepted": 0}

    def add(op: str, payload: dict[str, Any], quote: Optional[str]) -> None:
        nonlocal seq
        seq += 1
        verified = quote_found(quote, source)
        auto = (not approve_all) and auto_accept and auto_acceptable(op, payload, verified, quote)
        status = ReviewStatus.APPROVED if (approve_all or auto) else ReviewStatus.PROPOSED
        session.add(InjectionItem(injection_id=inj.id, seq=seq, op=op, payload=payload, origin=origin,
                                  source_quote=quote, quote_verified=verified,
                                  review_status=status, auto_accepted=auto))
        counts[op] += 1
        counts["auto_accepted"] += int(auto)

    for e in spec.get("new_entities") or []:
        key = _slug(e.get("key", ""))
        kind = str(e.get("kind", "")).lower()
        if not key or kind not in {k.value for k in EntityKind}:
            issues.append(f"new entity dropped (key/kind): {e!r:.100}")
            continue
        if key in seed:
            issues.append(f"new entity '{key}' already exists in the seed world; use fact_changes")
            continue
        if key in new_keys:
            continue
        new_keys.add(key)
        kinds[key] = kind
        add("add_entity", {"key": key, "kind": kind, "name": e.get("name") or key,
                           "description": e.get("description")}, e.get("source_quote"))

    known = set(seed) | new_keys
    actors = {k for k, kd in kinds.items() if kd in ACTOR_KINDS}

    def as_ref(v: Any) -> Any:
        if isinstance(v, str) and _slug(v) in known:
            return _slug(v)
        return v

    for f in spec.get("fact_changes") or []:
        ent = _slug(f.get("entity", ""))
        if ent not in known or not f.get("attribute"):
            issues.append(f"fact change dropped (unknown entity or no attribute): {f!r:.100}")
            continue
        informed = [_slug(a) for a in f.get("informed") or [] if _slug(a) in actors]
        add("set_fact", {"entity": ent, "attribute": f["attribute"], "value": as_ref(f.get("value")),
                         "unit": f.get("unit"), "informed": informed}, f.get("source_quote"))

    for r in spec.get("rules") or []:
        try:
            rules_mod.validate_rule(r.get("definition") or {})
        except rules_mod.RuleError as exc:
            issues.append(f"rule dropped ({exc}): {r!r:.100}")
            continue
        add("add_rule", {"key": _slug(r.get("key", "")), "description": r.get("description", ""),
                         "definition": r["definition"]}, r.get("source_quote"))

    for p in spec.get("profiles") or []:
        ent = _slug(p.get("entity", ""))
        if ent not in actors:
            issues.append(f"profile dropped (not an actor): {p!r:.100}")
            continue
        payload = {k: p.get(k) for k in ("role", "objectives", "incentives", "constraints",
                                         "risk_tolerance", "decision_style", "channels")}
        payload |= {"entity": ent,
                    "authority": [a for a in p.get("authority") or [] if a in ("REQUEST_ASSET", "RELEASE_ASSET")]}
        add("set_profile", payload, p.get("source_quote"))

    session.flush()
    return {"counts": counts, "issues": issues}


DELTA_PROMPT = """You update a crisis simulation with new information.

You receive (1) the current world: entities and their facts, and (2) a new briefing. Some names are placeholders such as [ORG_1]; keep them exactly as written.

Return one JSON object with only what the new briefing CHANGES or ADDS:

"new_entities": [{"key": short-kebab-id, "kind": one of %(kinds)s, "name": text, "source_quote": exact text}]
  - Only entities that are not already in the world.
"fact_changes": [{"entity": existing or new entity key, "attribute": snake_case, "value": number | string | boolean | null, "unit": optional, "informed": [actor keys who learn this now], "source_quote": exact text}]
  - Use the same attribute names as the current world when the fact already exists.
  - Values that name an entity hold that entity's key.
  - "informed" lists only actors the briefing says are told. Leave it empty if the briefing does not say.
"rules": [{"key": kebab-id, "description": text, "definition": RULE, "source_quote": exact text}]
  - Only for explicit new eligibility or constraint statements. To change an existing rule, reuse its key.
"profiles": [{"entity": actor key, "role": text, "authority": subset of ["REQUEST_ASSET", "RELEASE_ASSET"], "objectives": [text], "source_quote": exact text}]
  - Only for new actors, or actors whose authority the briefing changes.

RULE format: {"for_each": {"kind": ...} (optional), "when": COND, "then": [{"set": [ENTITY, attribute], "value": EXPR}], "else": [...] (optional)}; COND = {"all"|"any": [COND]} | {"not": COND} | {"op": "==|!=|<|<=|>|>=|in|is_null|not_null", "left": EXPR, "right": EXPR}; EXPR = literal | {"fact": [ENTITY, attribute]} | {"add|sub|mul|div|min|max": [EXPR, EXPR]}.

Every item needs a "source_quote" copied verbatim from the NEW briefing. Do not repeat facts that did not change. Do not invent numbers. Output JSON only."""


def world_context(session: Session, version_id: str) -> str:
    seed = _seed_entities(session, version_id)
    by_id = {e.id: e for e in seed.values()}
    lines = ["Entities:"]
    for e in sorted(seed.values(), key=lambda x: x.key):
        lines.append(f"- {e.key} ({e.kind.value}): {e.name}")
    lines.append("Facts at the start of the scenario:")
    for f in session.scalars(select(Fact).where(Fact.version_id == version_id,
                                                Fact.review_status == ReviewStatus.APPROVED)):
        if f.entity_id in by_id:
            lines.append(f"- {by_id[f.entity_id].key}.{f.attribute} = {json.dumps(f.value)}"
                         + (f" {f.unit}" if f.unit else ""))
    return "\n".join(lines)


def draft_with_llm(session: Session, injection_id: str, *, auto_accept: bool = True,
                   chat_json: Optional[Callable[..., dict[str, Any]]] = None) -> dict[str, Any]:
    inj = _injection(session, injection_id)
    _require_draft(inj)
    mapping = scenario_mapping(session, inj.version_id)
    # The world context holds real names from the DB: redact it with the same placeholders.
    context = redact(world_context(session, inj.version_id), base_mapping=mapping).text
    model_name = os.environ.get("WORLD_EXTRACTION_MODEL") or None
    if chat_json is None:
        from ..utils.llm_client import LLMClient

        client = LLMClient(model=model_name)
        model_name = client.model
        chat_json = client.chat_json
    messages = [
        {"role": "system", "content": DELTA_PROMPT % {"kinds": json.dumps([k.value for k in EntityKind])}},
        {"role": "user", "content": f"CURRENT WORLD\n{context}\n\nNEW BRIEFING\n{inj.redacted_text}"},
    ]
    raw = chat_json(messages=messages, temperature=0.1, max_tokens=6000, max_attempts=2)
    spec = unredact(raw if isinstance(raw, dict) else {}, inj.redaction_map)
    result = import_delta(session, inj.id, spec, origin=Origin.LLM, auto_accept=auto_accept)
    return result | {"call": {"model": model_name, "prompt_version": PROMPT_VERSION}}


# ---------------------------------------------------------------------------
# Review and finalize
# ---------------------------------------------------------------------------

EDITABLE = {
    "add_entity": {"name", "description", "kind"},
    "set_fact": {"attribute", "value", "unit", "informed"},
    "add_rule": {"description", "definition"},
    "set_profile": {"role", "authority", "objectives"},
}


def review_item(session: Session, item_id: str, decision: str, edits: dict | None = None) -> InjectionItem:
    item = session.get(InjectionItem, item_id)
    if item is None:
        raise StoreError(f"Injection item not found: {item_id}")
    _require_draft(_injection(session, item.injection_id))
    if edits:
        bad = set(edits) - EDITABLE[item.op]
        if bad:
            raise StoreError(f"Fields not editable on {item.op}: {sorted(bad)}")
        if item.op == "add_rule" and "definition" in edits:
            rules_mod.validate_rule(edits["definition"])
        if item.op == "add_entity" and "kind" in edits:
            EntityKind(edits["kind"])
        item.payload = {**item.payload, **edits}
        item.origin = Origin.HUMAN
        item.auto_accepted = False
    if decision == "approve":
        item.review_status = ReviewStatus.APPROVED
    elif decision == "reject":
        item.review_status = ReviewStatus.REJECTED
        item.auto_accepted = False
    elif decision != "edit":
        raise StoreError(f"Unknown decision '{decision}'")
    session.flush()
    return item


def finalize(session: Session, injection_id: str) -> Injection:
    """Freeze the injection and create rows for the entities it introduces."""
    inj = _injection(session, injection_id)
    _require_draft(inj)
    items = session.scalars(select(InjectionItem).where(InjectionItem.injection_id == inj.id)
                            .order_by(InjectionItem.seq)).all()
    if any(i.review_status == ReviewStatus.PROPOSED for i in items):
        raise StoreError("Review every proposed item first")
    approved = [i for i in items if i.review_status == ReviewStatus.APPROVED]
    if not approved:
        raise StoreError("The injection has no approved items")

    seed = _seed_entities(session, inj.version_id)
    added = {i.payload["key"] for i in approved if i.op == "add_entity"}
    for i in approved:
        refs = []
        if i.op == "set_fact":
            refs = [i.payload["entity"], *i.payload.get("informed", [])]
        elif i.op == "set_profile":
            refs = [i.payload["entity"]]
        missing = [r for r in refs if r not in seed and r not in added]
        if missing:
            raise StoreError(f"Item {i.seq} ({i.op}) refers to {missing}, which is rejected or unknown")

    for i in approved:
        if i.op != "add_entity":
            continue
        p = i.payload
        other = session.scalar(select(Entity).where(Entity.version_id == inj.version_id, Entity.key == p["key"]))
        if other is not None:
            if other.injection_id is None or other.kind.value != p["kind"]:
                raise StoreError(f"Entity key '{p['key']}' is already used in this scenario")
            continue  # same entity introduced by another injection: share its identity
        session.add(Entity(version_id=inj.version_id, key=p["key"], kind=EntityKind(p["kind"]),
                           name=p["name"], description=p.get("description"), injection_id=inj.id,
                           review_status=ReviewStatus.APPROVED, provenance=Provenance.X0,
                           origin=i.origin, source_quote=i.source_quote, quote_verified=i.quote_verified))
    inj.status = "ready"
    session.flush()
    return inj


def injection_payload(session: Session, injection_id: str) -> dict[str, Any]:
    inj = _injection(session, injection_id)
    data = _ser(inj)
    data.pop("redaction_map", None)
    data["redacted_terms"] = len(inj.redaction_map or {})
    data["items"] = [_ser(i) for i in session.scalars(
        select(InjectionItem).where(InjectionItem.injection_id == inj.id).order_by(InjectionItem.seq))]
    return data
