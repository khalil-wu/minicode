from openai import OpenAI

from agent.settings import LLMSettings, load_llm_settings


class RealLLMClient:
    def __init__(
        self,
        settings: LLMSettings | None = None,
        openai_client: OpenAI | None = None,
    ) -> None:
        self.settings = settings or load_llm_settings()
        self.client = openai_client or OpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
        )

    def generate_reply(self, message: str) -> str:
        response = self.client.responses.create(
            model=self.settings.model,
            input=message,
            reasoning={"effort": self.settings.reasoning_effort},
        )

        text = getattr(response, "output_text", "") or ""
        text = text.strip()
        if not text:
            raise RuntimeError("Model returned empty output")
        return text
