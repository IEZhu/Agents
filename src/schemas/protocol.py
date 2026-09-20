from typing import Dict, Any, Optional, List, Literal
from pydantic import BaseModel, Field, ConfigDict

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


PersonaAction = Literal["keep", "switch", "refresh", "restore"]


class PersonaDescriptor(BaseModel):
    """Client-owned activation, not a session identifier or authorization token."""

    model_config = ConfigDict(extra="forbid")
    agent: str = Field(pattern=r"^[a-z0-9][a-z0-9_]*$")
    activation_id: str = Field(min_length=1, max_length=128)
    bundle_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope: str = Field(min_length=1)
    skills_loaded: List[str]
    implants_loaded: List[str]
    rules_loaded: List[str]


class PersonaResponse(BaseModel):
    """Version 2 never samples; only SUCCESS contains a complete instruction bundle."""

    protocol_version: Literal[2] = 2
    status: Literal["SUCCESS", "NO_CHANGE", "ROUTE_REQUIRED", "ERROR"]
    request_id: str
    persona: Optional[PersonaDescriptor] = None
    replaces_activation_id: Optional[str] = None
    footer: Optional[str] = None
    instruction: Optional[str] = None
    persona_block: Optional[str] = None
    rules_block: Optional[str] = None
    skills_block: Optional[str] = None
    implants_block: Optional[str] = None
    candidates: Optional[List[Dict[str, Any]]] = None
    message: Optional[str] = None

    def to_json(self) -> str:
        # Null replacement explicitly identifies initial activation.
        payload = self.model_dump(exclude_none=True)
        payload["replaces_activation_id"] = self.replaces_activation_id
        import json
        return json.dumps(payload, ensure_ascii=False)
