"""Canonical public assets used by a source checkout; wheel builds bind package data."""
from pathlib import Path


PUBLIC_ASSET_ROOT = Path(__file__).resolve().parents[1] / "frontend" / "public"
