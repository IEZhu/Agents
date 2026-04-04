import uuid
import dotenv
import asyncio
from typing import List, Optional
from src.utils.langfuse_compat import observe, get_langfuse

from src.schemas.protocol import AgentRequest, AgentResponse
from src.engine.router import SemanticRouter
from src.utils.prompt_loader import load_agent_prompt
from src.engine.implants import ImplantRetriever
from src.engine.context import ContextRetriever

# Load environment variables
dotenv.load_dotenv()

langfuse = get_langfuse()

class AgentSystem:
    def __init__(self):
        self.router = SemanticRouter()
        self.implant_retriever = ImplantRetriever()
        self.context_retriever = ContextRetriever()

    @observe(name="process_request")
    async def process_request(self, user_query: str, user_id: str = "default_user", history: Optional[List[str]] = None) -> AgentResponse:
        if history is None:
            history = []
        request_id = str(uuid.uuid4())

        # 1. Retrieve Context (Langfuse Step 1)
        ctx = self.context_retriever.retrieve(user_query, history=history)

        request = AgentRequest(
            query=user_query,
            context=ctx,
            request_id=request_id,
            user_id=user_id
        )

        # 2. Route (Langfuse Step 2 - Uses Context)
        decision = await self.router.route(request)

        # 3. Load Agent Prompt
        try:
            system_prompt = load_agent_prompt(decision.target_agent)
        except Exception as e:
            # Fallback
            system_prompt = "You are a helpful assistant."

        # 4. Load Relevant Implants (Langfuse Step 3 - Uses Context + Role)
        implants = self.implant_retriever.retrieve(
            user_query,
            n_results=3,
            context=ctx,
            role=decision.target_agent
        )
        formatted_implants = self.implant_retriever.format_implants_for_prompt(implants)

        if formatted_implants:
            system_prompt += "\n\n" + formatted_implants

        # 5. Return enriched prompt (LLM execution is handled by the MCP client)
        return AgentResponse(
            content=system_prompt,
            agent_name=decision.target_agent,
            metadata={
                "router_decision": decision.model_dump(),
                "request_id": request_id,
                "implants_loaded": [i['metadata']['filename'] for i in implants],
                "context_retrieved": True
            }
        )

if __name__ == "__main__":
    # Simple CLI test
    import sys
    try:
        if len(sys.argv) > 1:
            query = sys.argv[1]
            system = AgentSystem()
            response = asyncio.run(system.process_request(query))
            print(f"Agent: {response.agent_name}")
            print(f"Response: {response.content}")
            print(f"Implants: {response.metadata.get('implants_loaded')}")
        else:
            print("Usage: python src/main.py 'Your query here'")
    finally:
        # Ensure events are sent before exit
        langfuse.flush()
