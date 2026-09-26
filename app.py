"""
Step 11: a Claude-style interface - a list of separate chats on the left,
file upload, and download buttons on every answer.

Install requirements first:  pip install -r requirements.txt
Run it with:                 streamlit run app.py

Streamlit reruns this whole file top-to-bottom on every click. That's why
state that needs to survive a click (which chat is open, its messages)
either lives in a file on disk (chats/<id>.json) or in st.session_state,
which Streamlit itself preserves across reruns.
"""

import os
import json
import uuid
import streamlit as st
from dotenv import load_dotenv

CHATS_DIR = "chats"
MAX_FILE_CHARS = 20000  # cap how much of an uploaded file we send, to control cost

os.makedirs(CHATS_DIR, exist_ok=True)

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
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_MODEL", "OPENAI_MODEL", "APP_PASSWORD"):
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

from bridge import ask_claude, ask_chatgpt, debate, independent_answers, get_usage_summary
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


# ---- Chat storage: one small JSON file per chat in chats/ ----

def list_chats():
    """Return [(chat_id, title), ...] for every saved chat, most recently
    used first."""
    rows = []
    for filename in os.listdir(CHATS_DIR):
        if filename.endswith(".json"):
            path = os.path.join(CHATS_DIR, filename)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rows.append((filename[:-5], data.get("title", "New chat"), os.path.getmtime(path)))
    rows.sort(key=lambda r: r[2], reverse=True)
    return [(chat_id, title) for chat_id, title, _ in rows]


def load_chat(chat_id):
    """Returns None if this chat's file doesn't exist - which happens if
    the app's storage got reset (e.g. a redeploy or reboot) after a browser
    tab already had this chat open."""
    path = os.path.join(CHATS_DIR, f"{chat_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_chat(chat_id, chat):
    with open(os.path.join(CHATS_DIR, f"{chat_id}.json"), "w", encoding="utf-8") as f:
        json.dump(chat, f, indent=2, ensure_ascii=False)


def create_chat():
    chat_id = str(uuid.uuid4())
    # "history" is the Claude/OpenAI-format message list (used for follow-up
    # context). "display" is what we show on screen: each question plus
    # both answers, kept separately so we can render and download them.
    save_chat(chat_id, {"title": "New chat", "history": [], "display": []})
    return chat_id


# ---- Pick which chat is open ----

if "current_chat_id" not in st.session_state:
    existing = list_chats()
    st.session_state.current_chat_id = existing[0][0] if existing else create_chat()


# ---- Sidebar: the chat list ----

st.sidebar.title("Chats")

if st.sidebar.button("+ New chat", use_container_width=True):
    st.session_state.current_chat_id = create_chat()
    st.rerun()

st.sidebar.divider()

for chat_id, title in list_chats():
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
            os.remove(os.path.join(CHATS_DIR, f"{chat_id}.json"))
            if st.session_state.current_chat_id == chat_id:
                remaining = list_chats()
                st.session_state.current_chat_id = remaining[0][0] if remaining else create_chat()
            st.rerun()


# ---- Step 16: cost visibility ----
#
# usage_totals lives in bridge.py's memory, so this resets whenever the app
# process restarts (every redeploy, every local re-run) - it's "since this
# app last started," not a permanent lifetime total.
st.sidebar.divider()
with st.sidebar.expander("Usage since last restart"):
    usage = get_usage_summary()

    st.write(f"Claude: {usage['claude']['input_tokens']:,} in / {usage['claude']['output_tokens']:,} out")
    if usage["claude"]["cost"] is not None:
        st.write(f"≈ ${usage['claude']['cost']:.4f}")

    st.write(f"ChatGPT: {usage['openai']['input_tokens']:,} in / {usage['openai']['output_tokens']:,} out")
    if usage["openai"]["cost"] is not None:
        st.write(f"≈ ${usage['openai']['cost']:.4f}")

    if usage["total_cost"] is not None:
        st.markdown(f"**Total: ≈ ${usage['total_cost']:.4f}**")
    else:
        st.caption("No cost estimate - configured model isn't in the pricing table in bridge.py.")


# ---- Main area: the open chat ----

chat = load_chat(st.session_state.current_chat_id)
if chat is None:
    # The chat this browser tab remembered no longer exists on disk (storage
    # got reset) - start a fresh one instead of crashing.
    st.session_state.current_chat_id = create_chat()
    chat = load_chat(st.session_state.current_chat_id)

st.title(chat["title"])

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

    if mode == "Single review":
        # Step 17: both models get the same shared context - see
        # second_opinion()'s docstring in bridge.py for how the reviewer
        # now sees the full history instead of just question+answer.
        chat["history"].append({"role": "user", "content": prompt})

        with st.spinner(f"Asking {primary}..."):
            first_answer = ask_claude(chat["history"]) if primary == "Claude" else ask_chatgpt(chat["history"])
        chat["history"].append({"role": "assistant", "content": first_answer})

        reviewer = "ChatGPT" if primary == "Claude" else "Claude"
        reviewer_messages = chat["history"] + [{
            "role": "user",
            "content": "Critique the answer above - point out mistakes or gaps - then give your own improved answer.",
        }]
        with st.spinner(f"Asking {reviewer} for a second opinion..."):
            second_answer = ask_chatgpt(reviewer_messages) if reviewer == "ChatGPT" else ask_claude(reviewer_messages)

        chat["display"].append({
            "type": "single",
            "question": question,
            "primary": primary,
            "claude": first_answer if primary == "Claude" else second_answer,
            "chatgpt": first_answer if primary == "ChatGPT" else second_answer,
        })

    elif mode == "Independent answers":
        chat["history"].append({"role": "user", "content": prompt})

        with st.spinner("Claude and ChatGPT are answering independently..."):
            claude_answer, chatgpt_answer, comparison = independent_answers(chat["history"], question)

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
        # debate() doesn't touch chat["history"] - it's its own separate
        # side conversation between the two models, not part of the
        # ongoing shared context used for follow-up questions. It does
        # rebuild the full debate transcript + original question every
        # round internally - see bridge.py.
        with st.spinner("Claude and ChatGPT are going back and forth..."):
            transcript = debate(prompt, rounds=rounds, claude_role=claude_role, chatgpt_role=chatgpt_role)

        chat["display"].append({
            "type": "debate",
            "question": question,
            "transcript": transcript,
            "claude_role": claude_role,
            "chatgpt_role": chatgpt_role,
        })

    else:  # One model only
        chat["history"].append({"role": "user", "content": prompt})

        with st.spinner(f"Asking {which_model}..."):
            answer = ask_claude(chat["history"]) if which_model == "Claude" else ask_chatgpt(chat["history"])
        chat["history"].append({"role": "assistant", "content": answer})

        chat["display"].append({
            "type": "solo",
            "question": question,
            "model": which_model,
            "answer": answer,
        })

    if chat["title"] == "New chat":
        chat["title"] = question[:40]

    save_chat(st.session_state.current_chat_id, chat)
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
