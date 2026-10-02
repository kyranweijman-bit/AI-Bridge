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


def _clean_param(p):
    """Postgres `text` columns can't store the NUL character (\\x00) - psycopg
    refuses the whole insert before it even reaches the database if any
    string parameter contains one. It shows up most often in PDF-extracted
    text, but this is a blanket safety net so no future caller has to
    remember to strip it themselves. Non-string params (including bytes,
    e.g. original_bytes) are passed through untouched."""
    return p.replace("\x00", "") if isinstance(p, str) else p


def _query(sql, params=None):
    """Run one SQL statement and return its rows as dicts (or [] if none)."""
    if params:
        params = tuple(_clean_param(p) for p in params) if not isinstance(params, dict) \
            else {k: _clean_param(v) for k, v in params.items()}
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


def branch_chat(source_chat_id, up_to_turn_index, new_title=None):
    """Branching conversations: fork a chat at any message. Copies every
    message and file row with turn_index <= up_to_turn_index into a brand
    new chat, so the two chats can then diverge independently - the
    original is completely untouched. Returns the new chat's id."""
    source_rows = _query("select title from chats where id = %s", (source_chat_id,))
    source_title = source_rows[0]["title"] if source_rows else "Chat"
    title = new_title or f"{source_title} (branch)"

    new_id = str(uuid.uuid4())
    with _get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("insert into chats (id, title) values (%s, %s)", (new_id, title))
            conn.execute(
                """insert into messages
                   (chat_id, turn_index, mode, kind, model, role, round,
                    content, context, in_context, meta)
                   select %s, turn_index, mode, kind, model, role, round,
                          content, context, in_context, meta
                   from messages where chat_id = %s and turn_index <= %s""",
                (new_id, source_chat_id, up_to_turn_index),
            )
            conn.execute(
                """insert into files (chat_id, turn_index, filename, storage_path, extracted_text, original_bytes)
                   select %s, turn_index, filename, storage_path, extracted_text, original_bytes
                   from files where chat_id = %s and turn_index <= %s""",
                (new_id, source_chat_id, up_to_turn_index),
            )
    return new_id


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
        result = {
            "type": "single",
            "question": question["content"],
            "primary": primary,
            "claude": combined.get("Claude", ""),
            "chatgpt": combined.get("ChatGPT", ""),
        }

    elif mode == "independent":
        answers = by_model("answer")
        comparison = next((r["content"] for r in rows if r["kind"] == "comparison"), "")
        result = {
            "type": "independent",
            "question": question["content"],
            "claude": answers.get("Claude", ""),
            "chatgpt": answers.get("ChatGPT", ""),
            "comparison": comparison,
        }

    elif mode == "debate":
        result = {
            "type": "debate",
            "question": question["content"],
            "transcript": [(r["model"], r["content"]) for r in rows if r["kind"] == "debate"],
            "claude_role": meta.get("claude_role"),
            "chatgpt_role": meta.get("chatgpt_role"),
        }

    elif mode == "recipe":
        # Step 29: same shape as debate, except each step's label (Draft /
        # Critique / Revise / Final check) varies per message rather than
        # being one constant role per speaker - so it's read back from each
        # row's own "role" column instead of from `meta`.
        result = {
            "type": "recipe",
            "question": question["content"],
            "transcript": [(r["model"], r["role"], r["content"]) for r in rows if r["kind"] == "recipe"],
        }

    else:
        answer = next((r for r in rows if r["kind"] == "answer"), None)
        result = {
            "type": "solo",
            "question": question["content"],
            "model": answer["model"] if answer else meta.get("model", "Claude"),
            "answer": answer["content"] if answer else "",
        }

    # Step 22: a final conclusion, if one was generated for this turn, rides
    # along as an extra field on top of whatever mode-specific shape above -
    # every turn type can carry one, generated after the fact by a button
    # click rather than as part of the turn's own rows.
    conclusion = next((r["content"] for r in rows if r["kind"] == "conclusion"), None)
    if conclusion is not None:
        result["conclusion"] = conclusion

    # Same pattern as conclusion above: a disagreement map or a third-option
    # suggestion, if one was generated for this turn by its button.
    disagreement_map = next((r["content"] for r in rows if r["kind"] == "disagreement_map"), None)
    if disagreement_map is not None:
        result["disagreement_map"] = disagreement_map

    third_option = next((r["content"] for r in rows if r["kind"] == "third_option"), None)
    if third_option is not None:
        result["third_option"] = third_option

    # "Edit my file": each edited file rides as its own extra row (like
    # conclusion/disagreement_map above) - "role" holds which original
    # filename it's replacing, "model" holds which model produced it (None
    # for the modes with one single, sourceless final answer).
    file_edits = [
        {"source": r["model"], "filename": r["role"], "edited_text": r["content"]}
        for r in rows if r["kind"] == "file_edit"
    ]
    if file_edits:
        result["file_edits"] = file_edits

    # Quick wins: confidence tags / blind judging were on for this turn -
    # carried in `meta` (see _turn_to_rows) since both apply regardless of mode.
    if meta.get("confidence_tags"):
        result["confidence_tags"] = True
    if meta.get("blind_judging"):
        result["blind_judging"] = True

    return result


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
    # Quick wins: confidence tags and blind judging both apply regardless of
    # mode, so they ride in the question row's `meta` alongside whatever
    # mode-specific meta each branch below already builds (read back in
    # _rows_to_turn above).
    extra_meta = {}
    if turn.get("confidence_tags"):
        extra_meta["confidence_tags"] = True
    if turn.get("blind_judging"):
        extra_meta["blind_judging"] = True

    if mode == "single":
        primary = turn.get("primary", "Claude")
        reviewer = "ChatGPT" if primary == "Claude" else "Claude"
        first = turn["claude"] if primary == "Claude" else turn["chatgpt"]
        second = turn["chatgpt"] if primary == "Claude" else turn["claude"]
        return [
            {**q, "in_context": True, "meta": {"primary": primary, **extra_meta}},
            {"mode": mode, "kind": "answer", "model": primary, "content": first, "in_context": True},
            {"mode": mode, "kind": "review", "model": reviewer, "content": second},
        ]

    if mode == "independent":
        return [
            {**q, "in_context": True, "meta": extra_meta},
            {"mode": mode, "kind": "answer", "model": "Claude", "content": turn["claude"]},
            {"mode": mode, "kind": "answer", "model": "ChatGPT", "content": turn["chatgpt"]},
            {"mode": mode, "kind": "comparison", "model": "Claude", "content": turn["comparison"], "in_context": True},
        ]

    if mode == "debate":
        roles = {"Claude": turn.get("claude_role"), "ChatGPT": turn.get("chatgpt_role")}
        rows = [{**q, "meta": {"claude_role": roles["Claude"], "chatgpt_role": roles["ChatGPT"], **extra_meta}}]
        for i, (speaker, reply) in enumerate(turn["transcript"]):
            rows.append({"mode": mode, "kind": "debate", "model": speaker,
                         "role": roles.get(speaker), "round": i + 1, "content": reply})
        return rows

    if mode == "recipe":
        rows = [{**q, "meta": extra_meta}]
        for i, (speaker, label, reply) in enumerate(turn["transcript"]):
            rows.append({"mode": mode, "kind": "recipe", "model": speaker,
                         "role": label, "round": i + 1, "content": reply})
        return rows

    return [
        {**q, "in_context": True, "meta": {"model": turn["model"], **extra_meta}},
        {"mode": mode, "kind": "answer", "model": turn["model"], "content": turn["answer"], "in_context": True},
    ]


def add_extra(chat_id, turn_index, mode, kind, model, content, in_context=False, role=None):
    """Step 22/30: append one more row onto an *existing* turn - a generated
    conclusion, for instance - rather than a new question+answer turn of its
    own. Reuses the same `messages` table and turn_index as the turn it
    belongs to, just with its own `kind` (e.g. "conclusion").

    `role` is free for the kind to reuse as it likes - "edit my file"
    (Built 48) stores the original filename there for kind="file_edit" rows,
    since several can share one turn (one per attached file) and need
    telling apart."""
    _query(
        """insert into messages (chat_id, turn_index, mode, kind, model, role, content, in_context)
           values (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (chat_id, turn_index, mode, kind, model, role, content, in_context),
    )
    _query("update chats set updated_at = now() where id = %s", (chat_id,))


def export_chat_text(chat_id):
    """Step 25: the whole conversation as one readable Markdown document -
    every question, its attachments, and every answer/review/comparison/
    debate-or-recipe reply/conclusion, in order. Built straight from the
    same rows load_chat() already reads, so it always matches what's shown
    on screen."""
    chat = load_chat(chat_id)
    if chat is None:
        return ""

    files_by_turn = {}
    for f in list_files(chat_id):
        files_by_turn.setdefault(f["turn_index"], []).append(f["filename"])

    lines = [f"# {chat['title']}", ""]
    for i, turn in enumerate(chat["display"]):
        lines.append(f"## Question {i + 1}")
        lines.append(turn["question"])
        if files_by_turn.get(i):
            lines.append("")
            lines.append(f"*Attached: {', '.join(files_by_turn[i])}*")
        lines.append("")

        t = turn.get("type", "single")
        if t == "single":
            primary = turn.get("primary", "Claude")
            reviewer = "ChatGPT" if primary == "Claude" else "Claude"
            lines += [f"**{primary} (answered first):**", turn["claude"] if primary == "Claude" else turn["chatgpt"], ""]
            lines += [f"**{reviewer} (reviewed):**", turn["chatgpt"] if primary == "Claude" else turn["claude"], ""]
        elif t == "independent":
            lines += ["**Claude:**", turn["claude"], "", "**ChatGPT:**", turn["chatgpt"], "",
                      "**Comparison:**", turn["comparison"], ""]
        elif t == "solo":
            lines += [f"**{turn['model']}:**", turn["answer"], ""]
        elif t in ("debate", "recipe"):
            for entry in turn["transcript"]:
                if len(entry) == 3:
                    speaker, label, reply = entry
                    lines.append(f"**{speaker} ({label}):**")
                else:
                    speaker, reply = entry
                    lines.append(f"**{speaker}:**")
                lines += [reply, ""]

        if turn.get("conclusion"):
            lines += ["**Conclusion:**", turn["conclusion"], ""]
        if turn.get("disagreement_map"):
            lines += ["**Disagreement map:**", turn["disagreement_map"], ""]
        if turn.get("third_option"):
            lines += ["**A third option:**", turn["third_option"], ""]

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


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


# ---- Settings (spending limit) ----

def get_setting(key, default=None):
    rows = _query("select value from settings where key = %s", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key, value):
    _query(
        """insert into settings (key, value) values (%s, %s)
           on conflict (key) do update set value = excluded.value, updated_at = now()""",
        (key, value),
    )


# ---- Blind-judging votes ----

def add_vote(chat_id, turn_index, winner):
    _query(
        "insert into votes (chat_id, turn_index, winner) values (%s, %s, %s)",
        (chat_id, turn_index, winner),
    )


def get_vote(chat_id, turn_index):
    """The winner already recorded for this turn, or None if nobody's voted
    yet - used to decide whether to still show it blind or reveal it."""
    rows = _query(
        "select winner from votes where chat_id = %s and turn_index = %s order by id desc limit 1",
        (chat_id, turn_index),
    )
    return rows[0]["winner"] if rows else None


def vote_stats():
    """Model-vs-model stats dashboard: overall blind-judging win counts.
    Per-topic breakdown isn't built yet - that needs a way to classify what
    each question was about first."""
    rows = _query("select winner, count(*) as n from votes group by winner order by n desc")
    return {r["winner"]: int(r["n"]) for r in rows}


# ---- Saveable personas ----

def list_personas():
    return _query("select id, name, instruction from personas order by name")


def add_persona(name, instruction):
    _query(
        """insert into personas (name, instruction) values (%s, %s)
           on conflict (name) do update set instruction = excluded.instruction""",
        (name.strip(), instruction.strip()),
    )


def delete_persona(persona_id):
    _query("delete from personas where id = %s", (persona_id,))


# ---- Editable working brief ----

def get_brief(chat_id):
    rows = _query("select content from briefs where chat_id = %s", (chat_id,))
    return rows[0]["content"] if rows else ""


def set_brief(chat_id, content):
    content = content.strip()
    if not content:
        _query("delete from briefs where chat_id = %s", (chat_id,))
        return
    _query(
        """insert into briefs (chat_id, content) values (%s, %s)
           on conflict (chat_id) do update set content = excluded.content, updated_at = now()""",
        (chat_id, content),
    )


# ---- Decision journal ----

def add_decision(chat_id, turn_index, choice, reasoning=""):
    _query(
        "insert into decisions (chat_id, turn_index, choice, reasoning) values (%s, %s, %s, %s)",
        (chat_id, turn_index, choice, reasoning),
    )


def list_decisions(chat_id=None):
    """All decisions, or just one chat's - newest first."""
    if chat_id is None:
        return _query("select d.*, c.title from decisions d join chats c on c.id = d.chat_id order by d.created_at desc")
    return _query("select * from decisions where chat_id = %s order by created_at desc", (chat_id,))


def set_decision_outcome(decision_id, outcome):
    _query("update decisions set outcome = %s, updated_at = now() where id = %s", (outcome, decision_id))


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

def save_file(chat_id, turn_index, filename, extracted_text, file_bytes=None, keep_original=False):
    """Store the file's text in the database and, if Supabase Storage is
    configured (SUPABASE_URL + SUPABASE_SERVICE_KEY), the original file too.
    Returns a warning string if the upload failed, otherwise None.

    keep_original=True additionally keeps a copy of file_bytes right here in
    Postgres (the new `original_bytes` column) - used only for a file
    attached with "edit my file" turned on, so its .docx styling or .pdf
    page size can be carried over into the edited download later, without
    depending on Supabase Storage being configured at all."""
    storage_path, warning = None, None
    if file_bytes is not None:
        try:
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
            storage_path = _upload_to_storage(f"{chat_id}/{turn_index}_{safe_name}", file_bytes)
        except Exception as e:
            warning = f"The original file couldn't be stored ({e}); its text was still saved."
    original_bytes = file_bytes if (keep_original and file_bytes is not None) else None
    _query(
        """insert into files (chat_id, turn_index, filename, storage_path, extracted_text, original_bytes)
           values (%s, %s, %s, %s, %s, %s)""",
        (chat_id, turn_index, filename, storage_path, extracted_text, original_bytes),
    )
    return warning


def list_files(chat_id):
    return _query(
        "select id, turn_index, filename, storage_path from files where chat_id = %s order by id",
        (chat_id,),
    )


def get_original_file_bytes(chat_id, turn_index, filename):
    """The original bytes kept for a file attached with keep_original=True,
    or None if there isn't one (not kept, or several files share this exact
    name/turn - picks the most recently saved one either way)."""
    rows = _query(
        """select original_bytes from files
           where chat_id = %s and turn_index = %s and filename = %s and original_bytes is not null
           order by id desc limit 1""",
        (chat_id, turn_index, filename),
    )
    return bytes(rows[0]["original_bytes"]) if rows else None


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
