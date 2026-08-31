from __future__ import annotations

from backend.llm.openai_adapter import _normalized_openai_base_url, _proxy_url_for_base_url


def _clear_proxy_env(monkeypatch) -> None:
    for name in (
        "LLM_PROXY_URL",
        "MINICODE_LLM_PROXY_URL",
        "OPENAI_PROXY_URL",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


def test_explicit_proxy_takes_precedence(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("LLM_PROXY_URL", "http://explicit.test:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.test:8080")

    assert _proxy_url_for_base_url("https://api.example.test/v1") == "http://explicit.test:8080"


def test_https_proxy_is_used_for_https_provider(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")

    assert _proxy_url_for_base_url("https://api.example.test/v1") == "http://127.0.0.1:7897"


def test_no_proxy_bypasses_explicit_and_environment_proxy(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("MINICODE_LLM_PROXY_URL", "http://explicit.test:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.test:8080")
    monkeypatch.setenv("NO_PROXY", ".example.test")

    assert _proxy_url_for_base_url("https://api.example.test/v1") == ""


def test_no_proxy_requires_hostname_boundary(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("NO_PROXY", "example.test")

    assert _proxy_url_for_base_url("https://notexample.test/v1") == "http://127.0.0.1:7897"


def test_direct_mode_ignores_explicit_and_environment_proxies(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("LLM_PROXY_URL", "http://explicit.test:8080")
    monkeypatch.setenv("OPENAI_PROXY_URL", "http://legacy.test:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.test:8080")

    assert _proxy_url_for_base_url(
        "https://api.example.test/v1",
        proxy_mode="direct",
    ) == ""


def test_openai_legacy_proxy_environment_is_not_an_implicit_provider_proxy(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("OPENAI_PROXY_URL", "http://legacy.test:8080")

    assert _proxy_url_for_base_url("https://api.example.test/v1") == ""


def test_no_proxy_matches_effective_default_port_and_rejects_mismatch(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.test:8080")
    monkeypatch.setenv("NO_PROXY", "api.example.test:443")

    assert _proxy_url_for_base_url("https://api.example.test/v1") == ""

    monkeypatch.setenv("NO_PROXY", "api.example.test:8443")
    assert _proxy_url_for_base_url("https://api.example.test/v1") == "http://environment.test:8080"


def test_no_proxy_matches_bracketed_ipv6_with_port(monkeypatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.test:8080")
    monkeypatch.setenv("NO_PROXY", "[2001:db8::1]:8443")

    assert _proxy_url_for_base_url("https://[2001:db8::1]:8443/v1") == ""


def test_host_only_openai_base_url_gets_v1_prefix() -> None:
    assert _normalized_openai_base_url("https://api.example.test") == "https://api.example.test/v1"


def test_explicit_openai_base_path_is_preserved() -> None:
    assert _normalized_openai_base_url("https://api.example.test/v1/") == "https://api.example.test/v1"
    assert (
        _normalized_openai_base_url("https://api.example.test/custom/openai")
        == "https://api.example.test/custom/openai"
    )


def test_explicit_root_openai_base_url_is_not_given_a_v1_prefix() -> None:
    # A trailing slash is an explicit root path, not a bare host. Appending
    # ``/v1`` here made a gateway that serves the API at its root impossible to
    # configure and silently rewrote a user-supplied base URL.
    assert _normalized_openai_base_url("https://gateway.example.test/") == "https://gateway.example.test"
    assert _normalized_openai_base_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
