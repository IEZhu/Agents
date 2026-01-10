# Request Routing Flow

This diagram illustrates the decision-making process for routing user requests to the appropriate agent.

## Routing Flow Sequence

```mermaid
sequenceDiagram
    participant User
    participant Router as get_routing_info
    participant Cache as Semantic Cache
    participant Meta as Meta-Query Detector
    participant Cursor as Cursor Model
    participant Context as get_agent_context
    participant Session as Session Cache
    participant Enrich as Enrichment Pipeline
    
    User->>Router: User Request
    Router->>Cache: Lookup Query
    
    alt Cache Hit (Distance < 0.05)
        Cache-->>Router: Agent Found
        Router->>Context: Load Agent Context
    else Cache Miss
        Cache-->>Router: No Match
        Router->>Meta: Check Query Type
        
        alt Meta-Query (< 10 chars or greeting)
            Meta-->>Router: Auto-route to universal_agent
            Router->>Context: Load universal_agent
        else Standard Query
            Meta-->>Router: Return Agent List
            Router->>Cursor: Select Best Agent
            Cursor-->>Router: Agent Selected
            Router->>Context: Load Agent Context
        end
    end
    
    Context->>Session: Check Session Cache
    
    alt Session Cache Hit
        Session-->>Context: Return Cached Prompt
        Context-->>User: System Prompt Ready
    else Session Cache Miss
        Session-->>Context: Not Found
        Context->>Enrich: Start Enrichment
        
        Enrich->>Enrich: Load Base Prompt
        Enrich->>Enrich: Retrieve Skills
        Enrich->>Enrich: Retrieve Implants (Top 3)
        Enrich->>Enrich: Detect Language
        
        alt Language Detected
            Enrich->>Enrich: Inject Language Directive
        end
        
        Enrich->>Enrich: Combine All Components
        Enrich->>Session: Update Session Cache
        Enrich->>Cache: Update Semantic Cache (if confidence > 0.8)
        Enrich-->>User: System Prompt Ready
    end
```

## Key Components

1.  **Semantic Cache**: Uses ChromaDB to store and retrieve previous routing decisions. A match occurs if the cosine distance is less than 0.05 (Similarity > 0.95).
2.  **Meta-Query Detection**: Automatically routes short queries (< 10 chars) or common greetings to `universal_agent` to reduce latency and model overhead.
3.  **Cursor Fallback**: If no cache hit and not a meta-query, the system returns the list of agents, allowing the Cursor model to make a semantic decision based on the query.
4.  **Enrichment Pipeline**:
    *   **Skills**: Retrieves relevant reusable skills (e.g., `git_operations`).
    *   **Implants**: Injects up to 3 cognitive implants (mental models) based on the query.
    *   **Language**: Detects the user's language and injects a critical instruction to respond in that language.
5.  **Session Cache**: Stores enriched prompts keyed by agent name and query hash to speed up repeated requests within the same session.
