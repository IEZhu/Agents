from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field

class AgentRequest(BaseModel):
    """
    Standardized request structure for all agents.
    """
    query: str = Field(..., description="The user's raw input text")
    context: Dict[str, Any] = Field(default_factory=dict, description="Additional context (file contents, history, etc.)")
    request_id: str = Field(..., description="Unique ID for tracing")
    user_id: Optional[str] = Field(None, description="User identifier")

class AgentResponse(BaseModel):
    """
    Standardized response structure from all agents.
    """
    content: str = Field(..., description="The main textual response")
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list, description="List of tool calls made")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Execution metadata (latency, tokens, etc.)")
    agent_name: str = Field(..., description="Name of the agent that produced the response")

class RouterDecision(BaseModel):
    """
    The decision made by the Semantic Router.
    """
    target_agent: str = Field(..., description="The name of the selected agent profile")
    confidence: float = Field(..., description="Confidence score of the routing decision (0.0 - 1.0)")
    reasoning: str = Field(..., description="Short explanation of why this agent was chosen")
    is_cached: bool = Field(default=False, description="Whether this decision came from semantic cache")
