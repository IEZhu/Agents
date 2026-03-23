import os
import sys
import uuid
import logging
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.engine.router import SemanticRouter
from src.engine.enrichment import enrich_agent_prompt
from src.utils.prompt_loader import load_agent_prompt, get_agent_metadata
from src.schemas.protocol import AgentRequest

env_path = os.path.join(os.path.dirname(__file__), "../.env")
dotenv.load_dotenv(env_path)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("http-bridge")

app = FastAPI()
router = SemanticRouter()

class ChatRequest(BaseModel):
    query: str
    history: List[str] = []

class ChatResponse(BaseModel):
    agent: str
    response: str
    reasoning: Optional[str] = None

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    query = request.query
    logger.info(f"Received query: {query}")

    try:
        agent_request = AgentRequest(
            query=query,
            context={"history_text": "\n".join(request.history)},
            request_id=str(uuid.uuid4()),
        )
        decision = await router.route(agent_request)

        target_agent = decision.target_agent
        reasoning = decision.reasoning

        # 2. Load Context
        base_prompt = load_agent_prompt(target_agent)
        metadata = get_agent_metadata(target_agent)
        preferred_skills = metadata.get("preferred_skills", [])

        final_system_prompt = await enrich_agent_prompt(
            target_agent,
            base_prompt,
            query,
            request.history,
            preferred_skills
        )

        # 3. Return enriched prompt (LLM execution is handled by the MCP client)
        logger.info(f"Routed to agent: {target_agent}")

        return ChatResponse(
            agent=target_agent,
            response=final_system_prompt,
            reasoning=reasoning
        )

    except Exception as e:
        logger.error(f"Error processing request: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
