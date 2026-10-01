"""World model schema (ScenarioIQ v2, P0).

Design rules:
- World truth (facts), agent knowledge/beliefs, and the event log are separate tables.
- Core entities, facts and relationships are queryable rows. JSON is used only for
  flexible values and payloads, never for the whole world state.
- Every content row carries a provenance tag (see Provenance) and, where it came
  from a document, the source quote that supports it.
- The schema uses only portable types so a move to PostgreSQL is a URL change.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Provenance(str, enum.Enum):
    """Epistemic classification for every material statement."""

    F0 = "F0"  # Seed fact: approved by a human from a source
    F1 = "F1"  # Deterministically derived fact (rules engine, arithmetic)
    A0 = "A0"  # Agent action
    A1 = "A1"  # Agent assertion (what an agent says or believes)
    S0 = "S0"  # Simulation outcome (validation result, state change)
    I0 = "I0"  # Analyst inference
    P0 = "P0"  # Scenario projection
    U0 = "U0"  # Uncertainty
    X0 = "X0"  # Exogenous shock: change injected from outside the simulation


class ReviewStatus(str, enum.Enum):
    PROPOSED = "proposed"  # drafted (LLM or import), not yet trusted
    APPROVED = "approved"  # human-approved; becomes F0
    REJECTED = "rejected"


class VersionStatus(str, enum.Enum):
    DRAFT = "draft"  # editable
    APPROVED = "approved"  # frozen; simulations may run on it


class EntityKind(str, enum.Enum):
    ORGANISATION = "organisation"
    AGENT = "agent"
    ASSET = "asset"
    VEHICLE = "vehicle"
    FACILITY = "facility"
    LOCATION = "location"
    COMMODITY = "commodity"
    INVENTORY = "inventory"
    CONTRACT = "contract"
    POLICY = "policy"
    CHANNEL = "channel"
    EVENT = "event"


class Origin(str, enum.Enum):
    LLM = "llm"
    HUMAN = "human"
    FIXTURE = "fixture"
    ENGINE = "engine"


class KnowledgeKind(str, enum.Enum):
    KNOWS = "knows"  # agent has been told / observed this value
    BELIEVES = "believes"  # agent's working assumption (may be stale or wrong)


_ts = lambda: mapped_column(DateTime(timezone=True), default=_now, nullable=False)  # noqa: E731


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()

    versions: Mapped[list["ScenarioVersion"]] = relationship(
        back_populates="scenario", order_by="ScenarioVersion.number"
    )


class ScenarioVersion(Base):
    """A world model snapshot. Approved versions are immutable; edits create a new version."""

    __tablename__ = "scenario_versions"
    __table_args__ = (UniqueConstraint("scenario_id", "number"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenarios.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("scenario_versions.id"))
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, native_enum=False, length=16), default=VersionStatus.DRAFT
    )
    ontology_version: Mapped[str] = mapped_column(String(32), default="p0-1")
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    scenario: Mapped[Scenario] = relationship(back_populates="versions")


class SourceDocument(Base):
    """A briefing attached to a version. Only the redacted text is ever sent to an LLM."""

    __tablename__ = "source_documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    version_id: Mapped[str] = mapped_column(ForeignKey("scenario_versions.id"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64))
    redacted_text: Mapped[str] = mapped_column(Text)
    # placeholder -> original term. Stays in the local DB; never sent to the LLM.
    redaction_map: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


class _Reviewed:
    """Columns shared by every reviewable world-model row."""

    review_status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, native_enum=False, length=16), default=ReviewStatus.PROPOSED
    )
    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, native_enum=False, length=4), default=Provenance.U0
    )
    origin: Mapped[Origin] = mapped_column(Enum(Origin, native_enum=False, length=16))
    source_document_id: Mapped[Optional[str]] = mapped_column(ForeignKey("source_documents.id"))
    source_quote: Mapped[Optional[str]] = mapped_column(Text)
    # True when source_quote was found verbatim in the redacted source text.
    quote_verified: Mapped[Optional[bool]] = mapped_column(Boolean)


class Entity(_Reviewed, Base):
    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("version_id", "key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    version_id: Mapped[str] = mapped_column(ForeignKey("scenario_versions.id"), index=True)
    key: Mapped[str] = mapped_column(String(100))  # stable slug, e.g. "rt-05"
    kind: Mapped[EntityKind] = mapped_column(Enum(EntityKind, native_enum=False, length=20))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text)
    # Set when an injection introduced the entity. Such entities exist only in runs that
    # apply that injection; the frozen seed world never includes them.
    injection_id: Mapped[Optional[str]] = mapped_column(ForeignKey("injections.id"), index=True)


class AgentProfile(_Reviewed, Base):
    """Decision-making profile for an actor entity (organisation or agent)."""

    __tablename__ = "agent_profiles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    version_id: Mapped[str] = mapped_column(ForeignKey("scenario_versions.id"), index=True)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), unique=True)
    role: Mapped[str] = mapped_column(String(200))
    objectives: Mapped[list[str]] = mapped_column(JSON, default=list)
    incentives: Mapped[list[str]] = mapped_column(JSON, default=list)
    authority: Mapped[list[str]] = mapped_column(JSON, default=list)  # permitted action types
    constraints: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_tolerance: Mapped[Optional[str]] = mapped_column(String(20))
    decision_style: Mapped[Optional[str]] = mapped_column(String(200))
    channels: Mapped[list[str]] = mapped_column(JSON, default=list)


class Fact(_Reviewed, Base):
    """Initial world truth: one attribute of one entity at simulation start."""

    __tablename__ = "facts"
    __table_args__ = (UniqueConstraint("version_id", "entity_id", "attribute"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    version_id: Mapped[str] = mapped_column(ForeignKey("scenario_versions.id"), index=True)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    attribute: Mapped[str] = mapped_column(String(100))
    value: Mapped[Any] = mapped_column(JSON)
    unit: Mapped[Optional[str]] = mapped_column(String(40))


class Relationship(_Reviewed, Base):
    __tablename__ = "relationships"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    version_id: Mapped[str] = mapped_column(ForeignKey("scenario_versions.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    type: Mapped[str] = mapped_column(String(60))  # e.g. owns, committed_to, located_at
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Rule(_Reviewed, Base):
    """Declarative rule (see world/rules.py for the JSON format)."""

    __tablename__ = "rules"
    __table_args__ = (UniqueConstraint("version_id", "key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    version_id: Mapped[str] = mapped_column(ForeignKey("scenario_versions.id"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)


class InitialKnowledge(_Reviewed, Base):
    """What an agent knows or believes at simulation start. May differ from Fact."""

    __tablename__ = "initial_knowledge"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    version_id: Mapped[str] = mapped_column(ForeignKey("scenario_versions.id"), index=True)
    agent_entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    subject_entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    attribute: Mapped[str] = mapped_column(String(100))
    value: Mapped[Any] = mapped_column(JSON)
    kind: Mapped[KnowledgeKind] = mapped_column(
        Enum(KnowledgeKind, native_enum=False, length=16), default=KnowledgeKind.BELIEVES
    )


# ---------------------------------------------------------------------------
# Runtime tables (append-only)
# ---------------------------------------------------------------------------


class Injection(Base):
    """New information introduced into a running world (escalation, de-escalation).

    An injection belongs to a scenario version and is reusable: the same document can be
    applied to several runs or branches. It never edits the frozen seed world.
    """

    __tablename__ = "injections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    version_id: Mapped[str] = mapped_column(ForeignKey("scenario_versions.id"), index=True)
    label: Mapped[str] = mapped_column(String(200))
    filename: Mapped[Optional[str]] = mapped_column(String(300))
    sha256: Mapped[Optional[str]] = mapped_column(String(64))
    redacted_text: Mapped[str] = mapped_column(Text, default="")
    redaction_map: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | ready
    created_at: Mapped[datetime] = _ts()


class InjectionItem(Base):
    """One change carried by an injection. Reviewed like seed rows before use.

    op: add_entity | set_fact | add_rule | set_profile
    """

    __tablename__ = "injection_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    injection_id: Mapped[str] = mapped_column(ForeignKey("injections.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    op: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    review_status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, native_enum=False, length=16), default=ReviewStatus.PROPOSED
    )
    origin: Mapped[Origin] = mapped_column(Enum(Origin, native_enum=False, length=16))
    source_quote: Mapped[Optional[str]] = mapped_column(Text)
    quote_verified: Mapped[Optional[bool]] = mapped_column(Boolean)
    auto_accepted: Mapped[bool] = mapped_column(Boolean, default=False)


class Simulation(Base):
    __tablename__ = "simulations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    version_id: Mapped[str] = mapped_column(ForeignKey("scenario_versions.id"), index=True)
    # A continuation starts from its parent's state after fork_round, then applies
    # injection_id (if any) before its first round. A root run has no parent.
    parent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("simulations.id"), index=True)
    fork_round: Mapped[Optional[int]] = mapped_column(Integer)
    injection_id: Mapped[Optional[str]] = mapped_column(ForeignKey("injections.id"))
    name: Mapped[str] = mapped_column(String(200))
    seed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="created")
    # Reproducibility record: engine/rules/prompt/model versions and parameters.
    run_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


class Event(Base):
    """Immutable event log entry. Never updated after insert."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("simulation_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    round: Mapped[int] = mapped_column(Integer)
    # observation | action | validation | state_change | knowledge_update | derivation
    kind: Mapped[str] = mapped_column(String(30))
    actor_entity_id: Mapped[Optional[str]] = mapped_column(ForeignKey("entities.id"))
    action_type: Mapped[Optional[str]] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    valid: Mapped[Optional[bool]] = mapped_column(Boolean)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    provenance: Mapped[Provenance] = mapped_column(Enum(Provenance, native_enum=False, length=4))
    caused_by_event_id: Mapped[Optional[str]] = mapped_column(ForeignKey("events.id"))
    model: Mapped[Optional[str]] = mapped_column(String(100))
    prompt_version: Mapped[Optional[str]] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class StateChange(Base):
    """One attribute mutation of world truth, caused by a validated event."""

    __tablename__ = "state_changes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    round: Mapped[int] = mapped_column(Integer)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    attribute: Mapped[str] = mapped_column(String(100))
    old_value: Mapped[Any] = mapped_column(JSON)
    new_value: Mapped[Any] = mapped_column(JSON)


class KnowledgeUpdate(Base):
    """Change to what an agent knows/believes during a run. Truth is untouched."""

    __tablename__ = "knowledge_updates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    round: Mapped[int] = mapped_column(Integer)
    agent_entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    subject_entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    attribute: Mapped[str] = mapped_column(String(100))
    value: Mapped[Any] = mapped_column(JSON)
    kind: Mapped[KnowledgeKind] = mapped_column(Enum(KnowledgeKind, native_enum=False, length=16))
