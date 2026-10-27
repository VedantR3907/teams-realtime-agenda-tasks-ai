import re
import time
import json
from typing import Dict, List, Optional, Any
from pydantic import BaseModel
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from loguru import logger
from app.config import Settings
from app.ProgressAnalyzer.prompts.agenda_prompt import get_agenda_tracking_prompt
from app.ProgressAnalyzer.conversation_manager import prepare_conversation_history

settings = Settings()


class StateChange(BaseModel):
    """Model representing a single agenda item state transition"""
    agenda_item: str
    new_state: str


def _is_valid_agenda_transition(current_state: str, new_state: str) -> bool:
    """
    Validate if an agenda state transition is allowed.
    
    Valid transitions:
    - Not Started → In Progress
    - In Progress → On Hold
    - In Progress → Completed  
    - On Hold → In Progress
    - On Hold → Completed
    - Completed → In Progress (allows restarting completed items)
    
    Invalid transitions:
    - Not Started → On Hold (must go through In Progress first)
    - Not Started → Completed (must go through In Progress first)
    - Any state → same state (no change needed)
    """
    # No change needed
    if current_state == new_state:
        return False
    
    # Define valid transitions
    valid_transitions = {
        "Not Started": ["In Progress"],
        "In Progress": ["On Hold", "Completed"],  
        "On Hold": ["In Progress", "Completed"],
        "Completed": ["In Progress"]  # Allow restarting completed items
    }
    
    return new_state in valid_transitions.get(current_state, [])


class LLMProcessor:
    """
    Processes transcripts using LLM to identify agenda item state transitions,
    keeping a running chat history of user and assistant messages.
    """
    def __init__(
        self,
        agenda_items: Optional[Dict[str, str]] = None,
        agenda_list: Optional[List[str]] = None
    ):
        if agenda_items is None and agenda_list is None:
            raise ValueError("Either agenda_items or agenda_list must be provided")

        # 1) Extract titles + descriptions
        if agenda_items:
            self.agenda_titles = list(agenda_items.keys())
            self.agenda_descriptions = agenda_items
        else:
            if agenda_list is None:
                raise ValueError("agenda_list cannot be None when agenda_items is None")
            self.agenda_titles = agenda_list
            self.agenda_descriptions = {t: "" for t in agenda_list}

        # 2) Initialize states and history
        self.agenda_states = {t: "Not Started" for t in self.agenda_titles}
        self.agenda_history: List[Dict] = []

        # 3) Build system prompt + initial messages
        self.system_prompt = get_agenda_tracking_prompt(
            self.agenda_titles,
            self.agenda_descriptions,
            self.agenda_states
        )
        
        # Debug: Log agenda prompt details
        logger.info(f"🎯 AGENDA PROMPT DEBUG: Built prompt with {len(self.agenda_titles)} titles")
        logger.info(f"🎯 AGENDA PROMPT DEBUG: Prompt length = {len(self.system_prompt)} characters")
        logger.debug(f"🎯 AGENDA PROMPT DEBUG: First 500 chars: {self.system_prompt[:500]}")
        
        self.messages: List[Dict] = [{"role": "system", "content": self.system_prompt}]

        # how many user+assistant pairs to keep at most between state-changes
        self.max_history_turns = getattr(settings, "LLM_MAX_TURNS", 10)

        # 4) Dynamic provider selection
        provider = settings.MODEL_PROVIDER.lower()
        if provider == "openai":
            self.client = OpenAI(api_key=settings.OPENAI_API_KEY)
            self.model_name = settings.OPENAI_MODEL

            # JSON Schema: wrap the array in an object named "state_changes"
            item_schema = StateChange.model_json_schema()
            # **Fix: require no extra props on each item**
            item_schema["additionalProperties"] = False

            self._json_schema_payload: Dict[str, Any] = {
                "name": "state_changes",
                "schema": {
                    "type": "object",
                    "properties": {
                        "state_changes": {
                            "type": "array",
                            "items": item_schema
                        }
                    },
                    "required": ["state_changes"],
                    "additionalProperties": False
                },
                "strict": True
            }
            self._use_structured = True

        elif provider == "gemini":
            self.client = OpenAI(
                api_key=settings.GEMINI_API_KEY,
                base_url=settings.GEMINI_BASE_URL
            )
            self.model_name = settings.GEMINI_MODEL
            self._use_structured = False

        else:
            raise ValueError(f"Unsupported MODEL_PROVIDER '{settings.MODEL_PROVIDER}'")

    def process_transcript(self, transcript: str) -> List[StateChange]:
        # ——— PRE-ROLL: cap history so we never exceed max_history_turns turns ———
        max_msgs = 1 + (self.max_history_turns * 2)  # 1 for system + 2 msgs per turn
        if len(self.messages) > max_msgs:
            # keep system prompt + the last max_msgs-1 entries
            self.messages = [self.messages[0]] + self.messages[-(max_msgs - 1):]

        # now record the incoming user message
        self.messages.append({"role": "user", "content": transcript})

        try:
            logger.info(f"Calling {settings.MODEL_PROVIDER} LLM with transcript: {transcript}")
            logger.debug(f"Chat history: {json.dumps(self.messages, indent=2)}")

            # Prepare conversation history with the current transcript and system prompt
            prepared_messages = prepare_conversation_history(
                self.messages, 
                history_depth=5,
                system_prompt=self.system_prompt
            )

            # Convert messages to proper OpenAI format
            openai_messages: List[ChatCompletionMessageParam] = []
            for msg in prepared_messages:
                openai_messages.append({
                    "role": msg["role"],  # type: ignore
                    "content": msg["content"]
                })

            # Debug: Check final system prompt before LLM call
            system_msg = next((msg for msg in openai_messages if msg["role"] == "system"), None)
            if system_msg:
                content = system_msg.get("content", "")
                if isinstance(content, str):
                    logger.info(f"🎯 AGENDA PROMPT DEBUG: Final system prompt length = {len(content)} characters")
                    logger.debug(f"🎯 AGENDA PROMPT DEBUG: Final prompt preview: {content[:300]}...")
                else:
                    logger.warning("🎯 AGENDA PROMPT DEBUG: System message content is not a string!")
            else:
                logger.warning("🎯 AGENDA PROMPT DEBUG: No system message found in final messages!")

            start_time = time.time()

            logger.info("⏳ Starting Agenda LLM call...")

            # Choose structured vs JSON mode
            if self._use_structured:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=openai_messages,
                    response_format={  # type: ignore
                        "type": "json_schema",
                        "json_schema": self._json_schema_payload
                    },
                )
            else:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=openai_messages,
                    response_format={"type": "json_object"},  # type: ignore
                )

            end_time = time.time()

            logger.info(f"Execution time for Agenda LLM: {end_time - start_time:.4f} seconds")

            # Extract and validate JSON
            result_text = response.choices[0].message.content
            if not result_text:
                logger.error("LLM returned no content; skipping parse")
                return []

            # 🔍 LOG THE FULL MODEL RESPONSE
            logger.info("=" * 80)
            logger.info("🤖 AGENDA TRACKING LLM RESPONSE:")
            logger.info("=" * 80)
            logger.info(f"📄 Raw response: {result_text}")
            logger.info("=" * 80)

            try:
                # Remove any leading/trailing whitespace and potential code block markers for flash 2.5 lite model
                # result_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", result_text.strip(), flags=re.IGNORECASE)
                result_obj = json.loads(result_text)
            except json.JSONDecodeError as je:
                logger.error(f"JSON decode failed: {je}; content: {result_text!r}")
                return []

            # For structured: expect {"state_changes": [ ... ]}
            if self._use_structured:
                if not isinstance(result_obj, dict) or "state_changes" not in result_obj:
                    logger.error(f"Unexpected structured format: {result_obj!r}")
                    return []
                items = result_obj["state_changes"]
            else:
                # For Gemini: expect a top-level list
                if not isinstance(result_obj, list):
                    logger.error(f"Unexpected JSON format: {result_obj!r}")
                    return []
                items = result_obj

            # Map to StateChange models
            state_changes: List[StateChange] = []
            for ch in items:
                if not isinstance(ch, dict):
                    logger.error(f"Invalid item type: {ch!r}")
                    continue
                try:
                    state_changes.append(StateChange(**ch))
                except Exception as ex:
                    logger.error(f"Failed to parse StateChange {ch!r}: {ex}")

            # Filter by current states so only valid transitions remain
            valid_changes: List[StateChange] = []
            invalid_changes: List[StateChange] = []
            
            for ch in state_changes:
                current_state = self.agenda_states.get(ch.agenda_item, "Not Started")
                if _is_valid_agenda_transition(current_state, ch.new_state):
                    valid_changes.append(ch)
                    logger.info(f"✅ VALID transition: '{ch.agenda_item}' {current_state} → {ch.new_state}")
                else:
                    invalid_changes.append(ch)
                    logger.warning(f"❌ INVALID transition filtered: '{ch.agenda_item}' {current_state} → {ch.new_state}")

            if invalid_changes:
                logger.warning(f"🚫 Filtered {len(invalid_changes)} invalid agenda transitions, keeping {len(valid_changes)} valid ones")

            # Rewrite assistant message to include only valid transitions
            if self._use_structured:
                filtered_payload = {"state_changes": [c.model_dump() for c in valid_changes]}
            else:
                filtered_payload = [c.model_dump() for c in valid_changes]

            filtered_text = json.dumps(filtered_payload, ensure_ascii=False)

            # Append assistant response (filtered to only valid transitions)
            self.messages.append({"role": "assistant", "content": filtered_text})

            # Update states & record history ONLY for valid changes
            for ch in valid_changes:
                old = self.agenda_states.get(ch.agenda_item, "Unknown")
                self.agenda_states[ch.agenda_item] = ch.new_state
                # Use simple relative timestamp (seconds since processing start)
                rel_ts = time.time() % 86400  # Use time of day in seconds for relative timestamp
                self.agenda_history.append({
                    "agenda_item": ch.agenda_item,
                    "from_state": old,
                    "to_state": ch.new_state,
                    "timestamp": rel_ts
                })
                logger.info(
                    f"Agenda '{ch.agenda_item}' {old}→{ch.new_state} @ {rel_ts:.1f}s"
                )

            # Refresh system prompt (now reflects only valid transitions)
            self.system_prompt = get_agenda_tracking_prompt(
                self.agenda_titles,
                self.agenda_descriptions,
                self.agenda_states
            )
            
            # Debug: Log refreshed prompt details
            logger.info(f"🎯 AGENDA PROMPT DEBUG: Refreshed prompt length = {len(self.system_prompt)} characters")
            
            self.messages[0]["content"] = self.system_prompt

            # Return only valid changes to the coordinator
            return valid_changes

        except Exception as e:
            logger.error(f"Error during LLM call: {e}")
            return [] 