import json
from typing import List, Dict, Any, Optional, cast
from openai import OpenAI
from loguru import logger
from app.config import Settings
from app.ProgressAnalyzer.prompts.name_correction_prompt import build_name_correction_prompt

settings = Settings()

class NameCorrectionProcessor:
    """Processor that uses name correction prompt to standardize assignee names
    in completed progress items against meeting participants list.
    """

    def __init__(self, meeting_participants: List[str]):
        self.meeting_participants = meeting_participants
        
        provider = settings.MODEL_PROVIDER.lower()
        if provider == "openai":
            self.client = OpenAI(api_key=settings.OPENAI_API_KEY)
            self.model_name = settings.OPENAI_MODEL
        elif provider == "gemini":
            self.client = OpenAI(
                api_key=settings.GEMINI_API_KEY,
                base_url=settings.GEMINI_BASE_URL,
            )
            self.model_name = settings.GEMINI_MODEL
        else:
            raise ValueError(f"Unsupported MODEL_PROVIDER '{settings.MODEL_PROVIDER}'")

    def correct_names(self, completed_items: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        """Correct assignee names in completed items using the name correction prompt.
        
        Args:
            completed_items: List of completed progress items that need name correction
            
        Returns:
            List of corrected progress items, or None if correction failed
        """
        if not completed_items:
            return completed_items
            
        # Filter only items that have assignees to correct
        items_with_assignees = [item for item in completed_items if item.get('assignee')]
        
        if not items_with_assignees:
            return completed_items
            
        system_prompt = build_name_correction_prompt(self.meeting_participants)
        user_content = json.dumps(items_with_assignees, indent=2)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=cast(Any, messages),
                response_format={"type": "json_object"},
            )
            
        except Exception as e:
            logger.error(f"Name correction LLM call failed: {e}")
            return None

        result_text = response.choices[0].message.content
        if not result_text:
            logger.error("Name correction LLM returned empty content")
            return None

        # 🔍 LOG THE FULL MODEL RESPONSE
        logger.info("=" * 80)
        logger.info("🤖 NAME CORRECTION LLM RESPONSE:")
        logger.info("=" * 80)
        logger.info(f"📄 Raw response: {result_text}")
        logger.info("=" * 80)

        try:
            corrected_items = json.loads(result_text)
            if not isinstance(corrected_items, list):
                logger.error("Name correction LLM should return a list")
                return None
                
        except json.JSONDecodeError as je:
            logger.error(f"Name correction JSON decode failed: {je}; content: {result_text!r}")
            return None

        # Create a mapping of corrected items by ID
        corrected_by_id = {item.get('id'): item for item in corrected_items if item.get('id')}
        
        # Apply corrections to the original completed_items list
        result_items = []
        for item in completed_items:
            item_id = item.get('id')
            if item_id in corrected_by_id:
                result_items.append(corrected_by_id[item_id])
            else:
                result_items.append(item)

        return result_items 