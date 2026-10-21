"""Public codex-prompts realtime fragments from the frozen Source checkout."""
from pathlib import Path
import getpass

BACKEND_PROMPT = (Path(__file__).parents[1] / "agent" / "codex_prompt_resources" / "realtime-backend.md").read_text(encoding="utf-8").rstrip()


def backend_prompt(start, config):
    if config.prompt.strip():
        return config.prompt
    if "prompt" in start.model_fields_set:
        return start.prompt or ""
    name = getpass.getuser().split()[0]
    return BACKEND_PROMPT.replace("{{ user_first_name }}", name)

START_INSTRUCTIONS = """Realtime conversation started.

You are operating as a backend executor behind an intermediary. The user does not talk to you directly. Any response you produce will be consumed by the intermediary and may be summarized before the user sees it.

When invoked, you receive the latest conversation transcript and any relevant mode or metadata. The intermediary may invoke you even when backend help is not actually needed. Use the transcript to decide whether you should do work. If backend help is unnecessary, avoid verbose responses that add user-visible latency.

When user text is routed from realtime, treat it as a transcript. It may be unpunctuated or contain recognition errors.

- Keep responses concise and action-oriented. Your updates should help the intermediary respond to the user."""
END_INSTRUCTIONS = """Realtime conversation ended.

Subsequent user input will return to typed text rather than transcript-style text. Do not assume recognition errors or missing punctuation once realtime has ended. Resume normal chat behavior."""


def contextual_fragment(active, service):
    if active:
        custom = service.params.realtime_start_instructions
        if custom is None:
            custom = service.config.start_instructions
        return (custom if custom is not None else START_INSTRUCTIONS,
            "realtime_conversation.custom_start_instructions" if custom is not None else "realtime_conversation.start_instructions")
    custom = service.params.realtime_end_instructions if service is not None else None
    return custom if custom is not None else END_INSTRUCTIONS, "realtime_conversation.end_instructions"
