# Teams Real-time Agenda & Tasks AI

## Overview

The AI server behind a Microsoft Teams meeting bot. It listens to a live meeting, transcribes every speaker separately, and **tracks the agenda in real time**: which item is being discussed, which is on hold, and which is done. At the same time it **captures tasks, decisions and issues** with an owner and a due date. When the meeting ends it writes the **meeting minutes** and sends each person their tasks as a card in Teams.

Runs in production for a meeting-productivity SaaS. Supports **English and German** (and a mix of both).

## Key Features

- **Live per-speaker transcription**: one streaming speech-to-text connection per participant, so speakers never get mixed up
- **Real-time agenda tracking**: an LLM moves agenda items between *Not Started → In Progress → On Hold → Completed* as people talk
- **Automatic task capture**: finds **Agreements** (tasks), **Decisions** and **Issues** with assignee and due date ("Sarah will send the budget by Friday")
- **Name correction**: fixes names the speech-to-text got wrong and maps them to real participant IDs
- **Agenda timers and overtime alerts**: each item has a time budget, and the host gets an alert the moment it runs over
- **Manual override**: the host can change an agenda status from the UI, and the AI respects it
- **Meeting minutes**: a structured summary when the call ends
- **Task cards in Teams**: every assignee gets their tasks as an Adaptive Card
- **Meeting analytics**: how often each participant spoke, agenda state history with timestamps, and a log of every LLM call

## High-Level Architecture

```
┌─────────────────────────┐
│  Microsoft Teams call   │
│  (bot joins the meeting)│
└────────────┬────────────┘
             │ each speaker's audio, live
             ▼
┌─────────────────────────┐   audio    ┌─────────────────────────┐
│  Agenda & Tasks AI      │ ─────────► │  Speech-to-Text API     │
│  server (this repo)     │ ◄───────── │  (streaming)            │
└────────────┬────────────┘ live text  └─────────────────────────┘
             │ new transcript after each pause
             ▼
┌─────────────────────────┐
│  External LLM call      │
│  • Agenda tracking      │
│  • Task tracking        │
│    per person           │
└────────────┬────────────┘
             │ live updates
             ▼
┌─────────────────────────┐
│  Meeting dashboard      │
│  agenda status, tasks,  │
│  overtime alerts        │
└────────────┬────────────┘
             │ meeting ends
             ▼
  Meeting minutes + task cards in Teams
```

**Flow:** Bot joins the Teams call → streams each speaker's audio to the server → speech-to-text turns it into live text per speaker → after each pause, the new text goes to the LLM → agenda status and each person's tasks update live on the dashboard → when the meeting ends, minutes are written and task cards go out in Teams.

## Design Decisions & Trade-offs

- **One STT stream per speaker, not one for the whole call.** The bot gets separate (unmixed) audio for each participant, so the transcript always knows who said what. This matters because task owners come from *who* spoke. Trade-off: more open speech-to-text connections per meeting.
- **Debounced LLM calls (250 ms after speech stops).** Calling the LLM on every word is slow and expensive; waiting for a short pause batches the speech into one call. Stale scheduled calls are dropped, so only the latest one runs.
- **Only new transcript goes to the LLM.** Each call gets the text since the last call plus the current agenda state, not the whole meeting. This keeps prompts small and fast even in long meetings.
- **Agenda and tasks run as two separate LLM calls, in parallel.** Two focused prompts are more accurate than one big prompt doing both. Results are emitted as soon as *each* finishes, so the faster one doesn't wait for the slower one.
- **Keep-alive and silence handling on STT streams.** The speech-to-text provider closes idle connections after about 10 seconds, so the server sends keep-alives and closes a speaker's stream after 45 seconds of silence to save cost.
- **Socket.IO with reconnect and exponential backoff.** If the bot is removed and re-added, or the network drops, the client reconnects and rejoins the meeting room without losing state.
- **Model provider is swappable.** All LLM calls go through one OpenAI-compatible client, so the provider can be changed with a setting. A small, fast model handles the live calls; a stronger model is used only for the final minutes.
- **Trade-off: in-memory meeting state.** Fast and simple for one meeting per process, but state is lost if the process crashes mid-meeting. Each meeting runs as its own process, so one crash doesn't affect others.

## Tech Stack

| Layer          | Technology                                                    |
|----------------|---------------------------------------------------------------|
| Runtime        | Python 3.12, FastAPI, asyncio, Uvicorn                        |
| AI             | Streaming speech-to-text API, LLM API (provider-swappable)    |
| Real-time      | WebSockets (bot ↔ server), Socket.IO (server ↔ platform)      |
| Microsoft      | Microsoft Teams SDK (real-time media bot), Microsoft Graph API, Adaptive Cards, MSAL |
| Cloud          | Docker, AWS CloudWatch logging                                |

## Project Structure

```
app/
├── realtime/
│   ├── bot_websocket/        WebSocket endpoint the Teams bot connects to
│   ├── websocket_handlers.py meeting_details / audio / meeting_ended events
│   └── client_socketio/      Socket.IO client that pushes live updates to the platform
├── ProgressAnalyzer/
│   ├── STT.py                streaming speech-to-text per speaker
│   ├── processors/           agenda tracker, task extractor, name correction, minutes
│   └── prompts/              LLM prompts (English + German)
└── utils/meeting/
    ├── llm_coordinator.py    debounce + parallel LLM calls + instant emit
    ├── agenda_timer_manager.py  time budgets and overtime alerts
    ├── analytics.py          participation, agenda history, LLM call log
    └── participant_manager.py   speaker ↔ participant mapping
```

## Running locally

```bash
python -m venv venv && venv\Scripts\activate      # Windows
pip install -r requirements.txt
copy .env.example .env                             # then fill in your keys
python uvicorn_config.py --meeting_id <id> --organization_id <id> --bot_join_url http://localhost:9442
```

Note: this service is one part of a larger system. To run end-to-end it needs the Teams media bot (streams the audio) and the meeting platform backend (agenda data and live UI).

## My Role

I designed and built this AI server end to end: the real-time audio pipeline, per-speaker speech-to-text streaming, the agenda-tracking and task-extraction prompts, the LLM coordinator, timers and alerts, analytics, minutes, and the Socket.IO integration with the platform.

## Disclaimer

This code runs in a real production system. Company names, endpoints, IDs and all meeting data have been removed or replaced for public release.
