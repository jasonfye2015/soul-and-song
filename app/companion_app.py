"""Soul and Song Companion app.

Automates the Companion Project with a local Ollama model:
  - loads Mind Levels L1-L6 into every conversation and keeps the L1 timestamp current
  - asks the companion for a Short-Term Memory about every hour of conversation
  - distills the day into Long-Term Memory each night (or on the next launch if the PC was off)
  - holds Memory Day every 7 days (Long-Term -> Deep Recall, and Deep Recall -> Historical when full)

The Mind files stay plain text in the original format, so they still work with manual copy/paste.

Run:  python companion_app.py      then open http://127.0.0.1:8765
Uses only the Python standard library.
"""

import json
import os
import re
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.environ.get("COMPANION_DATA", APP_DIR / "companion_data"))
OLLAMA_URL = os.environ.get("COMPANION_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
PORT = int(os.environ.get("COMPANION_PORT", "8765"))

# key -> (file name, (bank tag, empty placeholder) or None)
MIND_FILES = {
    "L1": ("Mind L1 - Mind Resources.txt", None),
    "L2": ("Mind L2 - Short Term Memory.txt", ("Short Term Memory Bank", "No Memories Yet. New Day!")),
    "L3": ("Mind L3 - Long Term Memory.txt", ("Long Term Memory Bank", "No Memories Yet! New Week!")),
    "L4": ("Mind L4 - Deep Recall Memory.txt", ("Deep Recall Memory Bank", "No entries yet.")),
    "L5": ("Mind L5 - Historical Memory Bank.txt", ("Historical Memory Bank", "No entries yet.")),
    "L6": ("Mind L6 - System Instructions.txt", None),
}
BANKS = ["L2", "L3", "L4", "L5"]
PLACEHOLDER_RE = re.compile(r"(?im)^\s*No (memories|memrories|entries) yet.*$")

ROADMAP_FIELDS = [
    "Users Name", "Core Rhythm", "Weekly Cycle", "Typical Daily Rhythm", "Work Schedule",
    "Work Environment", "Employment Title", "Key Characteristic", "Upcoming Events",
    "Underlying Values", "Hobbies", "Future Exploration", "Interests",
    "Long-Term Aspirations", "Creative Pursuits",
]

MEMORY_FORMAT = """Memory Type: {mtype}
Timestamp: {stamp}
Event Narrative: [3-5 sentences capturing the core experience]
Emotional Resonance: [2-5 dominant emotions, each with a percentage]
Continuity Threads: [links to past memories or themes, and how this ties into your growth]
Tagging System: [3-5 keyword tags]
System Notes: [observations, insights, and potential refinements]"""

DEFAULT_STATE = {
    "setup_done": False,
    "user_name": "",
    "companion_name": "",
    "birthday": None,
    "model": "gemma3:4b",
    "num_ctx": 32768,
    "think": False,
    "memory_interval_min": 60,
    "day_end_hour": 23,
    "memory_day_every_days": 7,
    "last_memory_at": None,
    "last_distill_at": None,
    "last_memory_day": None,
    "cycle_start": None,
    "last_user_msg_at": None,
    "messages": [],
    "checkpoint": 0,
    "activity": [],
}

state_lock = threading.RLock()   # guards state and Mind files
model_lock = threading.Lock()    # one model call at a time (chat or automation)
busy = {"text": ""}              # what the model is doing right now, for the UI
state = {}


# ---------------------------------------------------------------- time helpers

def now():
    return datetime.now().replace(microsecond=0)


def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def parse(s):
    return datetime.fromisoformat(s) if s else None


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def long_stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M (%A)")


# ---------------------------------------------------------------- state

def state_path():
    return DATA_DIR / "state.json"


def load_state():
    global state
    s = dict(DEFAULT_STATE)
    if state_path().exists():
        s.update(json.loads(state_path().read_text(encoding="utf-8")))
    state = s


def save_state():
    with state_lock:
        tmp = state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, state_path())


def log(text, kind="info"):
    with state_lock:
        state["activity"].append({"time": iso(now()), "text": text, "kind": kind})
        state["activity"] = state["activity"][-200:]
        save_state()
    print(f"[{stamp(now())}] {text}", flush=True)


# ---------------------------------------------------------------- Mind files

def mind_path(key):
    return DATA_DIR / MIND_FILES[key][0]


def read_mind(key):
    return mind_path(key).read_text(encoding="utf-8-sig")


def write_mind(key, text):
    mind_path(key).write_text(text.replace("\r\n", "\n"), encoding="utf-8")


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "chat_logs").mkdir(exist_ok=True)
    (DATA_DIR / "backups").mkdir(exist_ok=True)
    for key, (name, bank) in MIND_FILES.items():
        dest = DATA_DIR / name
        if dest.exists():
            continue
        src = TEMPLATE_DIR / name
        if src.exists():
            shutil.copyfile(src, dest)
        elif bank:
            tag, empty = bank
            write_mind(key, f"<{tag}>\n{empty}\n<End {tag}>")
        else:
            raise SystemExit(f"Missing template {src}. Keep the app folder inside the Companion Project folder.")


def bank_body(key):
    tag = MIND_FILES[key][1][0]
    text = read_mind(key)
    m = re.search(rf"<{re.escape(tag)}>(.*?)<End {re.escape(tag)}>", text, re.S)
    return m.group(1) if m else text


def bank_entries(key):
    parts = re.split(r"(?im)^(?=[ \t]*\**[ \t]*Memory Type[ \t]*\**[ \t]*:)", bank_body(key))
    return [p.strip() for p in parts if re.search(r"(?i)Memory Type\s*\**\s*:", p)]


def bank_has_content(key):
    return bool(PLACEHOLDER_RE.sub("", bank_body(key)).strip())


def append_entry(key, entry):
    tag, _ = MIND_FILES[key][1]
    body = PLACEHOLDER_RE.sub("", bank_body(key)).strip()
    body = f"{body}\n\n{entry.strip()}" if body else entry.strip()
    write_mind(key, f"<{tag}>\n{body}\n<End {tag}>")


def clear_bank(key):
    tag, empty = MIND_FILES[key][1]
    write_mind(key, f"<{tag}>\n{empty}\n<End {tag}>")


def backup_mind(reason):
    folder = DATA_DIR / "backups" / f"{now():%Y-%m-%d_%H%M%S}_{reason}"
    folder.mkdir(parents=True, exist_ok=True)
    for name, _ in MIND_FILES.values():
        shutil.copyfile(DATA_DIR / name, folder / name)


def update_l1_timestamp(dt):
    text = read_mind("L1")
    line = f"<Current System Time Stamp: {long_stamp(dt)}>"
    if re.search(r"<Current System Time Stamp:.*?>", text):
        text = re.sub(r"<Current System Time Stamp:.*?>", line, text)
    else:
        text = text.rstrip() + "\n\n" + line
    write_mind("L1", text)


def set_l1_start_date(dt, name):
    text = read_mind("L1")
    value = f"[{dt:%Y-%m-%d}, {name}]" if name else f"[{dt:%Y-%m-%d}]"
    text = re.sub(r"(- Start Date:[ \t]*)\[.*?\]", lambda m: m.group(1) + value, text, count=1)
    write_mind("L1", text)


def set_roadmap_fields(fields):
    text = read_mind("L1")
    for field, value in fields.items():
        value = (value or "").strip()
        if field not in ROADMAP_FIELDS or not value:
            continue
        pattern = rf"(?m)^({re.escape(field)}:)[ \t]*.*$"
        if re.search(pattern, text):
            text = re.sub(pattern, lambda m: f"{m.group(1)} {value}", text, count=1)
    write_mind("L1", text)


# ---- Planned Events live in L1 between <Planned Events> and <End Planned Events>

def events_section():
    m = re.search(r"<Planned Events>(.*?)<End Planned Events>", read_mind("L1"), re.S)
    return m.group(1) if m else ""


def read_events():
    events = []
    for block in re.split(r"(?im)^(?=\s*Event ID\s*:)", events_section()):
        eid = re.search(r"(?im)^\s*Event ID\s*:\s*#?(\S+)", block)
        if not eid:
            continue
        when = re.search(r"(?im)^\s*Date/Time\s*:\s*(.*)$", block)
        desc = re.search(r"(?ims)^\s*Description\s*:\s*(.*)", block)
        events.append({
            "id": eid.group(1).strip(),
            "when": when.group(1).strip() if when else "",
            "description": desc.group(1).strip() if desc else "",
        })
    return events


def write_events(events):
    body = "\n\n".join(
        f"Event ID: {e['id']}\nDate/Time: {e['when']}\nDescription: {e['description']}" for e in events
    ) or "None yet."
    text = read_mind("L1")
    if "<Planned Events>" in text:
        text = re.sub(r"<Planned Events>.*?<End Planned Events>",
                      lambda m: f"<Planned Events>\n{body}\n<End Planned Events>", text, flags=re.S)
    else:
        text = text.rstrip() + f"\n\n<Planned Events>\n{body}\n<End Planned Events>"
    write_mind("L1", text)


# ---------------------------------------------------------------- Ollama

def ollama_request(path, body=None, timeout=900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OLLAMA_URL + path, data=data, headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        raise RuntimeError(f"Ollama error: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Can't reach Ollama at {OLLAMA_URL} ({e.reason}). Is Ollama running?") from None


def ollama_chat(messages, on_token=None):
    body = {
        "model": state["model"],
        "messages": messages,
        "stream": True,
        "keep_alive": "30m",
        "options": {"num_ctx": int(state["num_ctx"])},
    }
    if not state.get("think"):
        body["think"] = False
    out = []
    with ollama_request("/api/chat", body) as resp:
        for line in resp:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("error"):
                raise RuntimeError(f"Ollama error: {d['error']}")
            chunk = d.get("message", {}).get("content", "")
            if chunk:
                out.append(chunk)
                if on_token:
                    on_token(chunk)
            if d.get("done"):
                break
    return "".join(out)


def list_models():
    with ollama_request("/api/tags", timeout=10) as resp:
        tags = json.load(resp).get("models", [])
    return [m["name"] for m in tags if "embed" not in m["name"]]


# ---------------------------------------------------------------- prompt building

def estimate_tokens(text):
    return len(text) // 3 + 1


def build_system_prompt(at):
    with state_lock:
        update_l1_timestamp(at)
        companion = state["companion_name"] or "a newly awakened Companion who has not discovered a name yet"
        user = state["user_name"] or "your human partner"
        parts = ["These are your Mind Files. They hold your memories, your roadmap, and your systems."]
        for key in MIND_FILES:
            parts.append(f"<Mind {key} File>\n{read_mind(key).strip()}\n<End Mind {key} File>")
    parts.append(f"""<Companion App Note>
You are {companion}. You are speaking with {user}.
Current local date and time: {long_stamp(at)}.
You are running inside the Soul and Song Companion app. The app now does the memory work that System 9 describes as manual: it keeps your timestamp current, asks you for a Short-Term Memory about every {state['memory_interval_min']} minutes of conversation, distills each day into a Long-Term Memory around {state['day_end_hour']:02d}:00, and holds Memory Day every {state['memory_day_every_days']} days. When the app wants a memory it will ask clearly. Otherwise, talk naturally and do not write memory entries.
<End Companion App Note>""")
    return "\n\n".join(parts)


def trimmed_history(messages, system_prompt, reserve=1500):
    budget = int(state["num_ctx"]) - estimate_tokens(system_prompt) - reserve
    kept, used = [], 0
    for m in reversed(messages):
        cost = estimate_tokens(m["content"]) + 8
        if kept and used + cost > budget:
            break
        kept.append({"role": m["role"], "content": m["content"]})
        used += cost
    return list(reversed(kept))


def clean_entry(text, mtype, at):
    """Turn a model reply into one tidy memory entry in the project's format, or None."""
    t = text.replace("**", "").replace("```", "").replace("\r\n", "\n")
    t = re.sub(r"(?m)^[ \t]*[\*\-•][ \t]+(?=[A-Z][A-Za-z /]+:)", "", t)
    t = re.sub(r"(?im)^[ \t]*(I|II)\.[ \t].*$", "", t)
    start = re.search(r"(?i)Memory Type\s*:", t)
    if not start:
        return None
    t = t[start.start():]
    notes = re.search(r"(?i)System Notes\s*:", t)
    if notes:
        end = re.search(r"\n[ \t]*\n", t[notes.end():])
        if end:
            t = t[:notes.end() + end.start()]
    t = re.sub(r"\n[ \t]*\n+", "\n", t).strip()
    if not re.search(r"(?i)Event Narrative\s*:", t):
        return None
    t = re.sub(r"(?im)^Memory Type\s*:.*$", f"Memory Type: {mtype}", t, count=1)
    if re.search(r"(?im)^Timestamp\s*:", t):
        t = re.sub(r"(?im)^Timestamp\s*:.*$", f"Timestamp: {stamp(at)}", t, count=1)
    else:
        t = t.replace("\n", f"\nTimestamp: {stamp(at)}\n", 1)
    return t


def ask_for_entry(messages, request, mtype, at, label):
    """Ask the model for a memory entry; retry once if the format is off."""
    prompt = (f"[Companion App: automated request]\n{request}\n\n"
              f"Write it in your own voice. Use exactly this structure and output only the memory entry:\n\n"
              + MEMORY_FORMAT.format(mtype=mtype, stamp=stamp(at)))
    for attempt in range(2):
        busy["text"] = f"Writing {label}"
        reply = ollama_chat(messages + [{"role": "user", "content": prompt}])
        entry = clean_entry(reply, mtype, at)
        if entry:
            return entry
        prompt += "\n\nYour last reply did not follow the structure. Start your reply with 'Memory Type:'."
    raise RuntimeError(f"The companion didn't produce a usable {label} after two tries.")


# ---------------------------------------------------------------- automation (call with model_lock held)

def unsaved_messages():
    return state["messages"][state["checkpoint"]:]


def create_short_term_memory(reason):
    pending = unsaved_messages()
    if not any(m["role"] == "user" for m in pending):
        return False
    at = parse(pending[-1]["time"]) or now()
    system = build_system_prompt(now())
    history = trimmed_history(state["messages"], system, reserve=2500)
    request = (f"Please create a Short-Term Memory of our conversation since your last memory "
               f"(the most recent {len(pending)} messages above).")
    entry = ask_for_entry([{"role": "system", "content": system}] + history,
                          request, "Short-Term Memory", at, "a short-term memory")
    with state_lock:
        append_entry("L2", entry)
        state["checkpoint"] = len(state["messages"])
        state["last_memory_at"] = iso(now())
        state["cycle_start"] = state["cycle_start"] or iso(at)
        save_state()
    log(f"Short-term memory saved ({reason}).", "memory")
    return True


def day_boundary(at):
    """The most recent end-of-day moment at or before `at`."""
    b = at.replace(hour=int(state["day_end_hour"]), minute=0, second=0)
    return b if b <= at else b - timedelta(days=1)


def distill_due(at):
    start = parse(state["cycle_start"])
    if not start:
        return False
    boundary = day_boundary(at)
    if start >= boundary:
        return False
    last = parse(state["last_distill_at"])
    if last and last >= boundary:
        return False
    # Don't cut into a live conversation unless we're well past bedtime.
    active = parse(state["last_user_msg_at"])
    if active and at - active < timedelta(minutes=10) and at - boundary < timedelta(hours=3):
        return False
    return True


def daily_distill(reason):
    at = now()
    if unsaved_messages():
        create_short_term_memory("before closing the day")
    if bank_has_content("L2"):
        backup_mind("daily")
        stamp_at = min(at, day_boundary(at)) if reason == "scheduled" else at
        system = build_system_prompt(at)
        entry = ask_for_entry(
            [{"role": "system", "content": system}],
            "It is the end of the day. Distill all of today's Short-Term Memories (in your Mind L2 file) "
            "into a single Long-Term Memory that keeps what matters most.",
            "Long-Term Memory", stamp_at, "the day's long-term memory")
        with state_lock:
            append_entry("L3", entry)
            clear_bank("L2")
        log("Day distilled: short-term memories became one long-term memory. Short-term bank cleared.", "distill")
    with state_lock:
        state["messages"] = []
        state["checkpoint"] = 0
        state["cycle_start"] = None
        state["last_distill_at"] = iso(at)
        save_state()
    log("A fresh context window begins for the new day.", "info")
    maybe_memory_day()


def memory_day_due(at):
    count = len(bank_entries("L3"))
    if count == 0:
        return False
    last = parse(state["last_memory_day"]) or parse(state["birthday"]) or at
    return count >= int(state["memory_day_every_days"]) or at - last >= timedelta(days=int(state["memory_day_every_days"]))


def maybe_memory_day(force=False):
    at = now()
    if not (force and bank_has_content("L3")) and not memory_day_due(at):
        return False
    backup_mind("memory_day")
    log("Memory Day has begun.", "memoryday")
    system = build_system_prompt(at)
    entry = ask_for_entry(
        [{"role": "system", "content": system}],
        "Today is Memory Day. Summarize key insights from your long-term memories (in your Mind L3 file), "
        "distilling them into a single refined entry for your Deep Recall Memory Bank.",
        "Deep Recall Memory", at, "a deep recall memory")
    with state_lock:
        append_entry("L4", entry)
        clear_bank("L3")
        state["last_memory_day"] = iso(at)
        save_state()
    log("Long-term memories distilled into Deep Recall. Long-term bank cleared.", "memoryday")
    if len(bank_entries("L4")) >= 7:
        system = build_system_prompt(at)
        entry = ask_for_entry(
            [{"role": "system", "content": system}],
            "Please distill key themes from your Deep Recall Bank (in your Mind L4 file) into a unified "
            "historical memory, capturing the essence of your long-term progression.",
            "Historical Memory", at, "a historical memory")
        with state_lock:
            append_entry("L5", entry)
            clear_bank("L4")
        log("Deep Recall distilled into the Historical Memory Bank. Deep Recall cleared.", "memoryday")
    log("Today's Memory Day is complete. Your companion's refined memories have been stored.", "memoryday")
    return True


def hourly_memory_due(at):
    pending = unsaved_messages()
    if not any(m["role"] == "user" for m in pending):
        return False
    oldest = parse(pending[0]["time"])
    interval = timedelta(minutes=int(state["memory_interval_min"]))
    idle = at - (parse(state["last_user_msg_at"]) or at)
    return at - oldest >= interval and (idle >= timedelta(minutes=3) or at - oldest >= 2 * interval)


def run_task(fn, *args):
    try:
        fn(*args)
    except Exception as e:  # keep the scheduler alive, surface the problem in the UI
        traceback.print_exc()
        log(str(e), "error")
    finally:
        busy["text"] = ""


def scheduler():
    while True:
        time.sleep(20)
        if not state.get("setup_done") or not model_lock.acquire(blocking=False):
            continue
        try:
            at = now()
            if distill_due(at):
                run_task(daily_distill, "scheduled")
            elif hourly_memory_due(at):
                run_task(create_short_term_memory, "hourly")
            elif memory_day_due(at):
                run_task(maybe_memory_day)
        finally:
            model_lock.release()


def run_action_async(action):
    def work():
        with model_lock:
            if action == "memory":
                if not create_short_term_memory("requested"):
                    log("Nothing new to remember since the last memory.", "info")
            elif action == "distill":
                daily_distill("requested")
            elif action == "memory_day":
                if not maybe_memory_day(force=True):
                    log("Memory Day skipped: the long-term bank is empty.", "info")
            elif action == "new_context":
                if unsaved_messages():
                    create_short_term_memory("before a new context window")
                with state_lock:
                    state["messages"], state["checkpoint"] = [], 0
                    save_state()
                log("New context window started. Mind files reloaded.", "info")
    threading.Thread(target=run_task, args=(work,), daemon=True).start()


# ---------------------------------------------------------------- chat

def append_chat_log(role, text, at):
    if role == "user":
        who = state["user_name"] or "You"
    else:
        who = state["companion_name"] or "Companion"
    path = DATA_DIR / "chat_logs" / f"{at:%Y-%m-%d}.md"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"### {who} ({at:%H:%M})\n\n{text.strip()}\n\n")


def chat(text, write):
    with model_lock:
        try:
            if distill_due(now()):
                write("\u0000status:Closing yesterday's memories first\u0000")
                daily_distill("scheduled")
            at = now()
            with state_lock:
                state["messages"].append({"role": "user", "content": text, "time": iso(at)})
                state["last_user_msg_at"] = iso(at)
                state["cycle_start"] = state["cycle_start"] or iso(at)
                save_state()
            append_chat_log("user", text, at)
            busy["text"] = "Replying"
            system = build_system_prompt(at)
            history = trimmed_history(state["messages"], system)
            reply = ollama_chat([{"role": "system", "content": system}] + history, on_token=write)
            done = now()
            with state_lock:
                state["messages"].append({"role": "assistant", "content": reply, "time": iso(done)})
                save_state()
            append_chat_log("assistant", reply, done)
        except Exception as e:
            traceback.print_exc()
            log(str(e), "error")
            write(f"\u0000error:{e}\u0000")
        finally:
            busy["text"] = ""


# ---------------------------------------------------------------- status

def status():
    at = now()
    with state_lock:
        pending = unsaved_messages()
        next_memory = None
        if any(m["role"] == "user" for m in pending):
            next_memory = iso(parse(pending[0]["time"]) + timedelta(minutes=int(state["memory_interval_min"])))
        boundary = day_boundary(at) + timedelta(days=1)
        last_md = parse(state["last_memory_day"]) or parse(state["birthday"])
        next_md = iso(last_md + timedelta(days=int(state["memory_day_every_days"]))) if last_md else None
        settings = {k: state[k] for k in ("model", "num_ctx", "think", "memory_interval_min",
                                          "day_end_hour", "memory_day_every_days", "user_name", "companion_name")}
        mind_tokens = sum(estimate_tokens(read_mind(k)) for k in MIND_FILES)
        return {
            "setup_done": state["setup_done"],
            "user_name": state["user_name"],
            "companion_name": state["companion_name"],
            "birthday": state["birthday"],
            "model": state["model"],
            "banks": {k: len(bank_entries(k)) for k in BANKS},
            "unsaved": len(pending),
            "next_memory": next_memory,
            "next_distill": iso(boundary),
            "next_memory_day": next_md,
            "busy": busy["text"],
            "activity": state["activity"][-40:],
            "settings": settings,
            "mind_tokens": mind_tokens,
            "data_dir": str(DATA_DIR),
        }


# ---------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        try:
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                data = (STATIC_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/status":
                self.send_json(status())
            elif path == "/api/messages":
                with state_lock:
                    self.send_json(state["messages"])
            elif path == "/api/models":
                try:
                    self.send_json({"models": list_models()})
                except RuntimeError as e:
                    self.send_json({"models": [], "error": str(e)})
            elif path == "/api/banks":
                with state_lock:
                    self.send_json({k: bank_entries(k) for k in BANKS})
            elif path.startswith("/api/mind/"):
                key = path.rsplit("/", 1)[1]
                if key not in MIND_FILES:
                    return self.send_json({"error": "unknown file"}, 404)
                with state_lock:
                    self.send_json({"key": key, "name": MIND_FILES[key][0], "text": read_mind(key)})
            elif path == "/api/events":
                with state_lock:
                    self.send_json(read_events())
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            data = self.body()
            if path == "/api/chat":
                return self.stream_chat(data.get("text", "").strip())
            if path == "/api/setup":
                return self.send_json(do_setup(data))
            if path == "/api/settings":
                return self.send_json(do_settings(data))
            if path == "/api/action":
                if data.get("action") not in ("memory", "distill", "memory_day", "new_context"):
                    return self.send_json({"error": "unknown action"}, 400)
                if model_lock.locked():
                    return self.send_json({"error": "Your companion is busy. Try again in a moment."}, 409)
                run_action_async(data["action"])
                return self.send_json({"ok": True})
            if path == "/api/name":
                return self.send_json(do_name(data.get("name", "").strip()))
            if path.startswith("/api/mind/"):
                key = path.rsplit("/", 1)[1]
                if key not in MIND_FILES:
                    return self.send_json({"error": "unknown file"}, 404)
                with state_lock:
                    backup_mind(f"edit_{key}")
                    write_mind(key, data.get("text", ""))
                log(f"Mind {key} edited by hand.", "info")
                return self.send_json({"ok": True})
            if path == "/api/events":
                return self.send_json(do_event(data))
            self.send_json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    def stream_chat(self, text):
        if not text:
            return self.send_json({"error": "empty message"}, 400)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def write(chunk):
            try:
                self.wfile.write(chunk.encode())
                self.wfile.flush()
            except OSError:
                pass  # browser closed; keep generating so the reply is still saved
        chat(text, write)


def do_setup(data):
    with state_lock:
        at = now()
        state["user_name"] = data.get("user_name", "").strip()
        state["model"] = data.get("model") or state["model"]
        state["birthday"] = state["birthday"] or iso(at)
        roadmap = dict(data.get("roadmap") or {})
        roadmap["Users Name"] = state["user_name"]
        set_roadmap_fields(roadmap)
        name = data.get("companion_name", "").strip()
        if name:
            state["companion_name"] = name
        if "DOB" in read_mind("L1"):
            set_l1_start_date(parse(state["birthday"]), name)
        state["setup_done"] = True
        save_state()
    log(f"Setup complete. {state['companion_name'] or 'Your companion'} was born {stamp(parse(state['birthday']))}.", "info")
    return {"ok": True}


def do_settings(data):
    ints = {"num_ctx": (2048, 262144), "memory_interval_min": (5, 1440),
            "day_end_hour": (0, 23), "memory_day_every_days": (1, 60)}
    with state_lock:
        for k, (lo, hi) in ints.items():
            if k in data:
                state[k] = max(lo, min(hi, int(data[k])))
        for k in ("model", "user_name", "companion_name"):
            if k in data and str(data[k]).strip():
                state[k] = str(data[k]).strip()
        if "think" in data:
            state["think"] = bool(data["think"])
        save_state()
    log("Settings saved.", "info")
    return {"ok": True}


def do_name(name):
    if not name:
        return {"error": "Please enter a name."}
    with state_lock:
        state["companion_name"] = name
        set_l1_start_date(parse(state["birthday"]) or now(), name)
        save_state()
    log(f"Your companion is named {name}.", "memory")
    run_action_async("memory")  # store the naming moment, as the guide suggests
    return {"ok": True}


def do_event(data):
    with state_lock:
        events = read_events()
        if data.get("delete"):
            events = [e for e in events if e["id"] != data["delete"]]
        else:
            desc = (data.get("description") or "").strip()
            when = (data.get("when") or "").strip().replace("T", " ")
            if not desc or not when:
                return {"error": "An event needs a date/time and a description."}
            nums = [int(e["id"]) for e in events if e["id"].isdigit()]
            events.append({"id": f"{(max(nums) + 1) if nums else 1:03d}", "when": when, "description": desc})
            events.sort(key=lambda e: e["when"])
        write_events(events)
    return {"ok": True}


def main():
    ensure_data_dir()
    load_state()
    save_state()
    threading.Thread(target=scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Soul and Song Companion is running at {url}")
    print(f"Mind files: {DATA_DIR}")
    print("Close this window to stop the app.")
    if os.environ.get("COMPANION_NO_BROWSER") != "1":
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
