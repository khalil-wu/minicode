from fastapi import FastAPI

from agent.loop import run_agent_loop
from agent.real_llm import RealLLMClient
from schemas import ChatRequest, ChatResponse


app = FastAPI(title="MiniCode Agent API")
DEFAULT_REAL_LLM: RealLLMClient | None = None


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    active_real_llm = DEFAULT_REAL_LLM or RealLLMClient()
    return run_agent_loop(
        message=request.message,
        max_iterations=request.max_iterations,
        real_llm=active_real_llm,
    )
