"""Run the live channels for one 園 and report what is watching and what is not.

The return value always carries ``channels`` alongside ``mentions``. A panel that
shows an empty mention list without saying three of four channels are pending
tells an operator the 園 is quiet, when the truth is that nobody is listening.
"""

from __future__ import annotations

from typing import Any

from .sources import LIVE, Channel, Mention, default_channels


def watch(
    institution: dict,
    channels: list[Channel] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Collect mentions from every live channel; never raise on one failure."""
    channels = channels if channels is not None else default_channels()
    mentions: list[Mention] = []
    errors: list[dict[str, str]] = []
    for ch in channels:
        if ch.status != LIVE:
            continue
        try:
            mentions.extend(ch.search(institution, limit=limit))
        except Exception as exc:  # noqa: BLE001 - one dead channel must not
            # take the whole panel down; the failure is reported, not swallowed.
            errors.append({"channel": ch.key, "error": f"{type(exc).__name__}: {exc}"})

    live = [c for c in channels if c.status == LIVE]
    return {
        "institution_id": institution.get("id"),
        "institution_title": institution.get("title"),
        "channels": [c.describe() for c in channels],
        "channels_live": len(live),
        "channels_total": len(channels),
        "mentions": [m.as_dict() for m in mentions],
        # Display-only channels must not reach storage; the caller writing a CSV
        # can filter on this instead of having to know each channel's terms.
        "storable": [m.as_dict() for m in mentions if m.stored or _storable(m, channels)],
        "errors": errors,
        "disposition": "待人工研判",
    }


def _storable(mention: Mention, channels: list[Channel]) -> bool:
    for ch in channels:
        if ch.key == mention.channel:
            return ch.may_store
    return False


def coverage_report(channels: list[Channel] | None = None) -> dict[str, Any]:
    """What is watching, what is pending, and what each needs to switch on."""
    channels = channels if channels is not None else default_channels()
    return {
        "live": [c.describe() for c in channels if c.status == LIVE],
        "pending": [c.describe() for c in channels if c.status != LIVE],
    }
