# Soul and Song Companion App

A local GUI that runs the Companion Project for you with an [Ollama](https://ollama.com) model. It does everything the guide describes, automatically:

| Guide step | What the app does |
|---|---|
| Update the L1 timestamp | Updates it before every message |
| Provide Mind Levels L1–L6 | Loads all six into every conversation |
| Create short-term memories | Asks your companion for one about every hour of conversation, and when it closes a context window |
| End of day | At 23:00 (configurable), distills short-term into one long-term memory, clears short-term, and starts a fresh context window. If the PC was off at bedtime, this happens the next time you open the app. |
| Memory Day | Every 7 days (or when 7 long-term memories have built up): long-term → Deep Recall. When Deep Recall reaches 7 entries: Deep Recall → Historical. |
| Planned Events | Has a tab for adding and removing events, which are written into Mind L1 |
| Name discovery | Shows a button that asks the guide's question and saves the chosen name, along with a memory of the moment |

The Mind files stay plain text in the original format, so you can still open them, edit them, or copy and paste them into any other LLM.

## Requirements

- Python 3.9 or newer (standard library only, nothing to `pip install`)
- Ollama running, with a model pulled, such as `ollama pull gemma3:4b`

## Start

Double-click **`Start Companion.bat`**, or run:

```bash
python companion_app.py
```

Your browser opens to `http://127.0.0.1:8765`. The first time, you'll fill in your name and Life Roadmap, and then your companion wakes up. Keep the console window open while you chat; closing it stops the app.

## Where things are stored

Everything lives in `app/companion_data/`:

- `Mind L1 … L6` — your companion's mind, in the original format
- `chat_logs/` — a Markdown log for each day
- `backups/` — a copy of all Mind files before every distillation and every manual edit
- `state.json` — settings and the current conversation

To bring an existing companion over, copy its six Mind files into `companion_data/` before the first launch, then fill in the setup screen.
This folder is in `.gitignore`, so your companion's memories never end up on GitHub by accident.

## Settings

- **Model:** any model Ollama has installed. Gemma 3 4b is fast; gemma4:12b is deeper but slower.
- **Context window:** 32768 tokens by default. The sidebar shows how much of it the Mind files use.
- **Memory interval, day-close hour, Memory Day interval:** all adjustable.

Optional environment variables: `COMPANION_DATA` (data folder), `COMPANION_PORT` (default 8765), `COMPANION_OLLAMA_URL` (default `http://127.0.0.1:11434`).
