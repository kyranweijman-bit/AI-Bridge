"""
Step 21: permanent storage in a PostgreSQL database.

Everything that has to survive a redeploy or reboot - chats, messages,
memory, usage and attached files - goes through this one file. The app
connects with a standard DATABASE_URL, so the database can live on
Supabase today and be moved to any other Postgres host later by changing
only that one setting.

Chats are stored as individual rows (one per question, answer, review,
comparison or debate reply) rather than one big blob per chat. That makes
future features like searching past chats, per-model statistics and
branching conversations simple queries.
"""

import atexit
import os
import re
import uuid
from pathlib import Path

import httpx
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
FILES_BUCKET = "files"

_pool = None


def _get_pool():
    """Open the connection pool the first time it's needed. A pool (rather
    than one connection) matters because Streamlit serves several browser
    tabs at once, and one connection can't be shared between them safely."""
    global _pool
    if _pool is None:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError(
                "DATABASE_URL isn't set. Add it to .env (locally) or to the app's "
                "Secrets on Streamlit Cloud - see SETUP_DATABASE.md."
            )
        _pool = ConnectionPool(
            url,
            min_size=1,
            max_size=5,
            open=True,
            # prepare_threshold=None: Supabase's connection pooler doesn't
            # support "prepared statements", so switch them off.
            kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        )
    return _pool


def close():
    """Close the connection pool. Called automatically when Python exits,
    so scripts end cleanly instead of printing a shutdown error."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


atexit.register(close)


def _query(sql, params=None):
    """Run one SQL statement and return its rows as dicts (or [] if none)."""
    with _get_pool().connection() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def init_schema():
    """Create any missing tables. Safe to run on every app start."""
    with _get_pool().connection() as conn:
        conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))


# ---- Chats ----

def list_chats():
    """[(chat_id, title), ...] - pinned first, then most recently used."""
    rows = _query("select id, title from chats order by pinned desc, updated_at desc")
    return [(str(r["id"]), r["title"]) for r in rows]


def create_chat(title="New chat"):
    chat_id = str(uuid.uuid4())
    _query("insert into chats (id, title) values (%s, %s)", (chat_id, title))
    return chat_id


def delete_chat(chat_id):
    # Its messages, memories and files are removed with it ("on delete cascade").
    _query("delete from chats where id = %s", (chat_id,))


def rename_chat(chat_id, title):
    _query("update chats set title = %s, updated_at = now() where id = %s", (title, chat_id))


def set_pinned(chat_id, pinned):
    _query("update chats set pinned = %s where id = %s", (pinned, chat_id))


def load_chat(chat_id):
    """Returns {"title", "pinned", "history", "display"} in the same shape the
    app used with JSON files, or None if the chat doesn't exist (anymore).

    "history" is rebuilt from the rows marked in_context - it's what gets
    sent to the models as the conversation so far. "display" is rebuilt per
    turn, for showing on screen."""
    try:
        chat_rows = _query("select title, pinned from chats where id = %s", (chat_id,))
    except Exception:
        return None  # e.g. a malformed id
    if not chat_rows:
        return None

    rows = _query(
        "select * from messages where chat_id = %s order by turn_index, id",
        (chat_id,),
    )

    history = []
    for r in rows:
        if r["in_context"]:
            role = "user" if r["kind"] == "question" else "assistant"
            history.append({"role": role, "content": r["context"] or r["content"]})

    turns = {}
    for r in rows:
        turns.setdefault(r["turn_index"], []).append(r)
    display = [_rows_to_turn(turns[i]) for i in sorted(turns)]

    return {
        "title": chat_rows[0]["title"],
        "pinned": chat_rows[0]["pinned"],
        "history": history,
        "display": display,
    }


def _rows_to_turn(rows):
    """Turn the rows of one turn back into the dict the app renders."""
    question = next(r for r in rows if r["kind"] == "question")
    mode, meta = question["mode"], question["meta"] or {}
    by_model = lambda kind: {r["model"]: r["content"] for r in rows if r["kind"] == kind}

    if mode == "single":
        primary = meta.get("primary", "Claude")
        answer, review = by_model("answer"), by_model("review")
        combined = {**answer, **review}
        return {
            "type": "single",
            "question": question["content"],
            "primary": primary,
            "claude": combined.get("Claude", ""),
            "chatgpt": combined.get("ChatGPT", ""),
        }

    if mode == "independent":
        answers = by_model("answer")
        comparison = next((r["content"] for r in rows if r["kind"] == "comparison"), "")
        return {
            "type": "independent",
            "question": question["content"],
            "claude": answers.get("Claude", ""),
            "chatgpt": answers.get("ChatGPT", ""),
            "comparison": comparison,
        }

    if mode == "debate":
        return {
            "type": "debate",
            "question": question["content"],
            "transcript": [(r["model"], r["content"]) for r in rows if r["kind"] == "debate"],
            "claude_role": meta.get("claude_role"),
            "chatgpt_role": meta.get("chatgpt_role"),
        }

    answer = next((r for r in rows if r["kind"] == "answer"), None)
    return {
        "type": "solo",
        "question": question["content"],
        "model": answer["model"] if answer else meta.get("model", "Claude"),
        "answer": answer["content"] if answer else "",
    }


def add_turn(chat_id, turn_index, turn, prompt=None):
    """Save one finished turn (the dict the app builds after pressing Ask).
    `prompt` is what the models were actually sent - the question plus any
    attached file text - if that differs from the question itself."""
    rows = _turn_to_rows(turn, prompt)
    with _get_pool().connection() as conn:
        with conn.transaction():  # all rows of a turn are saved, or none
            for r in rows:
                conn.execute(
                    """insert into messages
                       (chat_id, turn_index, mode, kind, model, role, round,
                        content, context, in_context, meta)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        chat_id, turn_index, r["mode"], r["kind"], r.get("model"),
                        r.get("role"), r.get("round"), r["content"], r.get("context"),
                        r.get("in_context", False), Jsonb(r.get("meta", {})),
                    ),
                )
            conn.execute("update chats set updated_at = now() where id = %s", (chat_id,))


def _turn_to_rows(turn, prompt=None):
    mode = turn.get("type", "single")
    context = prompt if prompt and prompt != turn["question"] else None
    q = {"mode": mode, "kind": "question", "content": turn["question"], "context": context}

    if mode == "single":
        primary = turn.get("primary", "Claude")
        reviewer = "ChatGPT" if primary == "Claude" else "Claude"
        first = turn["claude"] if primary == "Claude" else turn["chatgpt"]
        second = turn["chatgpt"] if primary == "Claude" else turn["claude"]
        return [
            {**q, "in_context": True, "meta": {"primary": primary}},
            {"mode": mode, "kind": "answer", "model": primary, "content": first, "in_context": True},
            {"mode": mode, "kind": "review", "model": reviewer, "content": second},
        ]

    if mode == "independent":
        return [
            {**q, "in_context": True},
            {"mode": mode, "kind": "answer", "model": "Claude", "content": turn["claude"]},
            {"mode": mode, "kind": "answer", "model": "ChatGPT", "content": turn["chatgpt"]},
            {"mode": mode, "kind": "comparison", "model": "Claude", "content": turn["comparison"], "in_context": True},
        ]

    if mode == "debate":
        roles = {"Claude": turn.get("claude_role"), "ChatGPT": turn.get("chatgpt_role")}
        rows = [{**q, "meta": {"claude_role": roles["Claude"], "chatgpt_role": roles["ChatGPT"]}}]
        for i, (speaker, reply) in enumerate(turn["transcript"]):
            rows.append({"mode": mode, "kind": "debate", "model": speaker,
                         "role": roles.get(speaker), "round": i + 1, "content": reply})
        return rows

    return [
        {**q, "in_context": True, "meta": {"model": turn["model"]}},
        {"mode": mode, "kind": "answer", "model": turn["model"], "content": turn["answer"], "in_context": True},
    ]


def search_chats(text, limit=20):
    """Chats whose title or any message contains `text` (case-insensitive)."""
    pattern = f"%{text}%"
    rows = _query(
        """select c.id, c.title from chats c
           where c.title ilike %s
              or exists (select 1 from messages m where m.chat_id = c.id and m.content ilike %s)
           order by c.updated_at desc limit %s""",
        (pattern, pattern, limit),
    )
    return [(str(r["id"]), r["title"]) for r in rows]


# ---- Memory ----

def list_memories(chat_id=None):
    """Global memories plus, if chat_id is given, that chat's own ones."""
    return _query(
        """select id, chat_id, content from memories
           where chat_id is null or chat_id = %s order by id""",
        (chat_id,),
    )


def add_memory(content, chat_id=None):
    _query("insert into memories (chat_id, content) values (%s, %s)", (chat_id, content.strip()))


def delete_memory(memory_id):
    _query("delete from memories where id = %s", (memory_id,))


def memory_prompt(chat_id=None):
    """The memories as a system prompt for the models, or None if empty."""
    items = list_memories(chat_id)
    if not items:
        return None
    lines = "\n".join(f"- {m['content']}" for m in items)
    return (
        "The user has asked you to keep the following in mind in this "
        f"conversation:\n{lines}"
    )


# ---- Usage ----

def log_usage(provider, model, input_tokens, output_tokens, cost, chat_id=None):
    _query(
        """insert into usage_log (chat_id, provider, model, input_tokens, output_tokens, cost)
           values (%s, %s, %s, %s, %s, %s)""",
        (chat_id, provider, model, input_tokens, output_tokens, cost),
    )


def usage_summary(chat_id=None):
    """Token and cost totals for today, this chat, and all time. "Today"
    follows the Europe/Amsterdam calendar day."""
    rows = _query(
        """select
             coalesce(sum(cost) filter (where created_at >= date_trunc('day', now() at time zone 'Europe/Amsterdam') at time zone 'Europe/Amsterdam'), 0) as today_cost,
             coalesce(sum(cost) filter (where chat_id = %s), 0)  as chat_cost,
             coalesce(sum(cost), 0)                              as total_cost,
             coalesce(sum(input_tokens + output_tokens), 0)      as total_tokens,
             count(*) filter (where cost is null)                as unpriced_calls
           from usage_log""",
        (chat_id,),
    )
    r = rows[0]
    return {k: (float(v) if k.endswith("cost") else int(v)) for k, v in r.items()}


def usage_by_model():
    return _query(
        """select model, sum(input_tokens) as input_tokens, sum(output_tokens) as output_tokens,
                  sum(cost) as cost
           from usage_log group by model order by model"""
    )


# ---- Files ----

def save_file(chat_id, turn_index, filename, extracted_text, file_bytes=None):
    """Store the file's text in the database and, if Supabase Storage is
    configured (SUPABASE_URL + SUPABASE_SERVICE_KEY), the original file too.
    Returns a warning string if the upload failed, otherwise None."""
    storage_path, warning = None, None
    if file_bytes is not None:
        try:
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
            storage_path = _upload_to_storage(f"{chat_id}/{turn_index}_{safe_name}", file_bytes)
        except Exception as e:
            warning = f"The original file couldn't be stored ({e}); its text was still saved."
    _query(
        """insert into files (chat_id, turn_index, filename, storage_path, extracted_text)
           values (%s, %s, %s, %s, %s)""",
        (chat_id, turn_index, filename, storage_path, extracted_text),
    )
    return warning


def list_files(chat_id):
    return _query(
        "select id, turn_index, filename, storage_path from files where chat_id = %s order by id",
        (chat_id,),
    )


def _upload_to_storage(path, data):
    """Upload to Supabase Storage via its web API. Returns the stored path,
    or None when Storage isn't configured (that's optional)."""
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        return None
    response = httpx.post(
        f"{url}/storage/v1/object/{FILES_BUCKET}/{path}",
        content=data,
        headers={
            "Authorization": f"Bearer {key}",
            "apikey": key,
            "Content-Type": "application/octet-stream",
            "x-upsert": "true",
        },
        timeout=60,
    )
    response.raise_for_status()
    return path
