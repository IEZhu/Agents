import logging
from typing import List, Dict, Any, Optional
from langfuse import observe

logger = logging.getLogger(__name__)

class ContextRetriever:
    """
    Retrieves and formats context for the conversation (history, memories, etc.).
    """
    
    def __init__(self):
        # Lazy import to avoid circular dependencies
        from src.engine.language import get_detector
        self.language_detector = get_detector()

    @observe(name="retrieve_context")
    def retrieve(self, query: str, history: List[str] = []) -> Dict[str, Any]:
        """
        Retrieves context based on the query and history.
        Current implementation focuses on formatting the history and detecting language.
        """
        
        # 1. Format History
        formatted_history = "\n".join(history) if history else ""
        
        # 2. Detect Language
        detected_language = self.language_detector.detect(query)
        logger.info(f"Detected language for query: {detected_language}")
        
        # Placeholder for future RAG/Memory retrieval
        # relevant_memories = memory_store.search(query)
        
        context_data = {
            "history_text": formatted_history,
            "history_list": history,
            "detected_language": detected_language,
            # "relevant_docs": [] 
        }
        
        logger.info(f"Context retrieved. History length: {len(history)}, Language: {detected_language}")
        
        return context_data
