"""LLM draft of a world model from a redacted briefing.

The model only sees redacted text. Its output is sanitised, names are restored
locally, and every row enters the store as PROPOSED. Nothing becomes F0 until a
human approves it in the World editor.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Optional

from . import rules as rules_mod
from .models import EntityKind

PROMPT_VERSION = "world-extract-1"
MAX_CHARS = 60_000

SYSTEM_PROMPT = """You extract a structured world model from an operational briefing for a crisis-planning simulator.

Some names are replaced by placeholders such as [ORG_1] or [TERM_2]. Keep placeholders exactly as written; never guess what they stand for.

Output one JSON object with these keys:

"entities": [{"key": short-kebab-id, "kind": one of %(kinds)s, "name": display name, "description": optional, "source_quote": exact text}]
"facts": [{"entity": entity key, "attribute": snake_case, "value": number | string | boolean | null, "unit": optional, "source_quote": exact text}]
  - One attribute per fact. Numbers as numbers, not strings. Put units in "unit".
  - Mutable state such as who an asset is committed to or assigned to is a fact whose value is an entity key (attributes "committed_to", "assigned_to", "operator").
  - Certifications and approvals as booleans (for example "gdp_certified": false when expired).
"relationships": [{"source": entity key, "target": entity key, "type": snake_case verb such as owns, operates, supplies, located_at, standby_for, depends_on, "source_quote": exact text}]
"profiles": [{"entity": key of an organisation or agent, "role": text, "objectives": [text], "incentives": [text], "constraints": [text], "authority": subset of ["REQUEST_ASSET", "RELEASE_ASSET"], "risk_tolerance": "low" | "medium" | "high" | null, "decision_style": text | null}]
"knowledge": [{"agent": actor key, "subject": entity key, "attribute": attribute name, "value": what the actor knows or believes, "kind": "knows" | "believes", "source_quote": exact text}]
  - Only record knowledge the briefing states. Use it especially where an actor's view differs from the facts.
"rules": [{"key": kebab-id, "description": text, "source_quote": exact text, "definition": RULE}]
  - Only for explicit eligibility or constraint statements.
  - RULE = {"for_each": {"kind": entity kind} (optional), "when": COND, "then": [{"set": [ENTITY, attribute], "value": EXPR}], "else": [...] (optional)}
  - COND = {"all": [COND]} | {"any": [COND]} | {"not": COND} | {"op": "==|!=|<|<=|>|>=|in|is_null|not_null", "left": EXPR, "right": EXPR}
  - EXPR = literal | {"fact": [ENTITY, attribute]} | {"add|sub|mul|div|min|max": [EXPR, EXPR]}; ENTITY = "$self" | entity key | {"fact": [...]} yielding a key.

Rules for every item:
- "source_quote" must be copied verbatim from the briefing (a short span, not a paraphrase). Omit the item if no text supports it.
- Do not invent numbers, names, or relationships. If the briefing is vague, leave the item out.
- Output JSON only."""


def _kinds() -> list[str]:
    return [k.value for k in EntityKind]


def build_messages(redacted_text: str, requirement: str | None = None) -> list[dict[str, str]]:
    user = "Briefing:\n\n" + redacted_text
    if requirement:
        user += "\n\nPlanning question (for relevance only, do not extract from it):\n" + requirement
    return [
        {"role": "system", "content": SYSTEM_PROMPT % {"kinds": json.dumps(_kinds())}},
        {"role": "user", "content": user},
    ]


def _slug(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(v).lower()).strip("-")


def sanitise(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Drop malformed items instead of failing the whole draft. Returns (spec, issues)."""
    issues: list[str] = []
    kinds = set(_kinds())
    spec: dict[str, list] = {k: [] for k in ("entities", "facts", "relationships", "profiles", "knowledge", "rules")}

    keys: set[str] = set()
    for e in raw.get("entities") or []:
        if not isinstance(e, dict) or not e.get("key"):
            issues.append(f"entity without key dropped: {e!r:.80}")
            continue
        kind = str(e.get("kind", "")).lower()
        if kind not in kinds:
            issues.append(f"entity {e['key']}: unknown kind '{kind}', dropped")
            continue
        k = _slug(e["key"])
        if k in keys:
            continue
        keys.add(k)
        spec["entities"].append({**e, "key": k, "kind": kind})

    def ref_ok(v: Any) -> bool:
        return isinstance(v, str) and _slug(v) in keys

    for f in raw.get("facts") or []:
        if isinstance(f, dict) and ref_ok(f.get("entity")) and f.get("attribute"):
            spec["facts"].append({**f, "entity": _slug(f["entity"])})
        else:
            issues.append(f"fact dropped (bad entity or attribute): {f!r:.100}")
    # One value per (entity, attribute): keep the first, report the rest.
    seen: set[tuple[str, str]] = set()
    deduped = []
    for f in spec["facts"]:
        key = (f["entity"], f["attribute"])
        if key in seen:
            issues.append(f"duplicate fact {key[0]}.{key[1]} dropped")
            continue
        seen.add(key)
        deduped.append(f)
    spec["facts"] = deduped

    for r in raw.get("relationships") or []:
        if isinstance(r, dict) and ref_ok(r.get("source")) and ref_ok(r.get("target")) and r.get("type"):
            spec["relationships"].append({**r, "source": _slug(r["source"]), "target": _slug(r["target"])})
        else:
            issues.append(f"relationship dropped: {r!r:.100}")

    actor_keys = {e["key"] for e in spec["entities"] if e["kind"] in ("organisation", "agent")}
    for p in raw.get("profiles") or []:
        if isinstance(p, dict) and isinstance(p.get("entity"), str) and _slug(p["entity"]) in actor_keys:
            auth = [a for a in p.get("authority") or [] if a in ("REQUEST_ASSET", "RELEASE_ASSET")]
            spec["profiles"].append({**p, "entity": _slug(p["entity"]), "authority": auth})
        else:
            issues.append(f"profile dropped (not an actor): {p!r:.100}")

    for k in raw.get("knowledge") or []:
        if (isinstance(k, dict) and isinstance(k.get("agent"), str) and _slug(k["agent"]) in actor_keys
                and ref_ok(k.get("subject")) and k.get("attribute")):
            kind = k.get("kind") if k.get("kind") in ("knows", "believes") else "believes"
            spec["knowledge"].append({**k, "agent": _slug(k["agent"]), "subject": _slug(k["subject"]), "kind": kind})
        else:
            issues.append(f"knowledge dropped: {k!r:.100}")

    for r in raw.get("rules") or []:
        try:
            if not isinstance(r, dict) or not r.get("key"):
                raise rules_mod.RuleError("missing key")
            rules_mod.validate_rule(r.get("definition") or {})
            spec["rules"].append({**r, "key": _slug(r["key"])})
        except rules_mod.RuleError as exc:
            issues.append(f"rule dropped ({exc}): {r!r:.100}")

    return spec, issues


def extract_world(
    redacted_text: str,
    *,
    requirement: str | None = None,
    chat_json: Optional[Callable[..., dict[str, Any]]] = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Call the (cheap-tier) model and return (spec, issues, call_meta).

    chat_json is injectable for tests. By default it uses WORLD_EXTRACTION_MODEL,
    falling back to LLM_MODEL_NAME: the first hook for the P1 model router.
    """
    if len(redacted_text) > MAX_CHARS:
        raise ValueError(f"Briefing is {len(redacted_text)} characters; the P0 limit is {MAX_CHARS}.")
    model_name = os.environ.get("WORLD_EXTRACTION_MODEL") or None
    if chat_json is None:
        from ..utils.llm_client import LLMClient

        client = LLMClient(model=model_name)
        model_name = client.model
        chat_json = client.chat_json
    raw = chat_json(messages=build_messages(redacted_text, requirement), temperature=0.1,
                    max_tokens=8000, max_attempts=2)
    spec, issues = sanitise(raw if isinstance(raw, dict) else {})
    return spec, issues, {"model": model_name, "prompt_version": PROMPT_VERSION}
