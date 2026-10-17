"""Exercise the real pinned Source meter through Python's production consumers."""
from __future__ import annotations

import base64
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import subprocess
import sys
from threading import Event

from PIL import Image
import pytest

import backend
from backend.mcp import result_projection
from backend.environments.errors import ExecutorRpcError
from backend.mcp.result_projection import (
    budget_mcp_output,
    estimate_audio_tokens,
    estimate_original_image_bytes,
    mcp_output_visible_bytes,
)


FIXTURES = Path(__file__).with_name("fixtures") / "source_media"


def audio_url(filename: str, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode((FIXTURES / filename).read_bytes()).decode("ascii")


def image_url(format: str, *, width: int = 65, height: int = 33) -> str:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (3, 9, 15)).save(output, format=format)
    return f"DATA:IMAGE/{format};BASE64," + base64.b64encode(output.getvalue()).decode("ascii")


@pytest.mark.parametrize(("filename", "mime", "expected"), [
    ("silence.wav", "audio/wav", 20),
    ("silence.mp3", "audio/mpeg", 20),
    ("silence.m4a", "audio/mp4", 22),
    ("silence.ogg", "audio/ogg", 20),
    ("silence.webm", "audio/webm", 21),
])
def test_real_containers_have_duration_budgets_instead_of_base64_size(filename, mime, expected):
    url = audio_url(filename, mime)
    # Goldens are pinned Source results, including its container-timing choice.
    assert estimate_audio_tokens(url) == expected
    assert mcp_output_visible_bytes([{"type": "input_audio", "audio_url": url}]) == expected * 4


def test_live_audio_without_metadata_uses_source_url_budget_without_decoding_packets():
    url = audio_url("silence-live.webm", "audio/webm")
    expected = (len(url.encode("utf-8")) + 3) // 4
    assert expected > 900
    assert estimate_audio_tokens(url) == expected
    output = budget_mcp_output({"content": [{"type": "audio", "data": url, "mimeType": "audio/webm"}]},
        wall_time=0, policy={"mode": "tokens", "limit": 50}, output_token_limit=None,
        original_image_detail_supported=False)
    assert output[-1] == {"type": "input_text", "text": "[omitted 1 audio items ...]"}
    assert not any(item["type"] == "input_audio" for item in output)


@pytest.mark.parametrize("url", [
    "data:audio/wav;base64,%%%",
    "data:audio/flac;base64,YXVkaW8=",
    "data:audio/ogg,not-base64",
    "https://example.test/声.mp3",
])
def test_invalid_or_unsupported_audio_uses_source_utf8_url_budget(url):
    assert estimate_audio_tokens(url) == (len(url.encode("utf-8")) + 3) // 4


@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF", "WEBP"])
def test_original_image_uses_real_source_decoders_and_patch_grid(format):
    url = image_url(format)
    assert estimate_original_image_bytes(url) == 24
    assert mcp_output_visible_bytes([{"type": "input_image", "image_url": url, "detail": "original"}]) == 24
    assert mcp_output_visible_bytes([{"type": "input_image", "image_url": url, "detail": "high"}]) == 7373


def test_original_image_caps_actual_decoded_patch_count():
    assert estimate_original_image_bytes(image_url("PNG", width=3201, height=3201)) == 40000


def test_original_image_needs_decodable_pixels_and_an_image_media_type():
    header = base64.b64decode(image_url("PNG").split(",", 1)[1])[:33]
    broken = "data:image/png;base64," + base64.b64encode(header).decode("ascii")
    for url in [broken, audio_url("silence.wav", "audio/wav"), "data:image/png;base64,%%%"]:
        assert estimate_original_image_bytes(url) is None
        assert mcp_output_visible_bytes([{"type": "input_image", "image_url": url, "detail": "original"}]) == 7373


def test_native_execution_failure_is_not_replaced_with_an_unknown_media_estimate(monkeypatch):
    def reject(self, method, params):
        raise ExecutorRpcError(-32000, "Actual native media request failed")

    monkeypatch.setattr("backend.network.native.NativeNetworkRuntime.invoke_sync", reject)
    with pytest.raises(ExecutorRpcError, match="Actual native media request failed"):
        estimate_audio_tokens("data:audio/wav;base64,bmF0aXZlLWZhaWx1cmU=")


def test_real_ws_and_media_process_exits_cleanly_without_loading_pyav(tmp_path):
    root = str(Path(backend.__file__).resolve().parent.parent)
    script = f"""import base64, json, pathlib, sys
sys.path.insert(0, {root!r})
import backend.ws.handler
from backend.mcp.result_projection import estimate_audio_tokens, mcp_output_visible_bytes
data = pathlib.Path({str(FIXTURES / 'silence.wav')!r}).read_bytes()
url = 'data:audio/wav;base64,' + base64.b64encode(data).decode('ascii')
assert estimate_audio_tokens(url) == 20
assert mcp_output_visible_bytes([{{'type':'input_audio','audio_url':url}}]) == 80
assert not any(name == 'av' or name.startswith('av.') for name in sys.modules)
print(json.dumps({{'tokens':20,'bytes':80,'pyav_loaded':False}}))
"""
    process = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", script], cwd=tmp_path,
        capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {"tokens": 20, "bytes": 80, "pyav_loaded": False}


@pytest.fixture
def isolated_media_caches(monkeypatch):
    monkeypatch.setattr(result_projection, "_audio_estimates", OrderedDict())
    monkeypatch.setattr(result_projection, "_image_estimates", OrderedDict())


@pytest.mark.parametrize("modality", ["audio", "image"])
def test_media_estimate_same_key_initializes_once(monkeypatch, isolated_media_caches, modality):
    estimate = estimate_audio_tokens if modality == "audio" else estimate_original_image_bytes
    expected = 17 if modality == "audio" else None
    entered, release, second_started = Event(), Event(), Event()
    calls = []

    def invoke(self, method, params):
        calls.append((method, params))
        entered.set()
        assert release.wait(5)
        return {"tokens": 17, "bytes": None}

    def second_call():
        second_started.set()
        return estimate("cache://same-key")

    monkeypatch.setattr("backend.network.native.NativeNetworkRuntime.invoke_sync", invoke)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(estimate, "cache://same-key")
        try:
            assert entered.wait(5)
            second = workers.submit(second_call)
            assert second_started.wait(5)
        finally:
            release.set()
        assert first.result(timeout=5) == expected
        assert second.result(timeout=5) == expected
    assert estimate("cache://same-key") == expected
    assert len(calls) == 1


@pytest.mark.parametrize("modality", ["audio", "image"])
def test_media_estimate_different_keys_initialize_concurrently(monkeypatch, isolated_media_caches, modality):
    estimate = estimate_audio_tokens if modality == "audio" else estimate_original_image_bytes
    expected = 17 if modality == "audio" else None
    first_entered, second_entered, release = Event(), Event(), Event()

    def invoke(self, method, params):
        url = params["audioUrl" if modality == "audio" else "imageUrl"]
        (first_entered if url == "cache://first-key" else second_entered).set()
        assert release.wait(5)
        return {"tokens": 17, "bytes": None}

    monkeypatch.setattr("backend.network.native.NativeNetworkRuntime.invoke_sync", invoke)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(estimate, "cache://first-key")
        try:
            assert first_entered.wait(5)
            second = workers.submit(estimate, "cache://second-key")
            assert second_entered.wait(5)
        finally:
            release.set()
        assert first.result(timeout=5) == expected
        assert second.result(timeout=5) == expected


@pytest.mark.parametrize("modality", ["audio", "image"])
def test_media_estimate_failed_initialization_propagates_without_caching_a_value(monkeypatch, isolated_media_caches, modality):
    estimate = estimate_audio_tokens if modality == "audio" else estimate_original_image_bytes
    expected = 17 if modality == "audio" else None
    failure = ExecutorRpcError(-32000, "Actual native initialization failed")
    calls = []

    def invoke(self, method, params):
        calls.append((method, params))
        if len(calls) == 1:
            raise failure
        return {"tokens": 17, "bytes": None}

    monkeypatch.setattr("backend.network.native.NativeNetworkRuntime.invoke_sync", invoke)
    with pytest.raises(ExecutorRpcError) as raised:
        estimate("cache://failed-initialization")
    assert raised.value is failure
    assert len(calls) == 1
    assert estimate("cache://failed-initialization") == expected
    assert estimate("cache://failed-initialization") == expected
    assert len(calls) == 2


@pytest.mark.parametrize("modality", ["audio", "image"])
def test_media_estimate_keeps_32_digest_entries_with_lru_recency(monkeypatch, isolated_media_caches, modality):
    estimate = estimate_audio_tokens if modality == "audio" else estimate_original_image_bytes
    calls = []

    def invoke(self, method, params):
        calls.append(params["audioUrl" if modality == "audio" else "imageUrl"])
        return {"tokens": 17, "bytes": None}

    monkeypatch.setattr("backend.network.native.NativeNetworkRuntime.invoke_sync", invoke)
    urls = [f"cache://{modality}/private-url-{index}" for index in range(33)]
    for url in urls[:32]:
        estimate(url)
    estimate(urls[0])
    estimate(urls[32])
    estimate(urls[0])
    assert calls == [*urls[:32], urls[32]]
    estimate(urls[1])
    assert calls[-1] == urls[1]
    assert len(calls) == 34
    cache = getattr(result_projection, f"_{modality}_estimates")
    assert len(cache) == 32
    assert all(isinstance(key, bytes) and len(key) == 20 for key in cache)
