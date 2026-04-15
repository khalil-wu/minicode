from fastapi import FastAPI

from agent.loop import run_agent_loop
from schemas import ChatRequest, ChatResponse


app = FastAPI(title="MiniCode Agent API")


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    return run_agent_loop(
        message=request.message,
        max_iterations=request.max_iterations,
    )
