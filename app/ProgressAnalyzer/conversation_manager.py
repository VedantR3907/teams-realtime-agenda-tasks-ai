from typing import List, Dict
from pydantic import BaseModel
from loguru import logger

class ConversationMessage(BaseModel):
    role: str  # "system", "user", or "assistant"
    content: str

def prepare_conversation_history(
    conversation_history: List[Dict[str, str]],
    transcript: str = None,
    history_depth: int = 5,
    system_prompt: str = None
) -> List[Dict[str, str]]:
    """
    Prepare history for the conversation with LLM models.
    
    Args:
        conversation_history (List[Dict[str, str]]): List of conversation messages with role and content
        transcript (str, optional): Current transcript to be processed
        history_depth (int, optional): Number of conversation turns to include. Defaults to 5.
        system_prompt (str, optional): System prompt to include at the beginning. If None, uses existing system prompt.
        
    Returns:
        List[Dict[str, str]]: List of messages in format {"role": str, "content": str}
    """
    logger.info(f"Preparing conversation history with depth {history_depth}")
    logger.debug(f"Initial history length: {len(conversation_history)}")
    
    try:
        # Extract system prompt if it exists in the history
        existing_system_prompt = None
        remaining_messages = []
        
        for msg in conversation_history:
            if msg["role"] == "system":
                existing_system_prompt = msg
            else:
                remaining_messages.append(msg)
        
        # Convert raw dictionaries to ConversationMessage objects for validation
        messages = [ConversationMessage(**msg) for msg in remaining_messages]
        logger.debug("Successfully validated all conversation messages")
        
        # Apply history depth (each conversation turn has two messages: user + assistant)
        relevant_history_count = history_depth * 2
        if len(messages) > relevant_history_count:
            logger.info(f"Trimming history to last {relevant_history_count} messages")
            messages = messages[-relevant_history_count:]
        
        # Convert back to dictionaries
        prepared_history = [
            {"role": msg.role, "content": msg.content}
            for msg in messages
        ]
        
        # Add the system prompt at the beginning
        if system_prompt:
            # Use the provided system prompt
            prepared_history.insert(0, {"role": "system", "content": system_prompt})
        elif existing_system_prompt:
            # Use the existing system prompt
            prepared_history.insert(0, existing_system_prompt)
        
        # Add the new transcript if provided
        if transcript:
            logger.debug("Adding new transcript to history")
            prepared_history.append({"role": "user", "content": transcript})
        
        logger.info(f"Successfully prepared conversation history. Final length: {len(prepared_history)}")
        return prepared_history
        
    except Exception as e:
        logger.error(f"Error preparing conversation history: {str(e)}")
        # Return a minimal valid history in case of error
        if system_prompt:
            return [{"role": "system", "content": system_prompt}]
        elif transcript:
            return [{"role": "user", "content": transcript}]
        else:
            raise
