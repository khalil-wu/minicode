"""Signed asset tokens carry a bounded lifetime, not just an unexpired stamp."""

from urllib.parse import urlencode

from starlette.requests import Request

from backend.api import auth as api_auth


def _artifact_request(token: str) -> Request:
    query = urlencode({"artifact_id": "a1", "session_id": "s1", "conversation_id": "c1", "asset_token": token})
    return Request({"type": "http", "method": "GET", "path": "/api/artifacts/raw", "headers": [],
                    "query_string": query.encode("ascii")})


def _artifact_token(expires_at: int) -> str:
    signature = api_auth._artifact_asset_token_signature("a1", "s1", "c1", expires_at, "lifetime-secret")
    return f"{expires_at}.{signature}"


def test_signed_token_parts_reject_lifetimes_past_the_ttl(monkeypatch):
    monkeypatch.setattr(api_auth.time, "time", lambda: 1_700_000_000)
    ttl = api_auth.ARTIFACT_ASSET_TOKEN_TTL_SECONDS
    latest = 1_700_000_000 + ttl + api_auth.TOKEN_CLOCK_SKEW_SECONDS
    assert api_auth._signed_token_parts(f"{latest}.sig", ttl) == (latest, "sig")
    assert api_auth._signed_token_parts(f"{latest + 1}.sig", ttl) is None
    assert api_auth._signed_token_parts("1699999999.sig", ttl) is None
    assert api_auth._signed_token_parts("not-a-number.sig", ttl) is None
    assert api_auth._signed_token_parts("1700000100", ttl) is None


def test_artifact_token_with_forged_long_lifetime_is_rejected(monkeypatch):
    monkeypatch.setenv(api_auth.RUNTIME_TOKEN_ENV, "lifetime-secret")
    monkeypatch.setattr(api_auth.time, "time", lambda: 1_700_000_000)
    minted = api_auth._build_artifact_asset_token("a1", "s1", "c1")
    assert api_auth._is_artifact_asset_token_authorized(_artifact_request(minted))
    assert not api_auth._is_artifact_asset_token_authorized(_artifact_request(_artifact_token(1_700_000_000 + 24 * 3600)))
