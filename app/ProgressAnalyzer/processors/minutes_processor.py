import json
import os
from typing import Dict, Optional, Any
from openai import OpenAI
from loguru import logger
from app.config import Settings
from app.ProgressAnalyzer.prompts.minutes_summarizer_prompt import get_minutes_summarizer_prompt

settings = Settings()


class MinutesProcessor:
    """
    Processes complete meeting transcripts to generate structured meeting minutes.
    This processor runs after the meeting ends, not in real-time.
    """
    
    def __init__(self, json_transcript_path: Optional[str] = None):
        """
        Initialize the MinutesProcessor.
        
        Args:
            json_transcript_path: Path to the JSON transcript file (uses settings if not provided)
        """
        if json_transcript_path is None:
            # Use settings-based output directory
            output_dir = os.path.dirname(settings.TRANSCRIPT_FILE_PATH) or "output"
            self.json_transcript_path = os.path.join(output_dir, "meeting_transcript.json")
        else:
            self.json_transcript_path = json_transcript_path
        
        # Initialize OpenAI client with settings from config
        self.client = OpenAI(
            api_key=settings.GEMINI_API_KEY,
            base_url=settings.GEMINI_BASE_URL
        )
        
        # Store model configuration
        self.model_name = settings.GEMINI_MINUTES_MODEL
        self.model_provider = settings.MODEL_PROVIDER
        logger.info(f"MinutesProcessor initialized with transcript path: {json_transcript_path}")
    
    def load_transcript_data(self) -> Dict[str, Any]:
        """
        Load the complete transcript data from the JSON file.
        
        Returns:
            Dict containing meeting info and transcripts
        """
        try:
            if not os.path.exists(self.json_transcript_path):
                logger.error(f"Transcript file not found: {self.json_transcript_path}")
                return {"meeting_info": {}, "transcripts": []}
            
            with open(self.json_transcript_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                logger.info(f"Loaded transcript data with {len(data.get('transcripts', []))} entries")
                return data
                
        except (json.JSONDecodeError, FileNotFoundError) as e:
            logger.error(f"Error loading transcript data: {e}")
            return {"meeting_info": {}, "transcripts": []}
    
    def format_transcript_for_llm(self, transcript_data: Dict[str, Any]) -> str:
        """
        Convert JSON transcript data to conversation format for LLM processing.
        Format: [Time] [Name] Text
        
        Args:
            transcript_data: The loaded JSON transcript data
            
        Returns:
            Formatted transcript string ready for LLM
        """
        transcripts = transcript_data.get("transcripts", [])
        
        if not transcripts:
            logger.warning("No transcript entries found")
            return "No transcript data available."
        
        # Sort by timestamp to ensure chronological order
        sorted_transcripts = sorted(transcripts, key=lambda x: x.get("timestamp", 0))
        
        # Extract formatted lines (already in [Time] [Name] Text format)
        formatted_lines = []
        for entry in sorted_transcripts:
            formatted_line = entry.get("formatted_line")
            if formatted_line:
                formatted_lines.append(formatted_line)
        
        conversation_transcript = "\n".join(formatted_lines)
        logger.info(f"Formatted transcript with {len(formatted_lines)} lines")
        
        return conversation_transcript
    
    def generate_meeting_minutes(
        self,
        transcript_data: Optional[Dict[str, Any]] = None,
        agendas: Optional[list] = None,
        progress_items: Optional[list] = None,
    ) -> Dict[str, Any]:
        try:
            if transcript_data is None:
                transcript_data = self.load_transcript_data()

            formatted_transcript = self.format_transcript_for_llm(transcript_data)
            if formatted_transcript == "No transcript data available.":
                return {
                    "success": False,
                    "error": "No transcript data available",
                    "minutes": None,
                    "metadata": transcript_data.get("meeting_info", {})
                }

            # NEW: fallbacks
            agendas = agendas or []
            progress_items = progress_items or []

            # NEW: call the chronology-first minutes prompt
            prompt = get_minutes_summarizer_prompt(
                transcript=formatted_transcript,
                agendas=agendas,
                progress_items=progress_items
            )

            logger.info(f"Sending transcript to {self.model_provider} ({self.model_name}) for minutes generation...")
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=4000
            )

            minutes_content = response.choices[0].message.content
            # Sanitize minutes: strip code fences and ensure it's a JSON array string
            try:
                cleaned = minutes_content.strip()
                # Remove leading and trailing triple backtick code fences if present
                if cleaned.startswith("```"):
                    # Strip starting fence with optional language tag
                    cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
                    # Remove trailing fence
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3]
                # Remove optional leading 'json' tag on first line
                if cleaned.lstrip().lower().startswith("json\n"):
                    cleaned = cleaned.lstrip()[5:]
                cleaned = cleaned.strip()
                # If the model escaped newlines (e.g., "\n"), unescape once
                if cleaned.startswith("[") is False and "[\n" in cleaned:
                    cleaned = cleaned.encode('utf-8').decode('unicode_escape')
                # Parse to verify valid JSON array; if not array, wrap appropriately
                parsed = json.loads(cleaned)
                if not isinstance(parsed, list):
                    parsed = [parsed]
                sanitized_minutes = json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception as _:
                # Fallback: best-effort removal of fences; keep original if parsing fails
                sanitized_minutes = minutes_content
            
            minutes_content = sanitized_minutes
            logger.info("Successfully generated meeting minutes")

            result = {
                "success": True,
                "minutes": minutes_content,
                "metadata": {
                    "meeting_info": transcript_data.get("meeting_info", {}),
                    "transcript_entries_count": len(transcript_data.get("transcripts", [])),
                    "generated_at": transcript_data.get("meeting_info", {}).get("created_at"),
                    "model_used": self.model_name,
                    "model_provider": self.model_provider
                },
                # Prefer structured raw transcript array for better JSON formatting
                "raw_transcript": formatted_transcript
            }

            # Normalize raw_transcript formatting: provide both string and array forms
            try:
                raw_lines = formatted_transcript.split("\n") if formatted_transcript else []
                result["raw_transcript_lines"] = [line for line in raw_lines if line != ""]
            except Exception:
                pass

            return result

        except Exception as e:
            logger.error(f"Error generating meeting minutes: {e}")
            return {
                "success": False,
                "error": str(e),
                "minutes": None,
                "metadata": transcript_data.get("meeting_info", {}) if transcript_data else {}
            }
    
    def save_meeting_minutes(self, minutes_data: Dict[str, Any], output_path: Optional[str] = None) -> bool:
        """
        Save the generated meeting minutes to a JSON file.
        
        Args:
            minutes_data: The generated minutes data from generate_meeting_minutes()
            output_path: Path where to save the minutes (uses settings if not provided)
            
        Returns:
            True if saved successfully, False otherwise
        """
        try:
            if output_path is None:
                # Use settings-based output directory
                output_dir = os.path.dirname(settings.TRANSCRIPT_FILE_PATH) or "output"
                output_path = os.path.join(output_dir, "meeting_minutes.json")
            
            # Ensure directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            # Ensure minutes field is a JSON array string; if so, also attach parsed
            try:
                minutes_str = minutes_data.get("minutes", "") or ""
                parsed_minutes = None
                if isinstance(minutes_str, str):
                    parsed_minutes = json.loads(minutes_str)
                elif isinstance(minutes_str, list):
                    parsed_minutes = minutes_str
                if parsed_minutes is not None:
                    minutes_data = dict(minutes_data)
                    minutes_data["minutes_array"] = parsed_minutes
                    try:
                        pretty_minutes2 = json.dumps(parsed_minutes, indent=2, ensure_ascii=False)
                        logger.info("📤 minutes_array (saving):\n" + pretty_minutes2)
                    except Exception:
                        pass
            except Exception:
                pass

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(minutes_data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Meeting minutes saved to: {output_path}")
            return True
            
        except Exception as e:
            logger.error(f"Error saving meeting minutes: {e}")
            return False
    
    def process_meeting_end(self, save_minutes: bool = True, output_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Complete workflow: Load transcript, generate minutes, and optionally save.
        This is the main function to call when a meeting ends.
        
        Args:
            save_minutes: Whether to save the minutes to file
            output_path: Path where to save the minutes
            
        Returns:
            Dict containing the complete minutes data
        """
        logger.info("Starting meeting minutes generation process...")
        
        # Generate minutes
        minutes_data = self.generate_meeting_minutes()
        
        # Save if requested and generation was successful
        if save_minutes and minutes_data.get("success", False):
            self.save_meeting_minutes(minutes_data, output_path)
        
        return minutes_data

if __name__ == "__main__":
    # ---- Example agendas ----
    agendas = [
        "Website Redesign Progress",
        "Q4 Marketing Campaign Plan",
        "Data Security Audit Findings",
        "Customer Support Improvements"
    ]

    # ---- Example progress items ----
    progress_items = [
        {
            "id": "d-101",
            "progress_type": "Decision",
            "description": "Launch redesigned homepage on November 15",
            "assignee": "Sarah Johnson",
            "due_date_iso": "2025-11-15",
            "due_text_original": "by November 15",
            "status": "remaining"
        },
        {
            "id": "a-202",
            "progress_type": "Agreement",
            "description": "Prepare Q4 social media calendar",
            "assignee": "Ravi Kumar",
            "due_date_iso": "2025-10-10",
            "due_text_original": "by October 10",
            "status": "remaining"
        },
        {
            "id": "i-303",
            "progress_type": "Issue",
            "description": "Data breach risk due to outdated encryption library",
            "assignee": "IT Security Team",
            "due_date_iso": "2025-09-30",
            "due_text_original": "by September 30",
            "status": "remaining"
        },
        {
            "id": "a-204",
            "progress_type": "Agreement",
            "description": "Hire two additional support agents",
            "assignee": "HR Department",
            "due_date_iso": "2025-10-05",
            "due_text_original": "by October 5",
            "status": "remaining"
        }
    ]

    # ---- Example transcript, converted to your JSON structure ----
    # Your class expects a list under key "transcripts", each with "formatted_line" and (optionally) "timestamp".
    # We'll add numeric timestamps for chronological sorting and reuse the [HH:MM:SS] line as formatted_line.
    raw_lines = [
        "[09:00:00] [Sarah Johnson] Good morning everyone, let's get started.",
        "[09:00:05] [Sarah Johnson] First up, Website Redesign Progress.",
        "[09:00:12] [Alex Lee] The redesign is 85% complete. We're finalizing the mobile responsive layouts this week.",
        "[09:00:30] [Maria Lopez] The product team has signed off on the new hero section design.",
        "[09:00:50] [Sarah Johnson] Great. Let's confirm the launch date.",
        "[09:01:00] [Alex Lee] I propose November 15 for the launch.",
        "[09:01:10] [Sarah Johnson] Agreed. We'll launch the redesigned homepage by November 15.",
        "[09:01:40] [John Miller] The QA team will complete final testing by November 10.",
        "",
        "[09:02:00] [Sarah Johnson] Moving to Q4 Marketing Campaign Plan.",
        "[09:02:15] [Ravi Kumar] We'll start with the social media calendar — I'll have it ready by October 10.",
        "[09:02:40] [Emily Chen] We should run A/B tests for the email campaign creatives in the second week of October.",
        "[09:03:00] [Ravi Kumar] Agreed. I’ll also prepare influencer outreach proposals by October 12.",
        "[09:03:20] [Sarah Johnson] Sounds good. The budget allocation will be reviewed next Monday.",
        "",
        "[09:04:00] [Sarah Johnson] Now onto Data Security Audit Findings.",
        "[09:04:15] [David Park] The audit revealed an outdated encryption library in our payment system, posing a security risk.",
        "[09:04:30] [Sarah Johnson] How soon can we address this?",
        "[09:04:40] [IT Security Team] We'll patch it and migrate to the latest version by September 30.",
        "[09:05:10] [David Park] No other major security issues were found.",
        "",
        "[09:06:00] [Sarah Johnson] Lastly, Customer Support Improvements.",
        "[09:06:15] [Linda Scott] We’ve had a 20% increase in support tickets this quarter.",
        "[09:06:30] [HR Department] We will hire two more support agents by October 5.",
        "[09:06:50] [Linda Scott] We’re also improving the chatbot to resolve 15% more queries automatically.",
        "[09:07:10] [Sarah Johnson] Excellent. That will help response times.",
        "",
        "[09:07:30] [Sarah Johnson] Before we wrap up, any other urgent items?",
        "[09:07:40] [Alex Lee] Yes, the staging server SSL certificate will expire in two weeks.",
        "[09:07:55] [Sarah Johnson] Let’s renew it this week to avoid last-minute issues.",
        "[09:08:10] [David Park] I’ll handle it and confirm once it’s done.",
        "",
        "[09:08:30] [Sarah Johnson] Great meeting, thanks everyone."
    ]

    def hhmmss_to_seconds(hms: str) -> int:
        h, m, s = map(int, hms.split(":"))
        return h*3600 + m*60 + s

    transcripts = []
    for line in raw_lines:
        if not line.strip():
            continue
        # extract [HH:MM:SS]
        ts = line.split("]")[0].replace("[", "")  # "09:00:00"
        transcripts.append({
            "timestamp": hhmmss_to_seconds(ts),
            "formatted_line": line
        })

    # Build transcript_data in the shape your class expects
    transcript_data = {
        "meeting_info": {"created_at": "2025-08-04T09:15:00Z"},
        "transcripts": transcripts
    }

    # ---- Run the processor on in-memory data ----
    processor = MinutesProcessor(json_transcript_path="transcript/meeting_transcript.json")
    result = processor.generate_meeting_minutes(
        transcript_data=transcript_data,
        agendas=agendas,
        progress_items=progress_items
    )

    # Print or save
    if result.get("success"):
        print("\n=== MINUTES (MODEL OUTPUT) ===\n")
        print(result["minutes"])
    else:
        print("Failed:", result.get("error"))