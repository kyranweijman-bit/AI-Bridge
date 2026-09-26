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

from bridge import ask_claude, ask_chatgpt


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

if st.button("Ask") and question:
    prompt = question

    if uploaded_file is not None:
        text = uploaded_file.read().decode("utf-8", errors="replace")
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS]
            st.warning(f"File was longer than {MAX_FILE_CHARS} characters - only the start was sent.")
        prompt = f"The user attached a file named '{uploaded_file.name}':\n\n{text}\n\nQuestion: {question}"

    chat["history"].append({"role": "user", "content": prompt})

    with st.spinner("Asking Claude..."):
        claude_answer = ask_claude(chat["history"])
    chat["history"].append({"role": "assistant", "content": claude_answer})

    with st.spinner("Asking ChatGPT for a second opinion..."):
        review_prompt = (
            "Another AI answered the question below. "
            "Point out mistakes or gaps, then give your own improved answer.\n\n"
            f"Question: {question}\n\nAnswer:\n{claude_answer}"
        )
        chatgpt_answer = ask_chatgpt([{"role": "user", "content": review_prompt}])

    chat["display"].append({"question": question, "claude": claude_answer, "chatgpt": chatgpt_answer})

    if chat["title"] == "New chat":
        chat["title"] = question[:40]

    save_chat(st.session_state.current_chat_id, chat)
    st.rerun()

st.divider()

# Show the conversation so far, oldest first, each answer with its own
# download button.
for i, turn in enumerate(chat["display"]):
    st.markdown(f"**You:** {turn['question']}")

    st.markdown("**Claude:**")
    st.write(turn["claude"])
    st.download_button(
        "Download Claude's answer",
        turn["claude"],
        file_name=f"claude_answer_{i}.txt",
        key=f"dl_claude_{i}",
    )

    st.markdown("**ChatGPT's review:**")
    st.write(turn["chatgpt"])
    st.download_button(
        "Download ChatGPT's review",
        turn["chatgpt"],
        file_name=f"chatgpt_review_{i}.txt",
        key=f"dl_chatgpt_{i}",
    )

    st.divider()
