from pathlib import Path


def test_runtime_shell_css_defines_semantic_tokens() -> None:
    source = Path("frontend/src.v2/styles/tokens.css").read_text(encoding="utf-8")

    assert "--surface-base:" in source
    assert "--surface-panel:" in source
    assert "--accent-strong:" in source
    assert "--state-danger:" in source
