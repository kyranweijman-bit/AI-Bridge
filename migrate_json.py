"""
Step 21: one-time copy of your old chats (chats/*.json) into the database.

Run it once, locally, after DATABASE_URL is in your .env:
    python migrate_json.py

It skips any chat that's already in the database, so running it twice
doesn't create duplicates. The JSON files themselves are left untouched.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import db

CHATS_DIR = Path(__file__).with_name("chats")


def migrate():
    db.init_schema()
    existing = {chat_id for chat_id, _ in db.list_chats()}
    files = sorted(CHATS_DIR.glob("*.json"), key=os.path.getmtime)
    if not files:
        print("No chats/*.json files found - nothing to migrate.")
        return

    copied = 0
    for path in files:
        chat_id = path.stem
        if chat_id in existing:
            print(f"Skipped (already in database): {path.name}")
            continue

        data = json.loads(path.read_text(encoding="utf-8"))
        title = data.get("title", "New chat")

        # The old "history" list holds what the models were actually sent,
        # including attached-file text. Match its user messages, in order,
        # to the turns that added one (every mode except debate).
        user_prompts = iter(m["content"] for m in data.get("history", []) if m["role"] == "user")

        db._query("insert into chats (id, title) values (%s, %s)", (chat_id, title))
        for i, turn in enumerate(data.get("display", [])):
            turn.setdefault("type", "single")
            if turn["type"] == "debate":
                # JSON stored the transcript as lists; the app expects pairs.
                turn["transcript"] = [tuple(pair) for pair in turn["transcript"]]
                prompt = None
            else:
                prompt = next(user_prompts, None)
            db.add_turn(chat_id, i, turn, prompt)

        copied += 1
        print(f"Copied: {title} ({len(data.get('display', []))} turns)")

    print(f"\nDone - {copied} chat(s) copied.")


if __name__ == "__main__":
    migrate()
