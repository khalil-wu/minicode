from dataclasses import dataclass
import os


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str


def load_llm_settings() -> LLMSettings:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-5.4").strip()
    reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "high").strip()

    if not api_key:
        raise SettingsError("Missing OPENAI_API_KEY")

    return LLMSettings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        reasoning_effort=reasoning_effort,
    )
