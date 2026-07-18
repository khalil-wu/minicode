"""Chroma telemetry adapters used by local-only MiniCode storage."""

from __future__ import annotations

from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent


class NoopProductTelemetryClient(ProductTelemetryClient):
    """Disable Chroma product telemetry without depending on PostHog behavior."""

    def capture(self, event: ProductTelemetryEvent) -> None:
        return None
