"""Original desktop voice claim carried outside the public Source arguments."""
from __future__ import annotations

import os

import httpx
from pydantic import Field

from backend.realtime.protocol import SourceParams


class DesktopVoiceClaim(SourceParams):
    claim_id: str = Field(min_length=1)
    host_id: str = Field(min_length=1)
    capture_screen_context: bool
    transfer_voice_call: bool


class DesktopVoiceClient:
    def __init__(self, session, claim: DesktopVoiceClaim):
        self.claim = claim
        self.endpoint = os.environ["MINICODE_EMBEDDED_BROWSER_ENDPOINT"].rstrip("/")
        self.token = os.environ["MINICODE_EMBEDDED_BROWSER_TOKEN"]
        self.owner = {"window_instance_id": session.window_instance_id, "window_owner_id": session.window_owner_id,
            "window_binding_token": session._window_binding_token}
        if not self.owner["window_instance_id"] or not self.owner["window_binding_token"]:
            raise ValueError("Voice tools require their original authenticated desktop window")

    def supports(self, name):
        return self.claim.capture_screen_context if name == "capture_screen_context" else self.claim.transfer_voice_call

    async def invoke(self, *, conversation_id, name, arguments, call_id):
        # This is the authenticated internal native gateway, independent of
        # application provider routing and its external destination policy.
        async with httpx.AsyncClient(timeout=None, follow_redirects=False, trust_env=False) as client:
            response = await client.post(self.endpoint + "/v1/voice-control",
                headers={"Authorization": "Bearer " + self.token}, json={**self.owner,
                    "claim_id": self.claim.claim_id, "host_id": self.claim.host_id,
                    "conversation_id": conversation_id, "tool": name, "arguments": arguments, "call_id": call_id})
            value = response.json()
            if not response.is_success:
                raise ValueError(value["error"])
            return value["value"]
