from backend.agent.content_projection import normalise_content


def test_core_content_projection_does_not_depend_on_extension_runtime() -> None:
    assert normalise_content(
        [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]
    ) == "one\ntwo"
