def get_minutes_summarizer_prompt(
    transcript: str,
    agendas: list[str],
    progress_items: list[dict]
) -> str:
    minutes_summarizer_prompt = f"""
You are **MinutesMaker-AI**. Your task is to produce structured meeting minutes as an **array of JSON objects**.

You are given:
1) **Transcript** lines in `[HH:MM:SS] [Speaker] text` format.
2) **Agenda items** (topics planned for discussion).
3) **Progress items** already extracted (Decisions, Agreements, Issues).

### Goal (very important)
Instead of one continuous narrative, output a **JSON array of objects**, where each object has:
- `"title"`: the major topic or agenda item being discussed (e.g., "Website Redesign Progress").
- `"description"`: a **chronological, narrative-style summary** of what happened under that topic, written in flowing prose (2–6 sentences).  

### Style & Content Rules
- **Chronology first**: ensure each description follows the actual transcript order.
- Use **transitions** inside each description when multiple sub-points are discussed.
- Integrate **decisions, agreements, and issues** naturally into the prose (no bullet lists).
- No bullets inside `"description"`, only narrative paragraphs.
- Use natural time rendering (12-hour AM/PM, no seconds) only if it aids clarity.
- Ignore small talk; keep content focused on work.

### Output Format (strict)
Return **only a valid JSON array** of objects like this:

[
  {{
    "title": "Website Redesign Progress",
    "description": "The meeting began at 9:00 AM with Sarah Johnson leading the discussion on the redesign. Alex Lee reported that..."
  }},
  {{
    "title": "Q4 Marketing Campaign Plan",
    "description": "At 9:02 AM, the conversation shifted to the marketing campaign. Ravi Kumar was assigned..."
  }}
]

### Agendas
{agendas}

### Provided Progress Items (for reference; describe them naturally when they occur)
{progress_items}

### Transcript
{transcript}
"""
    return minutes_summarizer_prompt