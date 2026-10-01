"""World model API (ScenarioIQ v2, P0).

Runs beside the legacy graph/simulation/report routes and does not touch them.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from functools import wraps
from typing import Any, Callable

from flask import jsonify, request
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from . import world_bp
from ..utils.logger import get_logger
from ..world import engine, extraction, store
from ..world.db import session_scope
from ..world.models import Origin
from ..world.redaction import redact
from ..world.rules import RuleError

logger = get_logger("scenarioiq.api.world")

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "world", "fixtures")


def _ok(data: Any = None, status: int = 200, **extra: Any):
    return jsonify({"success": True, "data": data, **extra}), status


def _fail(message: str, status: int = 400):
    return jsonify({"success": False, "error": message}), status


def api(fn: Callable) -> Callable:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        try:
            return fn(*args, **kwargs)
        except (store.StoreError, engine.EngineError, RuleError, ValueError) as exc:
            return _fail(str(exc), 400)
        except IntegrityError:
            return _fail("The change conflicts with an existing row (duplicate key)", 409)
        except ValidationError as exc:
            return _fail(f"Invalid action: {exc.errors()[0].get('msg')}", 400)
        except Exception:  # pragma: no cover - logged, generic message to client
            logger.exception("World API error")
            return _fail("Internal error in world API", 500)
    return wrapper


def _body() -> dict[str, Any]:
    return request.get_json(silent=True) or {}


# ---------------------------------------------------------------------------
# Scenarios, versions
# ---------------------------------------------------------------------------


@world_bp.route("/scenarios", methods=["GET"])
@api
def list_scenarios():
    with session_scope() as s:
        return _ok(store.list_scenarios(s))


@world_bp.route("/scenarios", methods=["POST"])
@api
def create_scenario():
    b = _body()
    if not (b.get("name") or "").strip():
        return _fail("name is required")
    with session_scope() as s:
        v = store.create_scenario(s, b["name"].strip(), b.get("description"))
        return _ok({"version_id": v.id, "scenario_id": v.scenario_id}, 201)


@world_bp.route("/versions/<version_id>", methods=["GET"])
@api
def get_version(version_id: str):
    with session_scope() as s:
        return _ok(store.version_payload(s, version_id))


@world_bp.route("/versions/<version_id>/approve", methods=["POST"])
@api
def approve_version(version_id: str):
    with session_scope() as s:
        store.approve_version(s, version_id)
        return _ok(store.version_payload(s, version_id))


@world_bp.route("/versions/<version_id>/new-version", methods=["POST"])
@api
def new_version(version_id: str):
    with session_scope() as s:
        v = store.new_version_from(s, version_id, _body().get("note"))
        return _ok({"version_id": v.id, "number": v.number}, 201)


@world_bp.route("/rows/<table>/<row_id>", methods=["PATCH"])
@api
def review_row(table: str, row_id: str):
    b = _body()
    with session_scope() as s:
        row = store.review_row(s, table, row_id, b.get("decision", "edit"), b.get("edits"))
        return _ok({"id": row.id, "review_status": row.review_status.value,
                    "provenance": row.provenance.value})


# ---------------------------------------------------------------------------
# Documents, redaction, extraction, import
# ---------------------------------------------------------------------------


def _read_upload() -> tuple[str, str]:
    """Return (filename, text) from a multipart file or JSON {filename, text}."""
    if "file" in request.files:
        from ..utils.file_parser import FileParser

        f = request.files["file"]
        name = os.path.basename(f.filename or "briefing.txt")
        suffix = os.path.splitext(name)[1].lower() or ".txt"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            f.save(tmp.name)
            path = tmp.name
        try:
            return name, FileParser.extract_text(path)
        finally:
            os.unlink(path)
    b = _body()
    return b.get("filename") or "briefing.txt", b.get("text") or ""


def _terms() -> list[Any]:
    if request.files:
        raw = request.form.get("terms") or "[]"
        return json.loads(raw)
    return _body().get("terms") or []


@world_bp.route("/redact-preview", methods=["POST"])
@api
def redact_preview():
    """Show exactly what would leave the machine. Stores nothing, calls no model."""
    _, text = _read_upload()
    result = redact(text, _terms())
    return _ok({"redacted_text": result.text,
                "placeholders": [{"placeholder": k, "length": len(v)} for k, v in result.mapping.items()]})


@world_bp.route("/versions/<version_id>/documents", methods=["POST"])
@api
def add_document(version_id: str):
    name, text = _read_upload()
    if not text.strip():
        return _fail("Document is empty")
    result = redact(text, _terms())
    with session_scope() as s:
        doc = store.add_document(s, version_id, name, hashlib.sha256(text.encode()).hexdigest(),
                                 result.text, result.mapping)
        return _ok({"document_id": doc.id, "redacted_text": result.text,
                    "redacted_terms": len(result.mapping)}, 201)


@world_bp.route("/versions/<version_id>/extract", methods=["POST"])
@api
def extract(version_id: str):
    from ..world.models import SourceDocument
    from ..world.redaction import unredact

    b = _body()
    with session_scope() as s:
        doc = s.get(SourceDocument, b.get("document_id") or "")
        if doc is None or doc.version_id != version_id:
            return _fail("document_id not found on this version")
        spec, issues, meta = extraction.extract_world(doc.redacted_text, requirement=b.get("requirement"))
        spec = unredact(spec, doc.redaction_map)  # names restored locally, after the model call
        counts = store.import_world(s, version_id, spec, origin=Origin.LLM, document=doc)
        return _ok({"counts": counts, "issues": issues, "call": meta})


@world_bp.route("/versions/<version_id>/import", methods=["POST"])
@api
def import_spec(version_id: str):
    """Manual JSON import. Rows enter as proposed and go through review like LLM drafts."""
    spec = _body().get("spec")
    if not isinstance(spec, dict):
        return _fail("spec (object) is required")
    with session_scope() as s:
        return _ok({"counts": store.import_world(s, version_id, spec, origin=Origin.HUMAN)})


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _fixture() -> tuple[dict[str, Any], str]:
    with open(os.path.join(FIXTURE_DIR, "rt05_world.json"), encoding="utf-8") as f:
        spec = json.load(f)
    with open(os.path.join(FIXTURE_DIR, "rt05_briefing.md"), encoding="utf-8") as f:
        text = f.read()
    return spec, text


def load_rt05_fixture(session, *, review: bool = False) -> str:
    """Create the synthetic RT-05 scenario. review=False approves and freezes it."""
    spec, text = _fixture()
    v = store.create_scenario(session, "RT-05 cold-chain (synthetic)",
                              "Synthetic test scenario: Kestrel Park medical relocation.")
    doc = store.add_document(session, v.id, "rt05_briefing.md",
                             hashlib.sha256(text.encode()).hexdigest(), text, {})
    store.import_world(session, v.id, spec, origin=Origin.FIXTURE, approve=not review, document=doc)
    if not review:
        store.approve_version(session, v.id)
    return v.id


@world_bp.route("/fixtures/rt05", methods=["POST"])
@api
def fixture_rt05():
    review = bool(_body().get("review"))
    with session_scope() as s:
        vid = load_rt05_fixture(s, review=review)
        return _ok({"version_id": vid, "scripts": _fixture()[0].get("scripts", {})}, 201)


@world_bp.route("/fixtures/rt05/scripts", methods=["GET"])
@api
def fixture_scripts():
    return _ok(_fixture()[0].get("scripts", {}))


# ---------------------------------------------------------------------------
# Simulations
# ---------------------------------------------------------------------------


@world_bp.route("/versions/<version_id>/simulations", methods=["POST"])
@api
def run(version_id: str):
    b = _body()
    actions = b.get("actions")
    if not isinstance(actions, list) or not actions:
        return _fail("actions (non-empty list) is required")
    with session_scope() as s:
        sim = engine.run_simulation(s, version_id, actions, name=b.get("name") or "Scripted run",
                                    seed=int(b.get("seed") or 0))
        return _ok(store.simulation_payload(s, sim.id), 201)


@world_bp.route("/simulations/<simulation_id>", methods=["GET"])
@api
def get_simulation(simulation_id: str):
    with session_scope() as s:
        return _ok(store.simulation_payload(s, simulation_id))


@world_bp.route("/simulations/<simulation_id>/replay", methods=["POST"])
@api
def replay(simulation_id: str):
    with session_scope() as s:
        sim = engine.replay(s, simulation_id)
        return _ok(store.simulation_payload(s, sim.id), 201)
