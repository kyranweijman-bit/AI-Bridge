"""
Step 11: a Claude-style interface - a list of separate chats on the left,
file upload, and download buttons on every answer.

Install requirements first:  pip install -r requirements.txt
Run it with:                 streamlit run app.py

Streamlit reruns this whole file top-to-bottom on every click. That's why
state that needs to survive a click (which chat is open, its messages)
either lives in the database (see db.py) or in st.session_state, which
Streamlit itself preserves across reruns.
"""

import os
import streamlit as st
from dotenv import load_dotenv

MAX_TOTAL_FILE_CHARS = 60000  # combined cap across every file attached to one question, to control cost

# Load .env right away, before the password check below runs - bridge.py
# also calls this, but that import happens *after* the password gate, which
# meant APP_PASSWORD wasn't in os.environ yet when it was needed. Calling it
# here too fixes that (load_dotenv() only fills in variables that aren't
# already set, so calling it twice is harmless).
load_dotenv()


# ---- Step 12: pull secrets from Streamlit Cloud's secrets store, if present ----
#
# Locally, bridge.py's load_dotenv() reads .env instead - .env is never
# uploaded to GitHub, so it isn't available once hosted. On Streamlit Cloud,
# there is no .env file; instead you paste the same values into the app's
# "Secrets" box in its dashboard, and Streamlit exposes them as st.secrets.
# This copies them into the normal environment variables so bridge.py
# doesn't need to know or care which situation it's running in.
try:
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_MODEL", "OPENAI_MODEL", "APP_PASSWORD",
                "DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
        if key in st.secrets:
            # .strip() guards against a stray newline or trailing space that
            # can sneak in when copy-pasting a long key - those are invisible
            # to look at but break the actual API request.
            os.environ.setdefault(key, str(st.secrets[key]).strip())
except Exception:
    pass  # no secrets configured (e.g. running locally with just a .env file) - that's fine


# ---- Step 12: a simple password gate ----
#
# Without this, anyone who gets hold of the URL could ask questions on your
# API keys' bill. st.session_state.authenticated is what remembers "this
# browser already typed the right password" across reruns, for the rest of
# this browser tab's session.
def check_password():
    def password_entered():
        st.session_state.authenticated = (
            st.session_state.get("password_input") == os.environ.get("APP_PASSWORD")
        )

    if st.session_state.get("authenticated"):
        return True

    st.text_input("Password", type="password", on_change=password_entered, key="password_input")
    if st.session_state.get("authenticated") is False:
        st.error("Wrong password.")
    return False


st.set_page_config(page_title="AI Bridge", layout="wide")

if not check_password():
    st.stop()  # halts the script here - nothing below this line ever runs without the password

import bridge
import db
from bridge import (
    ask_claude_stream,
    ask_chatgpt_stream,
    ROLE_INSTRUCTIONS,
    REVIEW_INSTRUCTION,
    build_compare_prompt,
    build_debate_message,
    build_conclusion_prompt,
    FOLLOWUP_PRESETS,
    OTHER_MODEL_INSTRUCTION,
    PROMPT_PRESETS,
    RECIPE_STEPS,
)
from pypdf import PdfReader
from docx import Document


# ---- Step 24: multi-file upload with passage references ----
#
# Each file is broken into tagged chunks - by page for PDFs, by a group of
# paragraphs for .docx (which has no fixed notion of a "page"), by a group
# of lines for anything else - so the model can point back at *where* in
# the file something came from, e.g. "[report.pdf, page 3]", instead of
# treating the whole document as one undifferentiated blob of text.

def extract_tagged_text(uploaded_file):
    name = uploaded_file.name

    if name.lower().endswith(".pdf"):
        reader = PdfReader(uploaded_file)
        chunks = []
        for i, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            if text:
                chunks.append(f"[{name}, page {i + 1}]\n{text}")
        return "\n\n".join(chunks)

    if name.lower().endswith(".docx"):
        document = Document(uploaded_file)
        paragraphs = [p.text for p in document.paragraphs]
        group_size = 20
        chunks = []
        for start in range(0, len(paragraphs), group_size):
            group = paragraphs[start:start + group_size]
            text = "\n".join(group).strip()
            if text:
                chunks.append(f"[{name}, paragraphs {start + 1}-{start + len(group)}]\n{text}")
        return "\n\n".join(chunks)

    text = uploaded_file.read().decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return text
    group_size = 40
    chunks = []
    for start in range(0, len(lines), group_size):
        group = lines[start:start + group_size]
        chunk_text = "\n".join(group).strip()
        if chunk_text:
            chunks.append(f"[{name}, lines {start + 1}-{start + len(group)}]\n{chunk_text}")
    return "\n\n".join(chunks) if chunks else text


def build_files_prompt(uploaded_files, chat_id, turn_index):
    """Extracts, tags and caps every attached file's text, saves each one to
    the database, and returns the combined block to prepend to the prompt.
    Any st.warning() calls happen here, right where the cap is applied."""
    tagged_parts, total_len, truncated = [], 0, False

    for uf in uploaded_files:
        text = extract_tagged_text(uf)
        remaining = MAX_TOTAL_FILE_CHARS - total_len
        if remaining <= 0:
            truncated = True
            break
        if len(text) > remaining:
            text = text[:remaining]
            truncated = True
        tagged_parts.append(text)
        total_len += len(text)

        warning = db.save_file(chat_id, turn_index, uf.name, text, uf.getvalue())
        if warning:
            st.warning(warning)

    if truncated:
        st.warning(f"Attached files were longer than {MAX_TOTAL_FILE_CHARS:,} characters combined - only the start was sent.")

    names = ", ".join(uf.name for uf in uploaded_files)
    return (
        f"The user attached {len(uploaded_files)} file(s): {names}.\n"
        "Each passage below is tagged with where it came from, like "
        "'[filename, page N]' - when you rely on specific content from "
        "these files, cite the tag it came from.\n\n"
        f"{chr(10).join(tagged_parts)}"
    )


# ---- Step 21: storage lives in a database (db.py) ----

@st.cache_resource
def connect_database():
    """Runs once per app start (not on every click): opens the connection
    and creates any missing tables."""
    db.init_schema()
    return True


try:
    connect_database()
except Exception as e:
    st.error(f"Couldn't connect to the database: {e}")
    st.info("Check DATABASE_URL in .env (locally) or in the app's Secrets on Streamlit Cloud. See SETUP_DATABASE.md.")
    st.stop()


def _log_usage(provider, model, input_tokens, output_tokens, cost):
    # st.session_state here is the session of whoever pressed Ask, because
    # this is called from inside that session's own script run.
    db.log_usage(provider, model, input_tokens, output_tokens, cost,
                 chat_id=st.session_state.get("current_chat_id"))


bridge.usage_callback = _log_usage


# ---- Pick which chat is open ----

if "current_chat_id" not in st.session_state:
    existing = db.list_chats()
    st.session_state.current_chat_id = existing[0][0] if existing else db.create_chat()

# Step 26: the multi-step flow currently running (if any) - which steps
# already succeeded, and which one (if any) just failed and is waiting to
# be retried. Step 22/27/30 reuse the same pattern for one-off actions
# (a conclusion, a follow-up, "add to memory") that don't need retry
# machinery of their own since they're a single call, not several in a row.
if "in_progress" not in st.session_state:
    st.session_state.in_progress = None
if "pending_action" not in st.session_state:
    st.session_state.pending_action = None


# ---- Sidebar: the chat list ----

st.sidebar.title("Chats")

if st.sidebar.button("+ New chat", use_container_width=True):
    st.session_state.current_chat_id = db.create_chat()
    st.session_state.in_progress = None
    st.rerun()

search = st.sidebar.text_input("Search chats", placeholder="Search titles and messages")

st.sidebar.divider()

chat_list = db.search_chats(search) if search else db.list_chats()
for chat_id, title in chat_list:
    label = title or "New chat"
    if chat_id == st.session_state.current_chat_id:
        label = f"-> {label}"

    col1, col2 = st.sidebar.columns([5, 1])
    with col1:
        if st.button(label, key=f"open_{chat_id}", use_container_width=True):
            st.session_state.current_chat_id = chat_id
            st.session_state.in_progress = None
            st.rerun()
    with col2:
        if st.button("x", key=f"del_{chat_id}"):
            db.delete_chat(chat_id)
            if st.session_state.current_chat_id == chat_id:
                remaining = db.list_chats()
                st.session_state.current_chat_id = remaining[0][0] if remaining else db.create_chat()
            st.rerun()

if search and not chat_list:
    st.sidebar.caption("No chats match.")


# ---- Step 21: memory ----
#
# Memories are sent to both models as a standing instruction on every call.
# "All chats" ones apply everywhere; "This chat only" ones just here.
st.sidebar.divider()
with st.sidebar.expander("Memory"):
    for m in db.list_memories(st.session_state.current_chat_id):
        scope = "all chats" if m["chat_id"] is None else "this chat"
        mcol1, mcol2 = st.columns([5, 1])
        mcol1.caption(f"{m['content']}  \n*({scope})*")
        if mcol2.button("x", key=f"mem_del_{m['id']}"):
            db.delete_memory(m["id"])
            st.rerun()

    with st.form("add_memory", clear_on_submit=True):
        new_memory = st.text_area("Remember...", placeholder="e.g. I study at HAN; answer in English")
        memory_scope = st.radio("Applies to", ["All chats", "This chat only"], horizontal=True)
        if st.form_submit_button("Save to memory") and new_memory.strip():
            db.add_memory(
                new_memory,
                chat_id=None if memory_scope == "All chats" else st.session_state.current_chat_id,
            )
            st.rerun()


# ---- Step 16/21: cost visibility, now saved permanently ----
st.sidebar.divider()
with st.sidebar.expander("Usage"):
    usage = db.usage_summary(st.session_state.current_chat_id)
    st.write(f"Today: ≈ ${usage['today_cost']:.4f}")
    st.write(f"This chat: ≈ ${usage['chat_cost']:.4f}")
    st.markdown(f"**All time: ≈ ${usage['total_cost']:.4f}**")
    st.caption(f"{usage['total_tokens']:,} tokens in total")
    for row in db.usage_by_model():
        cost = f"≈ ${float(row['cost']):.4f}" if row["cost"] is not None else "no price known"
        st.caption(f"{row['model']}: {int(row['input_tokens']):,} in / {int(row['output_tokens']):,} out, {cost}")
    if usage["unpriced_calls"]:
        st.caption("Some calls have no cost estimate - their model isn't in the PRICING table in bridge.py.")


# ---- Main area: the open chat ----

chat = db.load_chat(st.session_state.current_chat_id)
if chat is None:
    # The chat this browser tab remembered was deleted (e.g. in another
    # tab) - start a fresh one instead of crashing.
    st.session_state.current_chat_id = db.create_chat()
    chat = db.load_chat(st.session_state.current_chat_id)

chat_id = st.session_state.current_chat_id
turn_index = len(chat["display"])
memory = db.memory_prompt(chat_id)  # sent to both models as a system prompt


def turn_transcript_text(turn):
    """Step 22/30: the whole "discussion" a turn represents, as plain text -
    used both to ask for a final conclusion and to fill in a quick
    "add to memory" save with something more useful than a raw dump."""
    t = turn.get("type", "single")
    if t == "single":
        primary = turn.get("primary", "Claude")
        reviewer = "ChatGPT" if primary == "Claude" else "Claude"
        first = turn["claude"] if primary == "Claude" else turn["chatgpt"]
        second = turn["chatgpt"] if primary == "Claude" else turn["claude"]
        return f"{primary} answered:\n{first}\n\n{reviewer} reviewed:\n{second}"
    if t == "independent":
        return (
            f"Claude answered:\n{turn['claude']}\n\n"
            f"ChatGPT answered:\n{turn['chatgpt']}\n\n"
            f"Comparison:\n{turn['comparison']}"
        )
    if t in ("debate", "recipe"):
        parts = []
        for entry in turn["transcript"]:
            if len(entry) == 3:
                speaker, label, reply = entry
                parts.append(f"{speaker} ({label}): {reply}")
            else:
                speaker, reply = entry
                parts.append(f"{speaker}: {reply}")
        return "\n\n".join(parts)
    return turn.get("answer", "")  # solo


title_col, pin_col = st.columns([8, 1])
title_col.title(chat["title"])
with pin_col:
    if st.button("Unpin" if chat["pinned"] else "Pin", help="Pinned chats stay at the top of the list"):
        db.set_pinned(chat_id, not chat["pinned"])
        st.rerun()

with st.expander("Rename chat"):
    new_title = st.text_input("New name", value=chat["title"], key=f"rename_{chat_id}")
    if st.button("Save name") and new_title.strip():
        db.rename_chat(chat_id, new_title.strip()[:100])
        st.rerun()

# Step 25: export the whole conversation - every question, its attachments,
# and every answer/review/comparison/debate-or-recipe reply/conclusion - as
# one readable Markdown file, instead of only individual answers.
st.download_button(
    "Export whole conversation",
    db.export_chat_text(chat_id),
    file_name=f"{(chat['title'] or 'chat').strip()[:50]}.md",
    key="export_chat",
)


# =====================================================================
# Either: show the "ask a new question" form (mode picker, presets, etc.)
# and handle any one-off action a button lower down just queued up - or,
# if a multi-step flow is currently running/paused on an error, show its
# progress/retry controls instead. Never both at once, so a half-finished
# flow can't be interrupted by starting a second one.
# =====================================================================

if st.session_state.in_progress is None:

    uploaded_files = st.file_uploader("Attach file(s) (optional)", accept_multiple_files=True)
    question = st.text_input("Ask a question")

    # Step 29: one-click templates. Picking one fixes the mode (and roles,
    # for Debate) to whatever suits that kind of question, and prepends a
    # short instruction to whatever you actually typed - your own question
    # is always kept, never replaced. Pick "None" for full manual control.
    preset_name = st.selectbox("Quick start (optional)", ["None"] + list(PROMPT_PRESETS.keys()))
    preset = PROMPT_PRESETS.get(preset_name)

    MODE_OPTIONS = ["Single review", "Independent answers", "Debate",
                    "Recipe: Draft -> Critique -> Revise -> Check", "One model only"]

    primary = rounds = claude_role = chatgpt_role = which_model = debate_start = None

    if preset:
        mode = preset["mode"]
        primary = preset.get("primary", "Claude")
        rounds = preset.get("rounds", 3)
        claude_role = preset.get("claude_role", "Proposer")
        chatgpt_role = preset.get("chatgpt_role", "Critic")
        which_model = preset.get("which_model", "Claude")
        debate_start = "Claude"
        detail = f" ({primary} first)" if mode == "Single review" else (f" ({which_model})" if mode == "One model only" else "")
        st.caption(f"Preset **{preset_name}** -> {mode}{detail}.")
    else:
        mode = st.radio("Mode", MODE_OPTIONS, horizontal=True)
        if mode == "Single review":
            primary = st.radio("Who answers first?", ["Claude", "ChatGPT"], horizontal=True)
        elif mode == "Debate":
            rounds = st.slider("Rounds", min_value=1, max_value=6, value=3)
            debate_start = st.radio("Who starts?", ["Claude", "ChatGPT"], horizontal=True)
            role_col1, role_col2 = st.columns(2)
            with role_col1:
                claude_role = st.selectbox("Claude's role", ["Proposer", "Critic", "Fact-checker"], index=0)
            with role_col2:
                chatgpt_role = st.selectbox("ChatGPT's role", ["Proposer", "Critic", "Fact-checker"], index=1)
        elif mode == "One model only":
            which_model = st.radio("Which model?", ["Claude", "ChatGPT"], horizontal=True)

    if st.button("Ask") and question:
        prompt = question

        if uploaded_files:
            files_block = build_files_prompt(uploaded_files, chat_id, turn_index)
            prompt = f"{files_block}\n\nQuestion: {question}"

        if preset:
            prompt = f"{preset['prefix']}\n\n{prompt}"

        st.session_state.in_progress = {
            "mode": mode, "question": question, "prompt": prompt, "turn_index": turn_index,
            "primary": primary, "rounds": rounds, "claude_role": claude_role,
            "chatgpt_role": chatgpt_role, "which_model": which_model, "debate_start": debate_start,
            "steps": {}, "error": None,
        }
        st.rerun()

    # ---- Step 22/27/30: run whichever one-off action a button queued up ----
    pending = st.session_state.pending_action
    if pending is not None:
        st.session_state.pending_action = None
        target = chat["display"][pending["turn_index"]] if pending["turn_index"] < len(chat["display"]) else None

        if target is None:
            pass  # the turn it referred to is gone (e.g. chat changed) - just drop it

        elif pending["kind"] == "conclusion":
            discussion = turn_transcript_text(target)
            conclusion_prompt = build_conclusion_prompt(target["question"], discussion)
            try:
                st.markdown("**Conclusion:**")
                conclusion_text = st.write_stream(
                    ask_claude_stream([{"role": "user", "content": conclusion_prompt}], system=memory)
                )
                db.add_extra(chat_id, pending["turn_index"], target.get("type", "single"),
                             "conclusion", "Claude", conclusion_text, in_context=True)
            except Exception as e:
                st.error(f"Couldn't generate a conclusion: {e}")
            st.rerun()

        elif pending["kind"] == "memory":
            text = (target.get("conclusion") or turn_transcript_text(target))[:2000]
            db.add_memory(text, chat_id=chat_id)
            st.rerun()

        elif pending["kind"] == "followup":
            t = target.get("type", "single")
            if t == "solo":
                acting_model = target["model"]
            elif t == "single":
                acting_model = target.get("primary", "Claude")
            else:  # independent - the comparison (last thing in shared history) is always Claude's
                acting_model = "Claude"

            action = pending["action"]
            if action == "other_model":
                speak_model = "ChatGPT" if acting_model == "Claude" else "Claude"
                instruction = OTHER_MODEL_INSTRUCTION
            else:
                speak_model = acting_model
                instruction = FOLLOWUP_PRESETS[action]

            followup_messages = chat["history"] + [{"role": "user", "content": instruction}]
            try:
                st.markdown(f"**{speak_model}:**")
                stream = ask_claude_stream(followup_messages, system=memory) if speak_model == "Claude" \
                    else ask_chatgpt_stream(followup_messages, system=memory)
                answer = st.write_stream(stream)
                new_turn = {"type": "solo", "question": instruction, "model": speak_model, "answer": answer}
                db.add_turn(chat_id, turn_index, new_turn, instruction)
            except Exception as e:
                st.error(f"That follow-up failed: {e}")
            st.rerun()

else:
    # ---- Step 26: a multi-step flow is running, or paused on a failed step ----
    progress = st.session_state.in_progress

    if progress.get("error"):
        st.error(f"That step failed: {progress['error']}")
        retry_col, cancel_col = st.columns(2)
        if retry_col.button("Retry"):
            progress["error"] = None
            st.session_state.in_progress = progress
            st.rerun()
        if cancel_col.button("Cancel this question"):
            st.session_state.in_progress = None
            st.rerun()
        st.stop()  # don't attempt anything else until Retry is pressed

    def run_step(step_key, label, make_stream):
        """Runs one model call within the flow below. A step that already
        succeeded (on an earlier attempt, before something else failed)
        replays instantly from cache instead of calling the model again -
        only the step that actually failed gets retried. A step that raises
        stops the whole script run here; the error banner above picks it
        up on the next run."""
        st.markdown(label)
        if step_key in progress["steps"]:
            st.write(progress["steps"][step_key])
            return progress["steps"][step_key]
        try:
            result = st.write_stream(make_stream())
        except Exception as e:
            progress["error"] = str(e)
            st.session_state.in_progress = progress
            st.rerun()
        progress["steps"][step_key] = result
        st.session_state.in_progress = progress
        return result

    mode = progress["mode"]
    question = progress["question"]
    prompt = progress["prompt"]
    flow_turn_index = progress["turn_index"]
    new_turn = None

    if mode == "Single review":
        primary = progress["primary"]
        history_ctx = chat["history"] + [{"role": "user", "content": prompt}]
        first_answer = run_step(
            "first", f"**{primary}** (answering):",
            lambda: (ask_claude_stream if primary == "Claude" else ask_chatgpt_stream)(history_ctx, system=memory),
        )
        reviewer = "ChatGPT" if primary == "Claude" else "Claude"
        reviewer_messages = history_ctx + [
            {"role": "assistant", "content": first_answer},
            {"role": "user", "content": REVIEW_INSTRUCTION},
        ]
        second_answer = run_step(
            "second", f"**{reviewer}** (reviewing):",
            lambda: (ask_chatgpt_stream if reviewer == "ChatGPT" else ask_claude_stream)(reviewer_messages, system=memory),
        )
        new_turn = {
            "type": "single", "question": question, "primary": primary,
            "claude": first_answer if primary == "Claude" else second_answer,
            "chatgpt": first_answer if primary == "ChatGPT" else second_answer,
        }

    elif mode == "Independent answers":
        history_ctx = chat["history"] + [{"role": "user", "content": prompt}]
        claude_answer = run_step("claude", "**Claude** (answering independently):",
                                  lambda: ask_claude_stream(history_ctx, system=memory))
        chatgpt_answer = run_step("chatgpt", "**ChatGPT** (answering independently):",
                                   lambda: ask_chatgpt_stream(history_ctx, system=memory))
        compare_prompt = build_compare_prompt(question, claude_answer, chatgpt_answer)
        comparison = run_step("comparison", "**Comparison:**",
                               lambda: ask_claude_stream([{"role": "user", "content": compare_prompt}], system=memory))
        new_turn = {
            "type": "independent", "question": question,
            "claude": claude_answer, "chatgpt": chatgpt_answer, "comparison": comparison,
        }

    elif mode == "Debate":
        rounds = progress["rounds"]
        roles = {"claude": progress["claude_role"], "chatgpt": progress["chatgpt_role"]}
        speaker = "claude" if progress["debate_start"] == "Claude" else "chatgpt"
        transcript = []
        for i in range(rounds):
            speaker_name = "Claude" if speaker == "claude" else "ChatGPT"
            role_instruction = ROLE_INSTRUCTIONS[roles[speaker]]
            message = build_debate_message(prompt, transcript, role_instruction)
            reply = run_step(
                f"round_{i}", f"**{speaker_name}** (round {i + 1}, {roles[speaker]}):",
                (lambda m=message, s=speaker: (ask_claude_stream if s == "claude" else ask_chatgpt_stream)(
                    [{"role": "user", "content": m}], system=memory)),
            )
            transcript.append((speaker_name, reply))
            speaker = "chatgpt" if speaker == "claude" else "claude"
        new_turn = {
            "type": "debate", "question": question, "transcript": transcript,
            "claude_role": progress["claude_role"], "chatgpt_role": progress["chatgpt_role"],
        }

    elif mode.startswith("Recipe"):
        transcript = []
        for i, (speaker, label, instruction) in enumerate(RECIPE_STEPS):
            speaker_name = "Claude" if speaker == "claude" else "ChatGPT"
            so_far = [(s, r) for s, _l, r in transcript]
            message = build_debate_message(prompt, so_far, instruction)
            reply = run_step(
                f"recipe_{i}", f"**{speaker_name}** ({label}):",
                (lambda m=message, s=speaker: (ask_claude_stream if s == "claude" else ask_chatgpt_stream)(
                    [{"role": "user", "content": m}], system=memory)),
            )
            transcript.append((speaker_name, label, reply))
        new_turn = {"type": "recipe", "question": question, "transcript": transcript}

    else:  # One model only
        which_model = progress["which_model"]
        history_ctx = chat["history"] + [{"role": "user", "content": prompt}]
        answer = run_step(
            "answer", f"**{which_model}:**",
            lambda: (ask_claude_stream if which_model == "Claude" else ask_chatgpt_stream)(history_ctx, system=memory),
        )
        new_turn = {"type": "solo", "question": question, "model": which_model, "answer": answer}

    # Every step succeeded - persist the turn and clear the in-progress state.
    db.add_turn(chat_id, flow_turn_index, new_turn, prompt)
    if chat["title"] == "New chat":
        db.rename_chat(chat_id, question[:40])
    st.session_state.in_progress = None
    st.rerun()

st.divider()

# Show the conversation so far, oldest first, each answer with its own
# download button. Older chats saved before this mode toggle existed have
# no "type" key, so they're treated as "single" too.
last_index = len(chat["display"]) - 1

for i, turn in enumerate(chat["display"]):
    st.markdown(f"**You:** {turn['question']}")

    turn_type = turn.get("type", "single")

    if turn_type == "single":
        primary = turn.get("primary", "Claude")  # chats saved before this existed default to Claude-first
        claude_label = "answered first" if primary == "Claude" else "reviewed"
        chatgpt_label = "answered first" if primary == "ChatGPT" else "reviewed"

        st.markdown(f"**Claude** ({claude_label}):")
        st.write(turn["claude"])
        st.download_button("Download Claude's answer", turn["claude"], file_name=f"claude_answer_{i}.txt", key=f"dl_claude_{i}")

        st.markdown(f"**ChatGPT** ({chatgpt_label}):")
        st.write(turn["chatgpt"])
        st.download_button("Download ChatGPT's answer", turn["chatgpt"], file_name=f"chatgpt_answer_{i}.txt", key=f"dl_chatgpt_{i}")

    elif turn_type == "independent":
        # Quick win 4: the two independent answers side by side, so the
        # differences the comparison talks about are easy to scan across.
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**Claude** (answered independently):")
            st.write(turn["claude"])
            st.download_button("Download Claude's answer", turn["claude"], file_name=f"claude_answer_{i}.txt", key=f"dl_ind_claude_{i}")
        with col_b:
            st.markdown("**ChatGPT** (answered independently):")
            st.write(turn["chatgpt"])
            st.download_button("Download ChatGPT's answer", turn["chatgpt"], file_name=f"chatgpt_answer_{i}.txt", key=f"dl_ind_chatgpt_{i}")

        st.markdown("**Key differences & comparison:**")
        st.write(turn["comparison"])
        st.download_button("Download comparison", turn["comparison"], file_name=f"comparison_{i}.txt", key=f"dl_ind_compare_{i}")

    elif turn_type == "solo":
        st.markdown(f"**{turn['model']}:**")
        st.write(turn["answer"])
        st.download_button(f"Download {turn['model']}'s answer", turn["answer"], file_name=f"{turn['model'].lower()}_answer_{i}.txt", key=f"dl_solo_{i}")

    else:  # debate or recipe
        for j, entry in enumerate(turn["transcript"]):
            if turn_type == "recipe":
                speaker, label, reply = entry
                header = f"**{speaker} ({label}):**"
                dl_label = f"Download step {j + 1} ({speaker})"
            else:
                speaker, reply = entry
                role = turn.get("claude_role") if speaker == "Claude" else turn.get("chatgpt_role")
                role_label = f", {role}" if role else ""
                header = f"**{speaker} (round {j + 1}{role_label}):**"
                dl_label = f"Download round {j + 1} ({speaker})"
            st.markdown(header)
            st.write(reply)
            st.download_button(dl_label, reply, file_name=f"{turn_type}_{i}_{j + 1}_{speaker.lower()}.txt", key=f"dl_{turn_type}_{i}_{j}")

    if turn.get("conclusion"):
        st.markdown("**Conclusion:**")
        st.write(turn["conclusion"])
        st.download_button("Download conclusion", turn["conclusion"], file_name=f"conclusion_{i}.txt", key=f"dl_conclusion_{i}")

    # Step 22/30: available on every past turn - a conclusion, or saving its
    # gist to memory, doesn't depend on what happened afterward.
    action_col1, action_col2 = st.columns(2)
    can_act = st.session_state.in_progress is None
    if action_col1.button("Get final conclusion", key=f"concl_{i}", disabled=not can_act or bool(turn.get("conclusion"))):
        st.session_state.pending_action = {"kind": "conclusion", "turn_index": i}
        st.rerun()
    if action_col2.button("Add to memory", key=f"mem_{i}", disabled=not can_act):
        st.session_state.pending_action = {"kind": "memory", "turn_index": i}
        st.rerun()

    # Step 27: "continue from this answer" - only offered on the most recent
    # turn, and only for the modes that actually feed into the shared
    # history (debate/recipe are their own separate side conversation, so
    # there's nothing coherent for a follow-up to build on there).
    if turn_type in ("solo", "single", "independent") and i == last_index:
        st.caption("Continue from this answer:")
        fc = st.columns(4)
        for col, (action_key, label) in zip(fc, [
            ("explain", "Explain this"),
            ("challenge", "Challenge this"),
            ("practical", "Make it practical"),
            ("other_model", "Ask the other model"),
        ]):
            if col.button(label, key=f"followup_{i}_{action_key}", disabled=not can_act):
                st.session_state.pending_action = {"kind": "followup", "action": action_key, "based_on_index": i, "turn_index": i}
                st.rerun()

    st.divider()
