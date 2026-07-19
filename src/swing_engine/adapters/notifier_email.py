"""Email implementation of NotifierPort — an INDEPENDENT fallback channel.

Deliberately shares no code path with the Discord notifier so the two cannot
fail together: Discord uses an HTTPS webhook, this uses SMTP. If one channel is
down (outage, revoked webhook, anti-abuse block), the other still delivers the
week's advisories.

Uses only the standard library. Works with any SMTP server; for Gmail, create
an App Password (not your account password) and set:
    EMAIL_SMTP_HOST=smtp.gmail.com
    EMAIL_SMTP_PORT=465
    EMAIL_USERNAME=you@gmail.com
    EMAIL_PASSWORD=your-16-char-app-password
    EMAIL_TO=where-to-send@example.com   (defaults to EMAIL_USERNAME)
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

from ..domain.types import Action, Advisory

_ACTION_VERB = {
    Action.PLACE_GTT: "PLACE BUY GTT",
    Action.PLACE_OCO_EXIT: "PLACE OCO EXIT (stop+target)",
    Action.UPDATE_TRAIL: "RAISE TRAILING STOP",
    Action.CANCEL_GTT: "CANCEL GTT",
    Action.EXIT_POSITION: "EXIT POSITION NOW",
    Action.HOLD: "HOLD (no change)",
}


class EmailNotifier:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        to_addr: str | None = None,
    ):
        self.host = host or os.environ.get("EMAIL_SMTP_HOST", "")
        self.port = int(port or os.environ.get("EMAIL_SMTP_PORT", "465"))
        self.username = username or os.environ.get("EMAIL_USERNAME", "")
        self.password = password or os.environ.get("EMAIL_PASSWORD", "")
        self.to_addr = to_addr or os.environ.get("EMAIL_TO", "") or self.username

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password and self.to_addr)

    def _send(self, subject: str, body: str) -> None:
        if not self.configured:
            raise RuntimeError(
                "Email notifier not configured (need EMAIL_SMTP_HOST, "
                "EMAIL_USERNAME, EMAIL_PASSWORD, EMAIL_TO)"
            )
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.username
        msg["To"] = self.to_addr
        msg.set_content(body)
        ctx = ssl.create_default_context()
        # Port 465 = implicit TLS (SMTPS); 587 = STARTTLS.
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=20) as s:
                s.login(self.username, self.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                s.starttls(context=ctx)
                s.login(self.username, self.password)
                s.send_message(msg)

    def _format(self, a: Advisory) -> str:
        verb = _ACTION_VERB[a.action]
        if a.action == Action.PLACE_GTT:
            return (
                f"- {a.ticker}  {verb}  qty {a.quantity} @ trigger {a.trigger_price:.2f}\n"
                f"    on fill: single sell-GTT stop {a.stop_price:.2f} "
                f"(no fixed target; trailing stop is the exit)\n"
                f"    regime={a.regime.value} — {a.rationale}"
            )
        if a.action == Action.UPDATE_TRAIL:
            return f"- {a.ticker}  {verb} to {a.stop_price:.2f} — {a.rationale}"
        if a.action in (Action.CANCEL_GTT, Action.EXIT_POSITION):
            return f"- {a.ticker}  {verb} — {a.rationale}"
        return f"- {a.ticker}  {verb} — {a.rationale}"

    def send_advisories(self, advisories: list[Advisory],
                        failed_tickers: list[tuple[str, str]] | None = None) -> None:
        actionable = [a for a in advisories if a.action != Action.HOLD]
        body = ""
        if actionable:
            body += "Swing engine advisories\n\n"
            body += "\n".join(self._format(a) for a in actionable)
        else:
            body += "No actionable advisories this run."
        if failed_tickers:
            body += "\n\nWARNING - skipped tickers (data fetch failed):\n"
            body += "\n".join(f"- {tk}: {err[:140]}" for tk, err in failed_tickers)
            body += "\nCheck config/universe.json for typos or delisted symbols."
        if actionable:
            body += "\n\nAdvisory only. You place/cancel every order manually on Kite."
        subject = f"Swing engine: {len(actionable)} advisory(ies)"
        if failed_tickers:
            subject += f" ({len(failed_tickers)} ticker(s) skipped)"
        self._send(subject, body)

    def send_failure(self, error: str, run_id: str) -> None:
        self._send(
            f"[ALERT] Swing engine run FAILED ({run_id})",
            f"Run {run_id} failed.\n\n{error}\n\n"
            "No state was committed on a compute failure. Investigate the "
            "GitHub Actions log.",
        )

    def send_heartbeat(self, run_id: str) -> None:
        # Heartbeat over email would be noisy; keep it silent by design.
        # Discord carries the green-ping liveness signal.
        pass
