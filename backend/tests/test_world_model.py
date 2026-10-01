"""P0 world model: rules, redaction, extraction sanitising, store, engine slice, API."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.world import db, engine, extraction, rules, store
from app.world.memory import RecordingSink
from app.world.models import (
    Event,
    Fact,
    KnowledgeUpdate,
    Origin,
    ReviewStatus,
    StateChange,
    VersionStatus,
)
from app.world.redaction import redact, unredact


@pytest.fixture()
def dbsession(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'world.db'}"
    monkeypatch.setenv("WORLD_DB_URL", url)
    db.reset_engine()
    db.upgrade_to_head(url)
    with db.session_scope() as s:
        yield s
    db.reset_engine()


def _load(session, review=False):
    from app.api.world import load_rt05_fixture

    return load_rt05_fixture(session, review=review)


def _baseline():
    from app.api.world import _fixture

    return _fixture()[0]["scripts"]["baseline"]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def test_rules_derive_eligibility_and_cold_hold_risk():
    facts = {
        ("v1", "refrigerated"): True, ("v1", "operator"): "okco",
        ("v2", "refrigerated"): True, ("v2", "operator"): "badco",
        ("okco", "gdp_certified"): True, ("badco", "gdp_certified"): False,
        ("inv", "pallets"): 480, ("dock", "rate"): 40, ("inv", "limit"): 6,
    }
    kinds = {"v1": "vehicle", "v2": "vehicle", "okco": "organisation", "badco": "organisation",
             "inv": "inventory", "dock": "facility"}
    defs = [
        ("elig", {"for_each": {"kind": "vehicle"},
                  "when": {"all": [
                      {"op": "==", "left": {"fact": ["$self", "refrigerated"]}, "right": True},
                      {"op": "==", "left": {"fact": [{"fact": ["$self", "operator"]}, "gdp_certified"]}, "right": True}]},
                  "then": [{"set": ["$self", "ok"], "value": True}],
                  "else": [{"set": ["$self", "ok"], "value": False}]}),
        ("hours", {"when": {"op": ">", "left": {"fact": ["dock", "rate"]}, "right": 0},
                   "then": [{"set": ["inv", "hours"], "value": {"div": [{"fact": ["inv", "pallets"]}, {"fact": ["dock", "rate"]}]}}]}),
        ("risk", {"when": {"op": ">", "left": {"fact": ["inv", "hours"]}, "right": {"fact": ["inv", "limit"]}},
                  "then": [{"set": ["inv", "risk"], "value": True}]}),
    ]
    truth, trace = rules.derive(facts, kinds, defs)
    assert truth[("v1", "ok")] is True
    assert truth[("v2", "ok")] is False
    assert truth[("inv", "hours")] == 12
    assert truth[("inv", "risk")] is True  # needs a second pass: chained derivation
    risk = next(d for d in trace.derivations if d.attribute == "risk")
    assert risk.inputs == {"inv.hours": 12, "inv.limit": 6}


def test_rules_skip_on_missing_fact_and_refuse_to_overwrite_seed():
    defs = [("r", {"when": {"op": "==", "left": {"fact": ["x", "missing"]}, "right": 1},
                   "then": [{"set": ["x", "y"], "value": 1}]})]
    truth, trace = rules.derive({}, {}, defs)
    assert truth == {} and trace.skips[0].missing == "x.missing"

    bad = [("r", {"when": {"op": "==", "left": 1, "right": 1}, "then": [{"set": ["x", "seed"], "value": 2}]})]
    with pytest.raises(rules.RuleError, match="may not overwrite seed fact"):
        rules.derive({("x", "seed"): 1}, {"x": "asset"}, bad)


def test_rule_validation_rejects_malformed():
    with pytest.raises(rules.RuleError):
        rules.validate_rule({"when": {"op": "=="}})
    with pytest.raises(rules.RuleError):
        rules.validate_rule({"when": {}, "then": [{"set": "x"}]})


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redaction_replaces_terms_and_patterns_and_round_trips():
    text = ("Tidewater Logistics owns RT-05. tidewater logistics confirmed. Tidewater alone. "
            "Call 9123 4567 or ops@tidewater.example, NRIC S1234567D.")
    res = redact(text, [{"text": "Tidewater Logistics", "label": "org"}, "Tidewater"])
    assert "Tidewater" not in res.text and "tidewater logistics" not in res.text.lower()
    assert "9123 4567" not in res.text and "S1234567D" not in res.text and "@" not in res.text
    assert res.text.count("[ORG_1]") == 2  # case-insensitive, same placeholder
    assert unredact(res.text, res.mapping) == text.replace("tidewater logistics", "Tidewater Logistics")


# ---------------------------------------------------------------------------
# Extraction (model stubbed)
# ---------------------------------------------------------------------------


def test_extraction_sanitises_and_never_sees_real_names(dbsession):
    captured = {}

    def fake_chat_json(messages, **kw):
        captured["prompt"] = messages[1]["content"]
        return {
            "entities": [
                {"key": "[ORG_1]", "kind": "organisation", "name": "[ORG_1]", "source_quote": "[ORG_1] owns refrigerated truck RT-05."},
                {"key": "rt-05", "kind": "vehicle", "name": "RT-05", "source_quote": "RT-05 carries 16 pallets."},
                {"key": "ghost", "kind": "spaceship", "name": "Ghost"},
            ],
            "facts": [
                {"entity": "rt-05", "attribute": "capacity_pallets", "value": 16, "source_quote": "RT-05 carries 16 pallets."},
                {"entity": "rt-05", "attribute": "capacity_pallets", "value": 99, "source_quote": "made up"},
                {"entity": "nowhere", "attribute": "x", "value": 1},
            ],
            "relationships": [{"source": "[ORG_1]", "target": "rt-05", "type": "owns", "source_quote": "invented quote"}],
            "rules": [{"key": "broken", "definition": {"when": {}}}],
        }

    from app.api.world import _fixture
    from app.world.redaction import unredact as restore

    text = _fixture()[1]
    res = redact(text, [{"text": "Tidewater Logistics", "label": "org"}])
    v = store.create_scenario(dbsession, "draft")
    doc = store.add_document(dbsession, v.id, "b.md", "x", res.text, res.mapping)
    spec, issues, meta = extraction.extract_world(res.text, chat_json=fake_chat_json)
    assert "Tidewater Logistics" not in captured["prompt"]
    assert meta["prompt_version"] == extraction.PROMPT_VERSION
    assert any("spaceship" in i for i in issues)
    assert any("duplicate fact" in i for i in issues)
    assert any("rule dropped" in i for i in issues)

    spec = restore(spec, res.mapping)
    counts = store.import_world(dbsession, v.id, spec, origin=Origin.LLM, document=doc)
    assert counts["entities"] == 2 and counts["facts"] == 1 and counts["relationships"] == 1

    payload = store.version_payload(dbsession, v.id)
    names = {e["name"] for e in payload["entities"]}
    assert "Tidewater Logistics" in names  # restored locally after the model call
    assert all(r["review_status"] == "proposed" for r in payload["entities"] + payload["facts"])
    assert payload["facts"][0]["quote_verified"] is True
    assert payload["relationships"][0]["quote_verified"] is False  # fabricated quote flagged

    with pytest.raises(store.StoreError, match="cannot be auto-approved"):
        store.import_world(dbsession, v.id, spec, origin=Origin.LLM, approve=True)


# ---------------------------------------------------------------------------
# Store: review, approval, versioning
# ---------------------------------------------------------------------------


def test_review_flow_requires_all_rows_reviewed_then_freezes(dbsession):
    vid = _load(dbsession, review=True)
    with pytest.raises(store.StoreError, match="still proposed"):
        store.approve_version(dbsession, vid)
    with pytest.raises(engine.EngineError, match="approved versions"):
        engine.run_simulation(dbsession, vid, _baseline())

    payload = store.version_payload(dbsession, vid)
    for table in store.REVIEWABLE:
        for row in payload[table]:
            store.review_row(dbsession, table, row["id"], "approve")
    fact = dbsession.scalars(select(Fact).where(Fact.version_id == vid)).first()
    assert fact.provenance.value == "F0"
    assert all(f["quote_verified"] is not False for f in store.version_payload(dbsession, vid)["facts"])

    store.approve_version(dbsession, vid)
    with pytest.raises(store.StoreError, match="frozen"):
        store.review_row(dbsession, "facts", fact.id, "edit", {"value": 1})

    v2 = store.new_version_from(dbsession, vid, "raise dock throughput")
    assert v2.number == 2 and v2.status == VersionStatus.DRAFT and v2.parent_id == vid
    new_fact = dbsession.scalars(select(Fact).where(Fact.version_id == v2.id, Fact.attribute == fact.attribute,
                                                    Fact.value == fact.value)).first()
    edited = store.review_row(dbsession, "facts", new_fact.id, "edit", {"value": 999})
    assert edited.origin == Origin.HUMAN
    assert dbsession.get(Fact, fact.id).value != 999  # parent untouched


def test_approval_blocks_rows_pointing_at_rejected_entities(dbsession):
    vid = _load(dbsession, review=True)
    payload = store.version_payload(dbsession, vid)
    for table in store.REVIEWABLE:
        for row in payload[table]:
            decision = "reject" if table == "entities" and row["key"] == "linkhaul" else "approve"
            store.review_row(dbsession, table, row["id"], decision)
    with pytest.raises(store.StoreError, match="rejected entities"):
        store.approve_version(dbsession, vid)


# ---------------------------------------------------------------------------
# Engine slice
# ---------------------------------------------------------------------------


def test_rt05_baseline_slice(dbsession):
    vid = _load(dbsession)
    sink = RecordingSink()
    sim = engine.run_simulation(dbsession, vid, _baseline(), name="baseline", sink=sink)
    events = dbsession.scalars(select(Event).where(Event.simulation_id == sim.id).order_by(Event.seq)).all()

    # Round 0: deterministic derivations are logged as F1.
    derived = {(e.payload["entity"], e.payload["attribute"]): e.payload["value"]
               for e in events if e.kind == "derivation" and e.round == 0}
    assert all(e.provenance.value == "F1" for e in events if e.kind == "derivation")
    assert derived[("rt-05", "medical_transport_eligible")] is True
    assert derived[("ql-12", "medical_transport_eligible")] is False
    assert derived[("medical-inventory", "dock_handling_hours")] == 12
    assert derived[("medical-inventory", "cold_hold_breach_risk")] is True

    validations = [e for e in events if e.kind == "validation"]
    assert [v.valid for v in validations] == [False, False, True, True]
    assert "not eligible for medical transport" in validations[0].reason
    assert "gdp_certified" in validations[0].reason  # traceable to rule inputs
    assert validations[1].reason == "RT-05 is committed to Greenmart"

    # Truth vs belief: the Authority believed RT-05 was free; the log records the mismatch.
    obs = [e for e in events if e.kind == "observation" and e.payload["subject"] == "rt-05"][0]
    assert obs.provenance.value == "A1"
    assert obs.payload["beliefs"]["committed_to"] == {"value": None, "kind": "believes", "matches_truth": False}

    # Failure teaches the actor the blocking fact; truth itself did not change.
    kus = dbsession.scalars(select(KnowledgeUpdate).where(KnowledgeUpdate.simulation_id == sim.id)
                            .order_by(KnowledgeUpdate.round)).all()
    first = [(k.attribute, k.value) for k in kus if k.round == 1]
    assert ("medical_transport_eligible", False) in first and ("committed_to", "greenmart") in first
    assert len(sink.items) == len(kus)

    changes = dbsession.scalars(select(StateChange).where(StateChange.simulation_id == sim.id)
                                .order_by(StateChange.round)).all()
    assert [(c.round, c.attribute, c.old_value, c.new_value) for c in changes] == [
        (2, "committed_to", "greenmart", None),
        (3, "committed_to", None, "mla"),
        (3, "assigned_to", None, "mla"),
    ]
    assert sim.run_config["final_truth"]["rt-05.assigned_to"] == "mla"
    # Every state change is caused by a valid validation event.
    by_id = {e.id: e for e in events}
    for e in events:
        if e.kind == "state_change":
            assert by_id[e.caused_by_event_id].valid is True


def test_engine_rejects_unauthorised_and_unknown(dbsession):
    vid = _load(dbsession)
    sim = engine.run_simulation(dbsession, vid, [
        {"round": 1, "actor": "mla", "type": "RELEASE_ASSET", "params": {"asset": "rt-05"}},
        {"round": 1, "actor": "rt-05", "type": "REQUEST_ASSET", "params": {"asset": "ql-12"}},
        {"round": 1, "actor": "greenmart", "type": "TELEPORT", "params": {}},
        {"round": 1, "actor": "greenmart", "type": "REQUEST_ASSET", "params": {"asset": "kestrel-park"}},
    ])
    reasons = [e.reason for e in dbsession.scalars(select(Event).where(
        Event.simulation_id == sim.id, Event.kind == "validation").order_by(Event.seq))]
    assert "no authority" in reasons[0]
    assert "cannot act" in reasons[1]
    assert "Unsupported action type" in reasons[2]
    assert "not an allocatable asset" in reasons[3]
    assert dbsession.scalar(select(func.count()).select_from(StateChange)
                            .where(StateChange.simulation_id == sim.id)) == 0


def test_replay_is_deterministic(dbsession):
    vid = _load(dbsession)
    sim = engine.run_simulation(dbsession, vid, _baseline())
    rep = engine.replay(dbsession, sim.id)

    def trace(sid):
        return [(e.seq, e.round, e.kind, e.action_type, e.valid, e.reason, json.dumps(e.payload, sort_keys=True))
                for e in dbsession.scalars(select(Event).where(Event.simulation_id == sid).order_by(Event.seq))]

    assert trace(sim.id) == trace(rep.id)
    assert rep.run_config["rules_hash"] == sim.run_config["rules_hash"]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_DB_URL", f"sqlite:///{tmp_path / 'api.db'}")
    db.reset_engine()
    from app import create_app

    app = create_app()
    app.testing = True
    yield app.test_client()
    db.reset_engine()


def test_api_fixture_run_and_review(client):
    r = client.post("/api/world/fixtures/rt05", json={})
    assert r.status_code == 201, r.get_json()
    vid = r.get_json()["data"]["version_id"]
    script = r.get_json()["data"]["scripts"]["baseline"]

    r = client.post(f"/api/world/versions/{vid}/simulations", json={"actions": script})
    assert r.status_code == 201
    events = r.get_json()["data"]["events"]
    assert [e["valid"] for e in events if e["kind"] == "validation"] == [False, False, True, True]
    assert {e["provenance"] for e in events} >= {"F1", "A0", "A1", "S0"}

    r = client.get(f"/api/world/versions/{vid}")
    assert r.get_json()["data"]["version"]["status"] == "approved"
    fact_id = r.get_json()["data"]["facts"][0]["id"]
    r = client.patch(f"/api/world/rows/facts/{fact_id}", json={"decision": "reject"})
    assert r.status_code == 400 and "frozen" in r.get_json()["error"]

    r = client.post("/api/world/redact-preview", json={"text": "Call 9123 4567", "terms": []})
    assert "9123" not in r.get_json()["data"]["redacted_text"]

    r = client.post(f"/api/world/versions/{vid}/simulations", json={"actions": [{"round": 0}]})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Regressions from the P0 review
# ---------------------------------------------------------------------------


def test_reference_values_keep_entity_keys_after_unredaction():
    raw = {
        "entities": [{"key": "[ORG_1]", "kind": "organisation", "name": "[ORG_1]"},
                     {"key": "grocer", "kind": "organisation", "name": "[ORG_2]"},
                     {"key": "rt-05", "kind": "vehicle", "name": "RT-05"}],
        "facts": [{"entity": "rt-05", "attribute": "operator", "value": "[ORG_1]"},
                  {"entity": "rt-05", "attribute": "committed_to", "value": "[ORG_2]"},
                  {"entity": "rt-05", "attribute": "window", "value": "06:00-09:00"}],
        "knowledge": [{"agent": "[ORG_1]", "subject": "rt-05", "attribute": "committed_to", "value": "[ORG_2]"}],
    }
    spec, _ = extraction.sanitise(raw)
    spec = unredact(spec, {"[ORG_1]": "Tidewater Logistics", "[ORG_2]": "Greenmart"})
    values = {f["attribute"]: f["value"] for f in spec["facts"]}
    assert values == {"operator": "org-1", "committed_to": "grocer", "window": "06:00-09:00"}
    assert spec["knowledge"][0]["value"] == "grocer"
    assert {e["name"] for e in spec["entities"]} >= {"Tidewater Logistics", "Greenmart"}


def test_reimport_skips_duplicates_instead_of_failing(dbsession):
    from app.api.world import _fixture

    spec = _fixture()[0]
    v = store.create_scenario(dbsession, "dup")
    first = store.import_world(dbsession, v.id, spec, origin=Origin.HUMAN)
    second = store.import_world(dbsession, v.id, spec, origin=Origin.HUMAN)
    assert first["facts"] == 16 and second["facts"] == 0
    assert second["skipped_duplicates"] == sum(first[k] for k in store.REVIEWABLE)


def test_new_version_from_draft_drops_rows_on_rejected_entities(dbsession):
    vid = _load(dbsession, review=True)
    rt05 = next(e for e in store.version_payload(dbsession, vid)["entities"] if e["key"] == "rt-05")
    store.review_row(dbsession, "entities", rt05["id"], "reject")
    v2 = store.new_version_from(dbsession, vid)
    p2 = store.version_payload(dbsession, v2.id)
    assert "rt-05" not in {e["key"] for e in p2["entities"]}
    assert all(f["entity_key"] != "rt-05" for f in p2["facts"])
    assert all("rt-05" not in (r["source_key"], r["target_key"]) for r in p2["relationships"])


def test_rule_evaluation_errors_are_skips_not_crashes():
    defs = [("div", {"when": {"op": "==", "left": 1, "right": 1},
                     "then": [{"set": ["i", "h"], "value": {"div": [{"fact": ["i", "p"]}, {"fact": ["d", "r"]}]}}]}),
            ("cmp", {"when": {"op": ">", "left": {"fact": ["i", "n"]}, "right": 6},
                     "then": [{"set": ["i", "z"], "value": True}]})]
    truth, trace = rules.derive({("d", "r"): 0, ("i", "p"): 5, ("i", "n"): None}, {}, defs)
    assert ("i", "h") not in truth and ("i", "z") not in truth
    errors = {s.rule_key: s.error for s in trace.skips}
    assert "ZeroDivisionError" in errors["div"] and "TypeError" in errors["cmp"]


def test_approval_rejects_rule_that_would_overwrite_seed_fact(dbsession):
    vid = _load(dbsession, review=True)
    store.import_world(dbsession, vid, {"facts": [
        {"entity": "rt-05", "attribute": "medical_transport_eligible", "value": True}]}, origin=Origin.HUMAN)
    payload = store.version_payload(dbsession, vid)
    for table in store.REVIEWABLE:
        for row in payload[table]:
            store.review_row(dbsession, table, row["id"], "approve")
    with pytest.raises(store.StoreError, match="Rules conflict with approved facts"):
        store.approve_version(dbsession, vid)


def test_edit_that_collides_is_a_clean_error(dbsession):
    vid = _load(dbsession, review=True)
    facts = store.version_payload(dbsession, vid)["facts"]
    cap = next(f for f in facts if f["entity_key"] == "rt-05" and f["attribute"] == "capacity_pallets")
    with pytest.raises(store.StoreError, match="conflicts"):
        store.review_row(dbsession, "facts", cap["id"], "edit", {"attribute": "refrigerated"})
    assert dbsession.get(Fact, cap["id"]).attribute == "capacity_pallets"
    store.review_row(dbsession, "facts", cap["id"], "approve")  # session still usable


def test_actor_without_profile_has_no_authority(dbsession):
    vid = _load(dbsession)
    sim = engine.run_simulation(dbsession, vid, [
        {"round": 1, "actor": "linkhaul", "type": "REQUEST_ASSET", "params": {"asset": "ql-12"}}])
    v = dbsession.scalars(select(Event).where(Event.simulation_id == sim.id, Event.kind == "validation")).one()
    assert v.valid is False and "no approved agent profile" in v.reason


def test_redaction_nric_any_case_and_quantities_kept():
    res = redact("NRIC s1234567d, call 91234567. Stock 8000 0000 units, 90001234 %.")
    assert "s1234567d" not in res.text and "91234567" not in res.text
    assert "8000 0000 units" in res.text and "90001234 %" in res.text
