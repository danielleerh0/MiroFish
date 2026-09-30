"""Where agent knowledge goes besides the SQLite log.

In v2, Zep holds agent memory only (what each agent knows and believes). SQLite
stays the authority for world truth. P0 ships the interface and a no-op sink; the
Zep sink lands in P1 together with the OASIS mapping.
"""

from __future__ import annotations

from typing import Any, Protocol


class KnowledgeSink(Protocol):
    def publish(
        self, simulation_id: str, agent: str, subject: str, attribute: str,
        value: Any, kind: str, round_: int,
    ) -> None: ...


class NullSink:
    def publish(self, *args: Any, **kwargs: Any) -> None:
        return None


class RecordingSink:
    """Test helper."""

    def __init__(self) -> None:
        self.items: list[tuple] = []

    def publish(self, *args: Any, **kwargs: Any) -> None:
        self.items.append(args)
