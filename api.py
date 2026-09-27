"""
API / webhook endpoint (roadmap: "bigger additions for later" #12).

Lets another tool - a Home Assistant automation, a Discord bot, a phone
shortcut - send a question and get the combined Claude+ChatGPT answer back,
without opening the Streamlit app at all.

IMPORTANT - this is deliberately a SEPARATE script from app.py, not something
added to the Streamlit app itself. Streamlit Community Cloud only runs one
process (the Streamlit app) and doesn't expose a way to also run a second web
server alongside it, so this can't be hosted there too. Run it wherever you'd
run a small local service instead - your own machine, a Raspberry Pi, or
anywhere else on your home network reachable by whatever is calling it (that
fits neatly with the separate Home Assistant setup, if that's where a call
would come from).

Setup (same .env as the Streamlit app - it reads the same ANTHROPIC_API_KEY /
OPENAI_API_KEY / CLAUDE_MODEL / OPENAI_MODEL / DATABASE_URL):
    pip install fastapi uvicorn
    Add API_KEY=<pick-any-long-random-string> to .env
    uvicorn api:app --host 0.0.0.0 --port 8000

Then, from anywhere on your network:
    curl -X POST http://<this-machine>:8000/ask \
         -H "X-API-Key: <your API_KEY>" \
         -H "Content-Type: application/json" \
         -d '{"question": "Is it going to rain today?", "mode": "solo_claude"}'

Every call is logged to the same usage_log table as the Streamlit app (so it
counts toward the same spending-limit tracking), and optionally saved into an
existing chat (pass "chat_id") so it shows up in the Streamlit sidebar too -
handy for "ask from your phone, read the full answer later on the app."
"""

import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import bridge
import db

app = FastAPI(title="AI Bridge API", description=__doc__)

db.init_schema()


def _log_usage(provider, model, input_tokens, output_tokens, cost):
    db.log_usage(provider, model, input_tokens, output_tokens, cost, chat_id=None)


bridge.usage_callback = _log_usage


def _check_api_key(x_api_key: Optional[str]):
    expected = os.environ.get("API_KEY", "").strip()
    if not expected:
        raise HTTPException(500, "API_KEY isn't set in .env on this machine - add one before using this endpoint.")
    if x_api_key != expected:
        raise HTTPException(401, "Missing or wrong X-API-Key header.")


class AskRequest(BaseModel):
    question: str
    # solo_claude | solo_chatgpt | single | independent | debate
    mode: str = "solo_claude"
    rounds: int = 3          # only used for mode="debate"
    chat_id: Optional[str] = None  # if given, the answer is also saved into this existing chat


@app.post("/ask")
def ask(body: AskRequest, x_api_key: Optional[str] = Header(default=None)):
    _check_api_key(x_api_key)

    question = body.question.strip()
    if not question:
        raise HTTPException(400, "question can't be empty.")

    if body.mode == "solo_claude":
        answer = bridge.ask_claude([{"role": "user", "content": question}])
        result = {"mode": body.mode, "answer": answer}
        turn = {"type": "solo", "question": question, "model": "Claude", "answer": answer}

    elif body.mode == "solo_chatgpt":
        answer = bridge.ask_chatgpt([{"role": "user", "content": question}])
        result = {"mode": body.mode, "answer": answer}
        turn = {"type": "solo", "question": question, "model": "ChatGPT", "answer": answer}

    elif body.mode == "single":
        claude_answer, chatgpt_review = bridge.second_opinion(question)
        result = {"mode": body.mode, "claude": claude_answer, "chatgpt": chatgpt_review}
        turn = {"type": "single", "question": question, "primary": "Claude",
                "claude": claude_answer, "chatgpt": chatgpt_review}

    elif body.mode == "independent":
        claude_answer, chatgpt_answer, comparison = bridge.independent_answers(
            [{"role": "user", "content": question}], question)
        result = {"mode": body.mode, "claude": claude_answer, "chatgpt": chatgpt_answer, "comparison": comparison}
        turn = {"type": "independent", "question": question, "claude": claude_answer,
                "chatgpt": chatgpt_answer, "comparison": comparison}

    elif body.mode == "debate":
        rounds = max(1, min(body.rounds, 6))
        transcript = bridge.debate(question, rounds=rounds)
        result = {"mode": body.mode, "transcript": [{"speaker": s, "reply": r} for s, r in transcript]}
        turn = {"type": "debate", "question": question, "transcript": transcript,
                "claude_role": "Proposer", "chatgpt_role": "Critic"}

    else:
        raise HTTPException(400, "mode must be one of: solo_claude, solo_chatgpt, single, independent, debate.")

    if body.chat_id:
        chat = db.load_chat(body.chat_id)
        if chat is None:
            raise HTTPException(404, f"No chat with id {body.chat_id!r}.")
        turn_index = len(chat["display"])
        db.add_turn(body.chat_id, turn_index, turn, question)
        result["saved_to_chat_id"] = body.chat_id

    return result


@app.get("/chats")
def list_chats(x_api_key: Optional[str] = Header(default=None)):
    """So a calling tool can look up a chat_id to pass to /ask, without
    opening the Streamlit app."""
    _check_api_key(x_api_key)
    return [{"id": chat_id, "title": title} for chat_id, title in db.list_chats()]


@app.get("/health")
def health():
    return {"status": "ok"}
