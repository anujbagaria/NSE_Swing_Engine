"""Composite NotifierPort — fans one notification out to several channels.

Rationale (from the audit mandate: notifications must be decoupled and a single
channel outage must not lose advisories or fail the run):

  - The orchestrator holds ONE notifier and stays unaware of how many real
    channels exist. Adding SMS later means adding a channel here, not touching
    the orchestrator.
  - Delivery is best-effort across channels: every channel is attempted even if
    an earlier one throws. A method raises ONLY if EVERY channel failed — so as
    long as at least one channel (Discord OR email) delivers, the run succeeds.
  - When all channels fail, the aggregated error names each channel's failure,
    so the GitHub Actions log tells you exactly what broke.
"""
from __future__ import annotations

from ..domain.types import Advisory
from ..domain.ports import NotifierPort


class CompositeNotifier:
    def __init__(self, channels: list[NotifierPort]):
        if not channels:
            raise ValueError("CompositeNotifier needs at least one channel")
        self.channels = channels

    def _fan_out(self, method_name: str, *args) -> None:
        errors: list[str] = []
        delivered = 0
        for ch in self.channels:
            try:
                getattr(ch, method_name)(*args)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 — collect, don't abort
                errors.append(f"{type(ch).__name__}: {exc}")
        # Success if at least one channel delivered. Fail only if all did.
        if delivered == 0 and errors:
            raise RuntimeError(
                f"All notification channels failed for {method_name}: "
                + " | ".join(errors)
            )

    def send_advisories(self, advisories: list[Advisory]) -> None:
        self._fan_out("send_advisories", advisories)

    def send_failure(self, error: str, run_id: str) -> None:
        self._fan_out("send_failure", error, run_id)

    def send_heartbeat(self, run_id: str) -> None:
        self._fan_out("send_heartbeat", run_id)