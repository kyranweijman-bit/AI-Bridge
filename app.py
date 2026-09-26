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

MAX_FILE_CHARS = 20000  # cap how much of an uploaded file we send, to control cost

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
)
from pypdf import PdfReader
from docx import Document


def extract_file_text(uploaded_file):
    """Step 15: .pdf and .docx are binary formats - decoding them as plain
    text like before would just produce garbage. This pulls readable text
    out of each format specifically, and still falls back to plain-text
    decoding for everything else (.txt, .py, .csv, .md, ...)."""
    name = uploaded_file.name.lower()

    if name.endswith(".pdf"):
        reader = PdfReader(uploaded_file)
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    if name.endswith(".docx"):
        document = Document(uploaded_file)
        return "\n".join(paragraph.text for paragraph in document.paragraphs)

    return uploaded_file.read().decode("utf-8", errors="replace")


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


# ---- Sidebar: the chat list ----

st.sidebar.title("Chats")

if st.sidebar.button("+ New chat", use_container_width=True):
    st.session_state.current_chat_id = db.create_chat()
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

uploaded_file = st.file_uploader("Attach a file (optional)")
question = st.text_input("Ask a question")

# Step 13/17/18/19: pick per-question how the two models are used.
mode = st.radio(
    "Mode",
    ["Single review", "Independent answers", "Debate", "One model only"],
    horizontal=True,
)

# Extra controls specific to whichever mode is selected.
if mode == "Single review":
    primary = st.radio("Who answers first?", ["Claude", "ChatGPT"], horizontal=True)
elif mode == "Debate":
    rounds = st.slider("Rounds", min_value=1, max_value=6, value=3)
    role_col1, role_col2 = st.columns(2)
    with role_col1:
        claude_role = st.selectbox("Claude's role", ["Proposer", "Critic", "Fact-checker"], index=0)
    with role_col2:
        chatgpt_role = st.selectbox("ChatGPT's role", ["Proposer", "Critic", "Fact-checker"], index=1)
elif mode == "One model only":
    which_model = st.radio("Which model?", ["Claude", "ChatGPT"], horizontal=True)

if st.button("Ask") and question:
    prompt = question

    if uploaded_file is not None:
        text = extract_file_text(uploaded_file)
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS]
            st.warning(f"File was longer than {MAX_FILE_CHARS} characters - only the start was sent.")
        prompt = f"The user attached a file named '{uploaded_file.name}':\n\n{text}\n\nQuestion: {question}"

        warning = db.save_file(chat_id, turn_index, uploaded_file.name, text, uploaded_file.getvalue())
        if warning:
            st.warning(warning)

    # Step 20: live streaming. Each answer below is shown with st.write_stream()
    # right here, in this same run, as it's generated - the words appear
    # progressively instead of a spinner followed by the full text all at
    # once. st.write_stream() also returns the complete text once the stream
    # ends, which is what gets saved into chat["history"] / chat["display"]
    # exactly like before. This doesn't change token usage or cost at all -
    # same request, same answer, just a different way of displaying it.

    if mode == "Single review":
        # Step 17: both models get the same shared context - see
        # second_opinion()'s docstring in bridge.py for how the reviewer
        # now sees the full history instead of just question+answer.
        chat["history"].append({"role": "user", "content": prompt})

        st.markdown(f"**{primary}** (answering):")
        stream = ask_claude_stream(chat["history"], system=memory) if primary == "Claude" else ask_chatgpt_stream(chat["history"], system=memory)
        first_answer = st.write_stream(stream)
        chat["history"].append({"role": "assistant", "content": first_answer})

        reviewer = "ChatGPT" if primary == "Claude" else "Claude"
        reviewer_messages = chat["history"] + [{"role": "user", "content": REVIEW_INSTRUCTION}]
        st.markdown(f"**{reviewer}** (reviewing):")
        stream = ask_chatgpt_stream(reviewer_messages, system=memory) if reviewer == "ChatGPT" else ask_claude_stream(reviewer_messages, system=memory)
        second_answer = st.write_stream(stream)

        chat["display"].append({
            "type": "single",
            "question": question,
            "primary": primary,
            "claude": first_answer if primary == "Claude" else second_answer,
            "chatgpt": first_answer if primary == "ChatGPT" else second_answer,
        })

    elif mode == "Independent answers":
        chat["history"].append({"role": "user", "content": prompt})

        st.markdown("**Claude** (answering independently):")
        claude_answer = st.write_stream(ask_claude_stream(chat["history"], system=memory))

        st.markdown("**ChatGPT** (answering independently):")
        chatgpt_answer = st.write_stream(ask_chatgpt_stream(chat["history"], system=memory))

        compare_prompt = build_compare_prompt(question, claude_answer, chatgpt_answer)
        st.markdown("**Comparison:**")
        comparison = st.write_stream(ask_claude_stream([{"role": "user", "content": compare_prompt}], system=memory))

        # The comparison (which references both answers) becomes this turn's
        # contribution to the shared history, so a follow-up question has a
        # single coherent thing to build on rather than two raw answers.
        chat["history"].append({"role": "assistant", "content": comparison})

        chat["display"].append({
            "type": "independent",
            "question": question,
            "claude": claude_answer,
            "chatgpt": chatgpt_answer,
            "comparison": comparison,
        })

    elif mode == "Debate":
        # This doesn't touch chat["history"] - it's its own separate side
        # conversation between the two models, not part of the ongoing
        # shared context used for follow-up questions. Each round rebuilds
        # the full transcript-so-far + original question from scratch (via
        # build_debate_message), same as bridge.py's non-streaming debate().
        roles = {"claude": claude_role, "chatgpt": chatgpt_role}
        transcript = []
        speaker = "claude"

        for i in range(rounds):
            speaker_name = "Claude" if speaker == "claude" else "ChatGPT"
            role_instruction = ROLE_INSTRUCTIONS[roles[speaker]]
            message = build_debate_message(prompt, transcript, role_instruction)

            st.markdown(f"**{speaker_name}** (round {i + 1}, {roles[speaker]}):")
            if speaker == "claude":
                reply = st.write_stream(ask_claude_stream([{"role": "user", "content": message}], system=memory))
                speaker = "chatgpt"
            else:
                reply = st.write_stream(ask_chatgpt_stream([{"role": "user", "content": message}], system=memory))
                speaker = "claude"

            transcript.append((speaker_name, reply))

        chat["display"].append({
            "type": "debate",
            "question": question,
            "transcript": transcript,
            "claude_role": claude_role,
            "chatgpt_role": chatgpt_role,
        })

    else:  # One model only
        chat["history"].append({"role": "user", "content": prompt})

        st.markdown(f"**{which_model}:**")
        stream = ask_claude_stream(chat["history"], system=memory) if which_model == "Claude" else ask_chatgpt_stream(chat["history"], system=memory)
        answer = st.write_stream(stream)
        chat["history"].append({"role": "assistant", "content": answer})

        chat["display"].append({
            "type": "solo",
            "question": question,
            "model": which_model,
            "answer": answer,
        })

    db.add_turn(chat_id, turn_index, chat["display"][-1], prompt)
    if chat["title"] == "New chat":
        db.rename_chat(chat_id, question[:40])

    st.rerun()

st.divider()

# Show the conversation so far, oldest first, each answer with its own
# download button. Older chats saved before this mode toggle existed have
# no "type" key, so they're treated as "single" too.
for i, turn in enumerate(chat["display"]):
    st.markdown(f"**You:** {turn['question']}")

    turn_type = turn.get("type", "single")

    if turn_type == "single":
        primary = turn.get("primary", "Claude")  # chats saved before this existed default to Claude-first
        claude_label = "answered first" if primary == "Claude" else "reviewed"
        chatgpt_label = "answered first" if primary == "ChatGPT" else "reviewed"

        st.markdown(f"**Claude** ({claude_label}):")
        st.write(turn["claude"])
        st.download_button(
            "Download Claude's answer",
            turn["claude"],
            file_name=f"claude_answer_{i}.txt",
            key=f"dl_claude_{i}",
        )

        st.markdown(f"**ChatGPT** ({chatgpt_label}):")
        st.write(turn["chatgpt"])
        st.download_button(
            "Download ChatGPT's answer",
            turn["chatgpt"],
            file_name=f"chatgpt_answer_{i}.txt",
            key=f"dl_chatgpt_{i}",
        )

    elif turn_type == "independent":
        st.markdown("**Claude** (answered independently):")
        st.write(turn["claude"])
        st.download_button(
            "Download Claude's answer",
            turn["claude"],
            file_name=f"claude_answer_{i}.txt",
            key=f"dl_ind_claude_{i}",
        )

        st.markdown("**ChatGPT** (answered independently):")
        st.write(turn["chatgpt"])
        st.download_button(
            "Download ChatGPT's answer",
            turn["chatgpt"],
            file_name=f"chatgpt_answer_{i}.txt",
            key=f"dl_ind_chatgpt_{i}",
        )

        st.markdown("**Comparison:**")
        st.write(turn["comparison"])
        st.download_button(
            "Download comparison",
            turn["comparison"],
            file_name=f"comparison_{i}.txt",
            key=f"dl_ind_compare_{i}",
        )

    elif turn_type == "solo":
        st.markdown(f"**{turn['model']}:**")
        st.write(turn["answer"])
        st.download_button(
            f"Download {turn['model']}'s answer",
            turn["answer"],
            file_name=f"{turn['model'].lower()}_answer_{i}.txt",
            key=f"dl_solo_{i}",
        )

    else:  # debate
        for j, (speaker, reply) in enumerate(turn["transcript"]):
            role = turn.get("claude_role") if speaker == "Claude" else turn.get("chatgpt_role")
            role_label = f", {role}" if role else ""
            st.markdown(f"**{speaker} (round {j + 1}{role_label}):**")
            st.write(reply)
            st.download_button(
                f"Download round {j + 1} ({speaker})",
                reply,
                file_name=f"debate_{i}_round{j + 1}_{speaker.lower()}.txt",
                key=f"dl_debate_{i}_{j}",
            )

    st.divider()
