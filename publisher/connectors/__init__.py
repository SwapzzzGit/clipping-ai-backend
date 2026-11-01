"""Connector registry. To add a platform, implement `Connector` and add it to CONNECTORS."""

from __future__ import annotations

import os

from publisher.connectors.base import Connector
from publisher.connectors.instagram import InstagramConnector
from publisher.connectors.instagram_unified import InstagramUnifiedConnector
from publisher.connectors.mock import MockConnector
from publisher.connectors.tiktok import TikTokConnector
from publisher.connectors.youtube import YouTubeConnector

CONNECTORS: dict[str, type[Connector]] = {
    "youtube": YouTubeConnector,
    "tiktok": TikTokConnector,
    "instagram": InstagramUnifiedConnector,      # Unified Login: the only option for new Meta apps
    "instagram_login": InstagramConnector,       # Instagram Login: older apps only; enable via PUBLISHER_PLATFORMS
    "mock": MockConnector,
    # Planned: "facebook" (Reels via Page video API), "linkedin" (Posts API + video upload), "x" (media upload v2).
}


def build_connectors(enabled: list[str] | None = None) -> dict[str, Connector]:
    """Instantiate connectors. PUBLISHER_PLATFORMS="youtube,tiktok" limits the set; the mock
    platform is hidden unless listed there or PUBLISHER_MOCK=1."""
    if enabled is None:
        env = os.environ.get("PUBLISHER_PLATFORMS")
        enabled = [p.strip() for p in env.split(",") if p.strip()] if env else [
            p for p in CONNECTORS if p not in ("mock", "instagram_login") or
            (p == "mock" and os.environ.get("PUBLISHER_MOCK") == "1")]
    return {name: CONNECTORS[name]() for name in enabled if name in CONNECTORS}
