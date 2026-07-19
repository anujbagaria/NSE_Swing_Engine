"""Discord implementation of NotifierPort.

Every message is an INSTRUCTION for the human to act on Kite manually — the
engine holds no broker credentials and places nothing itself. Exit advice is
expressed as a Kite OCO GTT (stop + target) so the position is never naked.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..domain.types import Action, Advisory

# Discord's webhook endpoint enforces a User-Agent as an anti-abuse measure and
# returns a bare 403 if it's missing or looks like a default library UA. urllib
# otherwise sends "Python-urllib/x.y", which Discord rejects. A descriptive UA
# per Discord's docs (product/version + contact URL) satisfies the requirement.
_USER_AGENT = "nse-swing-engine/2.0 (+https://github.com/)"

_ACTION_VERB = {
    Action.PLACE_GTT: "PLACE BUY GTT",
    Action.PLACE_OCO_EXIT: "PLACE OCO EXIT (stop+target)",
    Action.UPDATE_TRAIL: "RAISE TRAILING STOP",
    Action.CANCEL_GTT: "CANCEL GTT",
    Action.EXIT_POSITION: "EXIT POSITION NOW",
    Action.HOLD: "HOLD (no change)",
}


class DiscordNotifier:
    def __init__(self, webhook_url: str | None = None):
        self.webhook = webhook_url or os.environ.get("DISCORD_WEBHOOK", "")

    def _post(self, content: str) -> None:
        if not self.webhook:
            raise RuntimeError("DISCORD_WEBHOOK not configured")
        data = json.dumps({"content": content[:1900]}).encode()
        req = urllib.request.Request(
            self.webhook, data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as exc:
            # Surface Discord's response body so a 403/404 says WHY (bad UA,
            # revoked webhook, rate limit) instead of a bare status code.
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                body = "<no body>"
            hint = {
                403: "webhook rejected (check User-Agent or that the webhook "
                     "URL is valid and not revoked)",
                404: "webhook not found (URL wrong, or the webhook/channel was "
                     "deleted — recreate it and update the DISCORD_WEBHOOK secret)",
                429: "rate limited by Discord (too many messages)",
            }.get(exc.code, "see Discord response body")
            raise RuntimeError(
                f"Discord POST failed: HTTP {exc.code} — {hint}. Body: {body}"
            ) from exc

    def _format(self, a: Advisory) -> str:
        verb = _ACTION_VERB[a.action]
        if a.action == Action.PLACE_GTT:
            if a.target_price and a.target_price > 0:
                exit_leg = (f"on fill place OCO stop `{a.stop_price:.2f}` "
                            f"/ target `{a.target_price:.2f}`")
            else:
                exit_leg = (f"on fill place a single sell-GTT stop `{a.stop_price:.2f}` "
                            f"(no fixed target — exits via rotation or stop)")
            return (
                f"- `{a.ticker}` **{verb}** {a.quantity} @ trigger {a.trigger_price:.2f} "
                f"| {exit_leg} (regime={a.regime.value}) — {a.rationale}"
            )
        if a.action == Action.UPDATE_TRAIL:
            return f"- `{a.ticker}` **{verb}** to {a.stop_price:.2f} — {a.rationale}"
        if a.action in (Action.CANCEL_GTT, Action.EXIT_POSITION):
            return f"- `{a.ticker}` **{verb}** — {a.rationale}"
        return f"- `{a.ticker}` {verb} — {a.rationale}"

    def send_advisories(self, advisories: list[Advisory],
                        failed_tickers: list[tuple[str, str]] | None = None) -> None:
        actionable = [a for a in advisories if a.action != Action.HOLD]
        lines: list[str] = []
        if actionable:
            lines.append("**Swing engine advisories**")
            lines += [self._format(a) for a in actionable]
        else:
            lines.append("No actionable advisories this run.")
        if failed_tickers:
            lines.append("")
            lines.append(":warning: **Skipped tickers (data fetch failed):**")
            for tk, err in failed_tickers:
                lines.append(f"- `{tk}` — {err[:140]}")
            lines.append("_Check config/universe.json for typos or delisted symbols._")
        if actionable:
            lines.append("\n_Advisory only. You place/cancel every order manually on Kite._")
        self._post("\n".join(lines))

    def send_failure(self, error: str, run_id: str) -> None:
        self._post(f"@here **RUN FAILED** ({run_id})\n```{error[:1500]}```\n"
                   f"No state was committed. Silence would be wrong — investigate.")

    def send_heartbeat(self, run_id: str) -> None:
        self._post(f":green_heart: run {run_id} completed OK.")
