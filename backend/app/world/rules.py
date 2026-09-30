"""Declarative rule evaluator.

A rule is JSON data. Nothing in it is executed as code.

    {
      "for_each": {"kind": "vehicle"},            # optional: binds $self to each entity of that kind
      "when": COND,
      "then": [{"set": ["$self", "medical_transport_eligible"], "value": EXPR}],
      "else": [...]                               # optional
    }

COND:
    {"all": [COND, ...]} | {"any": [COND, ...]} | {"not": COND}
    {"op": "==|!=|<|<=|>|>=|in|is_null|not_null", "left": EXPR, "right": EXPR}

EXPR:
    literal (number, string, bool, null, list)
    {"fact": [ENTITY, "attribute"]}      ENTITY is "$self", an entity key, or an EXPR
                                          that yields a key (e.g. the vehicle's operator)
    {"add"|"sub"|"mul"|"div"|"min"|"max": [EXPR, EXPR]}

If an expression reads a fact that does not exist, the rule does not fire for that
binding. The skip is reported (not silently guessed), so gaps in the world model
stay visible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

MAX_PASSES = 10

_ARITH: dict[str, Callable[[Any, Any], Any]] = {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
    "div": lambda a, b: a / b,
    "min": min,
    "max": max,
}

_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "in": lambda a, b: a in b,
}


class RuleError(ValueError):
    """Malformed rule definition."""


class MissingFact(LookupError):
    def __init__(self, entity: str, attribute: str):
        super().__init__(f"{entity}.{attribute}")
        self.entity = entity
        self.attribute = attribute


FactKey = tuple[str, str]


@dataclass
class Derivation:
    rule_key: str
    entity: str
    attribute: str
    value: Any
    inputs: dict[str, Any]  # "entity.attribute" -> value read while evaluating


@dataclass
class Skip:
    rule_key: str
    binding: Optional[str]
    missing: str


@dataclass
class DerivationResult:
    derivations: list[Derivation] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)


class _Ctx:
    def __init__(self, facts: Mapping[FactKey, Any], self_key: Optional[str]):
        self.facts = facts
        self.self_key = self_key
        self.inputs: dict[str, Any] = {}

    def entity(self, ref: str) -> str:
        if ref == "$self":
            if self.self_key is None:
                raise RuleError("$self used in a rule without for_each")
            return self.self_key
        return ref

    def read(self, ref: str, attribute: str) -> Any:
        ent = self.entity(ref)
        if (ent, attribute) not in self.facts:
            raise MissingFact(ent, attribute)
        value = self.facts[(ent, attribute)]
        self.inputs[f"{ent}.{attribute}"] = value
        return value


def _eval_expr(expr: Any, ctx: _Ctx) -> Any:
    if isinstance(expr, dict):
        if len(expr) != 1:
            raise RuleError(f"Expression must have exactly one key: {expr}")
        (op, args), = expr.items()
        if op == "fact":
            if not (isinstance(args, list) and len(args) == 2):
                raise RuleError("fact expects [entity, attribute]")
            ref = _eval_expr(args[0], ctx) if isinstance(args[0], dict) else args[0]
            if not isinstance(ref, str):
                raise RuleError(f"Entity reference must resolve to a key, got {ref!r}")
            return ctx.read(ref, args[1])
        if op in _ARITH:
            if not (isinstance(args, list) and len(args) == 2):
                raise RuleError(f"{op} expects two operands")
            return _ARITH[op](_eval_expr(args[0], ctx), _eval_expr(args[1], ctx))
        raise RuleError(f"Unknown expression operator: {op}")
    return expr


def _eval_cond(cond: Any, ctx: _Ctx) -> bool:
    if not isinstance(cond, dict):
        raise RuleError(f"Condition must be an object: {cond}")
    if "all" in cond:
        return all(_eval_cond(c, ctx) for c in cond["all"])
    if "any" in cond:
        return any(_eval_cond(c, ctx) for c in cond["any"])
    if "not" in cond:
        return not _eval_cond(cond["not"], ctx)
    op = cond.get("op")
    left = _eval_expr(cond.get("left"), ctx)
    if op == "is_null":
        return left is None
    if op == "not_null":
        return left is not None
    if op not in _COMPARE:
        raise RuleError(f"Unknown comparison operator: {op}")
    return bool(_COMPARE[op](left, _eval_expr(cond.get("right"), ctx)))


def validate_rule(definition: Mapping[str, Any]) -> None:
    """Structural check used before a rule is stored or approved."""
    if "when" not in definition or "then" not in definition:
        raise RuleError("Rule needs 'when' and 'then'")
    for branch in ("then", "else"):
        for eff in definition.get(branch, []) or []:
            target = eff.get("set")
            if not (isinstance(target, list) and len(target) == 2):
                raise RuleError(f"Effect needs set: [entity, attribute]: {eff}")
            if "value" not in eff:
                raise RuleError(f"Effect needs a value: {eff}")
    fe = definition.get("for_each")
    if fe is not None and not (isinstance(fe, dict) and "kind" in fe):
        raise RuleError("for_each must be {\"kind\": ...}")


def rules_hash(rules: Iterable[tuple[str, Mapping[str, Any]]]) -> str:
    blob = json.dumps(sorted((k, d) for k, d in rules), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def derive(
    facts: Mapping[FactKey, Any],
    entity_kinds: Mapping[str, str],
    rules: Iterable[tuple[str, Mapping[str, Any]]],
) -> tuple[dict[FactKey, Any], DerivationResult]:
    """Apply rules to a fixpoint. Returns (facts + derived facts, trace).

    Rules may only set attributes that are not seed facts; a rule cannot overwrite
    F0 truth. Evaluation order is sorted by rule key so results are reproducible.
    """
    base = dict(facts)
    seed_keys = set(base)
    ordered = sorted(rules, key=lambda r: r[0])
    for _, d in ordered:
        validate_rule(d)

    current = dict(base)
    result = DerivationResult()
    for _ in range(MAX_PASSES):
        derived: dict[FactKey, Derivation] = {}
        skips: list[Skip] = []
        for key, d in ordered:
            fe = d.get("for_each")
            bindings: list[Optional[str]]
            if fe:
                bindings = sorted(k for k, kind in entity_kinds.items() if kind == fe["kind"])
            else:
                bindings = [None]
            for b in bindings:
                ctx = _Ctx(current, b)
                try:
                    effects = d["then"] if _eval_cond(d["when"], ctx) else (d.get("else") or [])
                    for eff in effects:
                        ent = ctx.entity(eff["set"][0])
                        attr = eff["set"][1]
                        if (ent, attr) in seed_keys:
                            raise RuleError(f"Rule {key} may not overwrite seed fact {ent}.{attr}")
                        value = _eval_expr(eff["value"], ctx)
                        derived[(ent, attr)] = Derivation(key, ent, attr, value, dict(ctx.inputs))
                except MissingFact as mf:
                    skips.append(Skip(key, b, f"{mf.entity}.{mf.attribute}"))
        nxt = dict(base)
        nxt.update({k: v.value for k, v in derived.items()})
        if nxt == current:
            result.derivations = sorted(derived.values(), key=lambda x: (x.entity, x.attribute))
            result.skips = skips
            return current, result
        current = nxt
    raise RuleError(f"Rules did not converge in {MAX_PASSES} passes")
