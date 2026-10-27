import json
import re
import time
from typing import List, Optional, Dict, Any, cast
from pydantic import BaseModel
from openai import OpenAI
from loguru import logger
from app.config import Settings
from app.ProgressAnalyzer.prompts.dai_prompt import build_progress_prompt

settings = Settings()

class ProgressItem(BaseModel):
    id: Optional[str]
    progress_type: str
    description: Optional[str]
    assignee: Optional[str]
    due_date_iso: Optional[str]
    due_text_original: Optional[str]
    status: Optional[str] = None

class ProgressLLMProcessor:
    """Processor that uses :func:`build_progress_prompt` to extract progress items
    and manages a small chat history similar to ``new_features_test.py``.
    """

    def __init__(self, agendas: List[str], previous_meeting_items: Optional[List[Dict[str, Any]]] = None, meeting_participants: Optional[List[str]] = None, agenda_status: Optional[Dict[str, str]] = None):
        self.agendas = agendas
        self.items_missing_details: List[Dict[str, Any]] = []
        self.completed_items: List[Dict[str, Any]] = []
        self.previous_meeting_items: List[Dict[str, Any]] = previous_meeting_items or []
        self.meeting_participants: List[str] = meeting_participants or []
        self.agenda_status: Dict[str, str] = agenda_status or {}
        
        # Chat history with proper user/assistant messages instead of raw snippets
        self.messages: List[Dict[str, str]] = []
        self.max_history_turns: int = 5  # Max 5 conversation turns (user+assistant pairs)
        
        logger.info(f"✅ ProgressLLMProcessor initialized with {len(self.previous_meeting_items)} previous meeting items")
        if self.previous_meeting_items:
            logger.info("📋 Previous meeting items:")
            for item in self.previous_meeting_items:
                logger.info(f"   └─ {item.get('id')}: {item.get('progress_type')} - {item.get('description')}")
        else:
            logger.info("ℹ️ No previous meeting items found")
            
        if self.agenda_status:
            logger.info(f"🎯 Agenda status context provided for {len(self.agenda_status)} items:")
            for agenda, status in self.agenda_status.items():
                logger.info(f"   └─ {agenda}: {status}")
        else:
            logger.info("ℹ️ No agenda status information provided")

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

    def process_transcript(self, transcript: str) -> Optional[ProgressItem]:
        """Process a snippet of conversation and return a :class:`ProgressItem`.

        The processor keeps a sliding window of recent user/assistant conversation turns
        and flushes the history shortly after a progress item is completed.
        """

        # 1. Cap history to max turns before adding new message
        max_msgs = self.max_history_turns * 2  # 2 messages per turn (user + assistant)
        if len(self.messages) > max_msgs:
            # Keep only the last max_msgs messages
            self.messages = self.messages[-max_msgs:]

        logger.info(f"🔍 Building progress prompt with:")
        logger.info(f"   └─ {len(self.items_missing_details)} items missing details")
        logger.info(f"   └─ {len(self.completed_items)} completed items") 
        logger.info(f"   └─ {len(self.messages)//2} conversation turns in history")
        logger.info(f"   └─ {len(self.meeting_participants)} meeting participants for name correction")
        logger.info(f"   └─ {len(self.agenda_status)} agenda items with status context")
        
        # 2. Build system prompt with current state
        system_prompt = build_progress_prompt(
            self.items_missing_details,
            self.completed_items,
            self.agendas,
            self.previous_meeting_items,
            self.meeting_participants,
            self.agenda_status,
        )

        # 3. Build messages: system + chat history + current user message
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self.messages)  # Add conversation history
        messages.append({"role": "user", "content": transcript})  # Add current transcript

        # Debug: Log conversation history being sent
        logger.info(f"📤 Sending to Progress LLM:")
        logger.info(f"   └─ Chat history: {len(self.messages)} messages ({len(self.messages)//2} turns)")
        logger.info(f"   └─ Current input: {transcript}")
        if self.messages:
            logger.info(f"   └─ Recent history preview:")
            for i, msg in enumerate(self.messages[-4:]):  # Show last 2 turns
                role_emoji = "👤" if msg["role"] == "user" else "🤖"
                content_preview = msg["content"][:100] + "..." if len(msg["content"]) > 100 else msg["content"]
                logger.info(f"      {role_emoji} {msg['role']}: {content_preview}")

        start_time = time.time()

        logger.info("⏳ Starting Progress LLM call...")

        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=cast(Any, messages),
                response_format={"type": "json_object"},
            )
        except Exception as e:
            logger.error(f"Progress LLM call failed: {e}")
            return None

        end_time = time.time()

        logger.info(f"Execution time for Progress LLM: {end_time - start_time:.4f} seconds")

        result_text = response.choices[0].message.content
        if not result_text:
            logger.error("Progress LLM returned empty content")
            return None

        # 🔍 LOG THE FULL MODEL RESPONSE
        logger.info("=" * 80)
        logger.info("🤖 PROGRESS ITEMS LLM RESPONSE:")
        logger.info("=" * 80)
        logger.info(f"📄 Raw response: {result_text}")
        logger.info("=" * 80)

        # 4. Add the conversation turn to history BEFORE processing results
        self.messages.append({"role": "user", "content": transcript})
        self.messages.append({"role": "assistant", "content": result_text})

        try:
            # Remove any leading/trailing whitespace and potential code block markers for flash 2.5 lite model
            # result_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", result_text.strip(), flags=re.IGNORECASE)
            result_obj = json.loads(result_text)
            # If the LLM returns a list, process each item
            if isinstance(result_obj, list):
                results = result_obj
            else:
                results = [result_obj]
        except json.JSONDecodeError as je:
            logger.error(f"JSON decode failed: {je}; content: {result_text!r}")
            return None

        # Update items_missing_details and completed_items for all items in the result
        for item_dict in results:
            try:
                item = ProgressItem(**item_dict)
            except Exception as ex:
                logger.error(f"Failed to parse ProgressItem: {ex}")
                continue

            if item.id:
                has_assignee = bool(item.assignee)
                has_due_date = bool(item.due_date_iso)
                is_complete = has_assignee and has_due_date

                if not is_complete:
                    updated = False
                    for i, existing in enumerate(self.items_missing_details):
                        if existing.get("id") == item.id:
                            self.items_missing_details[i] = item.model_dump()
                            updated = True
                            break
                    if not updated:
                        self.items_missing_details.append(item.model_dump())
                else:
                    self.items_missing_details = [p for p in self.items_missing_details if p.get("id") != item.id]
                    updated = False
                    for i, existing in enumerate(self.completed_items):
                        if existing.get("id") == item.id:
                            self.completed_items[i] = item.model_dump()
                            updated = True
                            break
                    if not updated:
                        self.completed_items.append(item.model_dump())

        # Return the first item as the main result (for compatibility)
        if results:
            try:
                return ProgressItem(**results[0])
            except Exception as ex:
                logger.error(f"Failed to parse ProgressItem for return: {ex}")
                return None
        else:
            return None 

    def prune_history_for_item(self, item_id: str) -> None:
        """Prune conversation history for a specific progress item id.

        - If an assistant message JSON contains only this item, remove the assistant message and its preceding user message.
        - If an assistant message JSON contains multiple items, remove only the matching item and keep the rest.
        """
        if not item_id:
            return
        try:
            logger.info(f"🔍 Starting prune for item_id='{item_id}'")
            new_messages: List[Dict[str, str]] = []
            i = 0
            while i < len(self.messages):
                msg = self.messages[i]
                # Only examine assistant messages
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    removed_pair = False
                    try:
                        obj = json.loads(content)
                    except Exception:
                        # Not JSON; keep as is
                        new_messages.append(msg)
                        i += 1
                        continue

                    # Normalize to list
                    items = obj if isinstance(obj, list) else [obj]
                    # Filter out the matching item id
                    kept = []
                    for it in items:
                        try:
                            if isinstance(it, dict) and it.get("id") == item_id:
                                continue
                        except Exception:
                            pass
                        kept.append(it)

                    if not kept:
                        # Entire assistant output was for this item → remove assistant and prior user if present
                        # Check previous message is user
                        if new_messages and new_messages[-1].get("role") == "user":
                            logger.info("🗑️ Removing user+assistant pair due to single-item assistant message")
                            logger.debug(f"   └─ Removed user message: {new_messages[-1].get('content','')[:120]}")
                            new_messages.pop()
                        logger.debug(f"   └─ Removed assistant message items: {json.dumps(obj, ensure_ascii=False)[:200]}")
                        removed_pair = True
                    else:
                        # Keep assistant message but rewrite content to remaining items
                        try:
                            # If original was a single object and more than one kept, keep array; if one kept, keep single object
                            if isinstance(obj, list):
                                new_content = json.dumps(kept, ensure_ascii=False)
                            else:
                                # original single → if kept>1 store as array, if kept==1 store the single
                                new_content = json.dumps(kept[0] if len(kept) == 1 else kept, ensure_ascii=False)
                            logger.info("✂️ Edited assistant message to remove item while keeping others")
                            logger.debug(f"   └─ New assistant content: {new_content[:200]}")
                            new_messages.append({"role": "assistant", "content": new_content})
                        except Exception:
                            # On any failure, keep original content
                            new_messages.append(msg)

                    if removed_pair:
                        # Skip adding this assistant message
                        i += 1
                        continue
                else:
                    # Non-assistant message
                    new_messages.append(msg)
                i += 1

            logger.info("🧾 Conversation history before prune (last 10):")
            for m in self.messages[-10:]:
                logger.debug(f"   {m.get('role')}: {m.get('content','')[:200]}")

            self.messages = new_messages

            logger.info("🧾 Conversation history after prune (last 10):")
            for m in self.messages[-10:]:
                logger.debug(f"   {m.get('role')}: {m.get('content','')[:200]}")
            # Also purge item from state lists just in case
            self.items_missing_details = [p for p in self.items_missing_details if p.get("id") != item_id]
            self.completed_items = [p for p in self.completed_items if p.get("id") != item_id]
            logger.info(f"🧹 Pruned conversation history for progress item '{item_id}'")
        except Exception as e:
            logger.error(f"Failed to prune history for item '{item_id}': {e}")