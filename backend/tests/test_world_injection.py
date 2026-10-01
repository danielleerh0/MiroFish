"""Injections: shared redaction, delta review, fork + inject, branches, replay."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.world import db, engine, injection, store
from app.world.models import Entity, Event, Origin, Simulation
from app.world.redaction import redact


@pytest.fixture()
def s(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'inj.db'}"
    monkeypatch.setenv("WORLD_DB_URL", url)
    db.reset_engine()
    db.upgrade_to_head(url)
    with db.session_scope() as session:
        yield session
    db.reset_engine()


def _world(s):
    from app.api.world import _fixture, load_rt05_fixture

    vid = load_rt05_fixture(s)
    base = engine.run_simulation(s, vid, _fixture()[0]["scripts"]["baseline"][:2], name="baseline r1")
    return vid, base


def _events(s, sim_id, kind=None):
    q = select(Event).where(Event.simulation_id == sim_id)
    if kind:
        q = q.where(Event.kind == kind)
    return s.scalars(q.order_by(Event.seq)).all()


def test_branches_escalation_vs_deescalation(s):
    from app.api.world import load_rt05_injections

    vid, base = _world(s)
    fx = load_rt05_injections(s, vid)

    esc = engine.run_simulation(s, None, fx["scripts"]["escalation"], name="escalation",
                                parent_id=base.id, fork_round=1, injection_id=fx["injections"]["escalation"])
    dee = engine.run_simulation(s, None, fx["scripts"]["deescalation"], name="de-escalation",
                                parent_id=base.id, fork_round=1, injection_id=fx["injections"]["deescalation"])

    et, dt = esc.run_config["final_truth"], dee.run_config["final_truth"]
    # Escalation: slower dock, tighter cold hold, a new carrier.
    assert et["dock-1.throughput_pallets_per_hour"] == 25
    assert et["medical-inventory.dock_handling_hours"] == 19.2
    assert et["medical-inventory.cold_hold_breach_risk"] is True
    assert et["nw-03.medical_transport_eligible"] is True and et["nw-03.assigned_to"] == "mla"
    # De-escalation: less stock, Quayline recertified, no breach risk.
    assert dt["medical-inventory.dock_handling_hours"] == 3
    assert dt["medical-inventory.cold_hold_breach_risk"] is False
    assert dt["ql-12.assigned_to"] == "mla"
    assert "nw-03.capacity_pallets" not in dt  # the other branch's entity does not leak

    # Validations in each branch.
    assert [v.valid for v in _events(s, esc.id, "validation")] == [True, False]  # QL-12 still ineligible
    assert [v.valid for v in _events(s, dee.id, "validation")] == [True]

    # Injection changes are X0; derived consequences are F1 and point at the injection.
    inj_ev = _events(s, esc.id, "injection")[0]
    changes = [e for e in _events(s, esc.id, "state_change") if e.round == 2 and e.provenance.value == "X0"]
    assert {c.payload["attribute"] for c in changes} >= {"throughput_pallets_per_hour", "cold_hold_limit_hours"}
    derived = [e for e in _events(s, esc.id, "derivation") if e.caused_by_event_id == inj_ev.id]
    assert any(d.payload["attribute"] == "dock_handling_hours" and d.payload["value"] == 19.2 for d in derived)

    # Only the actors the briefing names learn the news.
    ku = {(e.actor_entity_id, e.payload["attribute"]) for e in _events(s, esc.id, "knowledge_update")
          if e.provenance.value == "X0"}
    mla_id = s.scalar(select(Entity.id).where(Entity.version_id == vid, Entity.key == "mla"))
    assert (mla_id, "throughput_pallets_per_hour") in ku
    assert (mla_id, "cold_hold_limit_hours") not in ku  # Authority not told about the generator

    # The parent and the seed world are untouched.
    assert base.run_config["final_truth"]["dock-1.throughput_pallets_per_hour"] == 40
    assert "nw-03" not in {e["key"] for e in store.version_payload(s, vid)["entities"]}


def test_state_at_rebuilds_from_log_and_nested_forks(s):
    from app.api.world import _fixture, load_rt05_injections

    vid, _ = _world(s)
    full = engine.run_simulation(s, vid, _fixture()[0]["scripts"]["baseline"], name="full")
    st = engine.state_at(s, full, 2)
    assert st.get("rt-05", "committed_to") is None and st.get("rt-05", "assigned_to") is None
    assert st.belief("mla", "rt-05", "committed_to")[0] == "greenmart"  # learned in round 1
    assert engine.state_at(s, full, 3).get("rt-05", "assigned_to") == "mla"

    fx = load_rt05_injections(s, vid)
    child = engine.run_simulation(s, None, [], parent_id=full.id, fork_round=3,
                                  injection_id=fx["injections"]["escalation"])
    grandchild = engine.run_simulation(s, None, [
        {"round": 5, "actor": "mla", "type": "REQUEST_ASSET", "params": {"asset": "nw-03", "purpose": "medical"}}],
        parent_id=child.id, fork_round=4)
    gt = grandchild.run_config["final_truth"]
    assert gt["rt-05.assigned_to"] == "mla" and gt["nw-03.assigned_to"] == "mla"
    assert gt["dock-1.throughput_pallets_per_hour"] == 25  # inherited injection


def test_replay_of_continuation_is_deterministic(s):
    from app.api.world import load_rt05_injections

    vid, base = _world(s)
    fx = load_rt05_injections(s, vid)
    esc = engine.run_simulation(s, None, fx["scripts"]["escalation"], parent_id=base.id, fork_round=1,
                                injection_id=fx["injections"]["escalation"])
    rep = engine.replay(s, esc.id)

    def trace(sid):
        return [(e.seq, e.round, e.kind, e.valid, e.reason, json.dumps(e.payload, sort_keys=True))
                for e in _events(s, sid)]

    assert trace(esc.id) == trace(rep.id)
    assert rep.parent_id == base.id and rep.injection_id == esc.injection_id


def test_injection_guards(s):
    vid, base = _world(s)
    inj = injection.create_injection(s, vid, "draft only", "Dock 1 now handles 25 pallets per hour.")
    with pytest.raises(engine.EngineError, match="Finalize"):
        engine.run_simulation(s, None, [], parent_id=base.id, fork_round=1, injection_id=inj.id)
    with pytest.raises(engine.EngineError, match="continue a run"):
        engine.run_simulation(s, vid, [], injection_id=inj.id)
    with pytest.raises(engine.EngineError, match="round 2 or later"):
        engine.run_simulation(s, None, [{"round": 1, "actor": "mla", "type": "REQUEST_ASSET",
                                         "params": {"asset": "rt-05"}}], parent_id=base.id, fork_round=1)
    with pytest.raises(engine.EngineError, match="between 0 and 1"):
        engine.run_simulation(s, None, [], parent_id=base.id, fork_round=7)

    draft = store.create_scenario(s, "unapproved")
    with pytest.raises(store.StoreError, match="Approve the seed world"):
        injection.create_injection(s, draft.id, "x", "text")


def test_review_auto_accept_only_when_source_proves_it(s):
    vid, _ = _world(s)
    text = "Dock 1 now handles 25 pallets per hour. Northwind Cold Chain joined. Dock 1 is slower."
    inj = injection.create_injection(s, vid, "test", text)
    result = injection.import_delta(s, inj.id, {
        "new_entities": [{"key": "northwind", "kind": "organisation", "name": "Northwind",
                          "source_quote": "Northwind Cold Chain joined."}],
        "fact_changes": [
            {"entity": "dock-1", "attribute": "throughput_pallets_per_hour", "value": 25,
             "source_quote": "Dock 1 now handles 25 pallets per hour."},          # proven: auto
            {"entity": "dock-1", "attribute": "throughput_pallets_per_hour_min", "value": 20,
             "source_quote": "Dock 1 is slower."},                                 # number not in quote
            {"entity": "dock-1", "attribute": "status", "value": "degraded",
             "source_quote": "Dock 1 is out of service"},                          # quote not in source
            {"entity": "ghost", "attribute": "x", "value": 1},                     # unknown entity
        ],
        "profiles": [{"entity": "northwind", "role": "carrier", "authority": ["RELEASE_ASSET"],
                      "source_quote": "Northwind Cold Chain joined."}],            # authority: always review
    }, origin=Origin.LLM)
    assert result["counts"]["auto_accepted"] == 2
    assert any("ghost" in i for i in result["issues"])
    items = injection.injection_payload(s, inj.id)["items"]
    status = {(i["op"], i["payload"].get("attribute") or i["payload"].get("key") or i["payload"].get("entity")):
              i["review_status"] for i in items}
    assert status[("add_entity", "northwind")] == "approved"
    assert status[("set_fact", "throughput_pallets_per_hour")] == "approved"
    assert status[("set_fact", "throughput_pallets_per_hour_min")] == "proposed"
    assert status[("set_fact", "status")] == "proposed"
    assert status[("set_profile", "northwind")] == "proposed"

    with pytest.raises(store.StoreError, match="Review every proposed item"):
        injection.finalize(s, inj.id)
    # Rejecting the new entity orphans the profile: finalize must refuse.
    for i in items:
        injection.review_item(s, i["id"], "reject" if i["op"] == "add_entity" else "approve")
    with pytest.raises(store.StoreError, match="rejected or unknown"):
        injection.finalize(s, inj.id)


def test_injection_rule_overrides_seed_rule_in_branch_only(s):
    vid, base = _world(s)
    inj = injection.create_injection(s, vid, "relaxation",
                                     "Emergency order: any refrigerated vehicle may carry medical stock.")
    injection.import_delta(s, inj.id, {"rules": [{
        "key": "medical-transport-eligibility",
        "description": "Emergency relaxation: refrigeration is enough.",
        "definition": {"for_each": {"kind": "vehicle"},
                       "when": {"op": "==", "left": {"fact": ["$self", "refrigerated"]}, "right": True},
                       "then": [{"set": ["$self", "medical_transport_eligible"], "value": True}],
                       "else": [{"set": ["$self", "medical_transport_eligible"], "value": False}]}}]},
        origin=Origin.HUMAN, approve_all=True)
    injection.finalize(s, inj.id)
    branch = engine.run_simulation(s, None, [
        {"round": 2, "actor": "mla", "type": "REQUEST_ASSET", "params": {"asset": "ql-12", "purpose": "medical"}}],
        parent_id=base.id, fork_round=1, injection_id=inj.id)
    assert [v.valid for v in _events(s, branch.id, "validation")] == [True]
    rule_ev = _events(s, branch.id, "rule_set")[0]
    assert rule_ev.payload["replaces_seed_rule"] is True
    assert base.run_config["final_truth"]["ql-12.medical_transport_eligible"] is False


def test_shared_placeholders_across_seed_and_injection(s):
    v = store.create_scenario(s, "shared")
    seed = redact("Tidewater Logistics owns RT-05.", [{"text": "Tidewater Logistics", "label": "ORG"}])
    store.add_document(s, v.id, "seed.md", "x", seed.text, seed.mapping)
    store.import_world(s, v.id, {"entities": [{"key": "rt-05", "kind": "vehicle", "name": "RT-05"}]},
                       origin=Origin.FIXTURE, approve=True)
    store.approve_version(s, v.id)
    inj = injection.create_injection(s, v.id, "u", "Tidewater Logistics recalled RT-05. Northwind helps.",
                                     terms=[{"text": "Northwind", "label": "ORG"}])
    assert inj.redacted_text == "[ORG_1] recalled RT-05. [ORG_2] helps."

    captured = {}

    def fake(messages, **kw):
        captured["user"] = messages[1]["content"]
        return {"fact_changes": [{"entity": "rt-05", "attribute": "operator", "value": "[ORG_1]",
                                  "source_quote": "[ORG_1] recalled RT-05."}]}

    out = injection.draft_with_llm(s, inj.id, chat_json=fake)
    assert "Tidewater" not in captured["user"] and "Northwind" not in captured["user"]
    item = injection.injection_payload(s, inj.id)["items"][0]
    assert item["source_quote"] == "Tidewater Logistics recalled RT-05." and item["quote_verified"] is True
    assert out["call"]["prompt_version"] == injection.PROMPT_VERSION


def test_api_inject_and_continue(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_DB_URL", f"sqlite:///{tmp_path / 'api.db'}")
    db.reset_engine()
    from app import create_app

    c = create_app().test_client()
    r = c.post("/api/world/fixtures/rt05", json={})
    vid, script = r.get_json()["data"]["version_id"], r.get_json()["data"]["scripts"]["baseline"]
    sim_id = c.post(f"/api/world/versions/{vid}/simulations", json={"actions": script[:2]}).get_json()["data"]["simulation"]["id"]

    r = c.post(f"/api/world/versions/{vid}/injections",
               json={"label": "manual", "text": "Dock 1 now handles 25 pallets per hour."})
    assert r.status_code == 201
    inj_id = r.get_json()["data"]["id"]
    r = c.post(f"/api/world/injections/{inj_id}/import", json={"spec": {"fact_changes": [
        {"entity": "dock-1", "attribute": "throughput_pallets_per_hour", "value": 25,
         "source_quote": "Dock 1 now handles 25 pallets per hour."}]}})
    assert r.get_json()["data"]["counts"]["auto_accepted"] == 1
    assert c.post(f"/api/world/injections/{inj_id}/finalize").status_code == 200

    r = c.post(f"/api/world/simulations/{sim_id}/continue", json={"fork_round": 1, "injection_id": inj_id})
    assert r.status_code == 201, r.get_json()
    kinds = [e["kind"] for e in r.get_json()["data"]["events"]]
    assert kinds[:2] == ["fork", "injection"] and "state_change" in kinds

    payload = c.get(f"/api/world/versions/{vid}").get_json()["data"]
    assert payload["injections"][0]["status"] == "ready"
    child = [x for x in payload["simulations"] if x["parent_id"] == sim_id][0]
    assert child["fork_round"] == 1 and child["injection_label"] == "manual"
    db.reset_engine()
