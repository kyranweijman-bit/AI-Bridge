"""
Step 6: the actual bridge.

Both models read from and write to the same `history` list, and either one
can be asked a question. `second_opinion()` shows the two-way pattern:
Claude answers first, then ChatGPT is asked to review and improve on it.
"""

import os
import json
from dotenv import load_dotenv
from anthropic import Anthropic
from openai import OpenAI

load_dotenv()
# .strip() guards against a stray newline or trailing space in a copy-pasted
# key or model name - invisible to look at, but enough to break a request.
claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())
chatgpt = OpenAI(api_key=os.environ["OPENAI_API_KEY"].strip())

CLAUDE_MODEL = os.environ["CLAUDE_MODEL"].strip()
OPENAI_MODEL = os.environ["OPENAI_MODEL"].strip()

HISTORY_FILE = "history.json"

# Shared history: one list of {"role": ..., "content": ...} messages
# that both models get sent, so they're both "seeing" the same conversation.
history = []


def ask_claude(messages):
    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=800,
        messages=messages,
    )
    # Newer Claude models sometimes "think" before answering, which shows up
    # as an extra ThinkingBlock ahead of the actual answer in response.content.
    # So we look for the text block specifically, instead of assuming it's
    # always content[0].
    for block in response.content:
        if block.type == "text":
            return block.text
    return ""


def ask_chatgpt(messages):
    response = chatgpt.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
    )
    return response.choices[0].message.content


def ask(question, target="claude"):
    """Add a question to the shared history, get an answer from `target`,
    and save that answer back into the history."""
    history.append({"role": "user", "content": question})
    answer = ask_claude(history) if target == "claude" else ask_chatgpt(history)
    history.append({"role": "assistant", "content": answer})
    return answer


def second_opinion(question):
    """Claude answers, then ChatGPT is shown the question + Claude's answer
    and asked to critique and improve it. Swap the two calls below if you
    want ChatGPT to answer first instead."""
    first = ask(question, target="claude")

    review_prompt = (
        "Another AI answered the question below. "
        "Point out mistakes or gaps, then give your own improved answer.\n\n"
        f"Question: {question}\n\nAnswer:\n{first}"
    )
    second = ask_chatgpt([{"role": "user", "content": review_prompt}])
    return first, second


def debate(question, rounds=3):
    """Step 8: let the two models go back and forth instead of stopping
    after one review. `rounds` is a hard cap on how many replies get
    generated in total, so a run can never rack up an open-ended bill.

    Each model only sees the other's latest reply (not the whole shared
    `history`), so this is a separate side conversation from `ask()` /
    `second_opinion()` above."""
    transcript = []
    speaker = "claude"
    message = question

    for i in range(rounds):
        if speaker == "claude":
            reply = ask_claude([{"role": "user", "content": message}])
            speaker_name, next_speaker = "Claude", "chatgpt"
        else:
            reply = ask_chatgpt([{"role": "user", "content": message}])
            speaker_name, next_speaker = "ChatGPT", "claude"

        transcript.append((speaker_name, reply))
        print(f"\n--- Round {i + 1}: {speaker_name} ---\n{reply}")

        # Hand this reply to the other model as something to react to.
        message = (
            f"The other AI said:\n\n{reply}\n\n"
            "Respond to it - agree, disagree, or add something new."
        )
        speaker = next_speaker

    return transcript


def save_history(path=HISTORY_FILE):
    """Step 9: write the shared conversation to disk so it survives after
    the script closes. json.dump turns the Python list of dicts into text;
    indent=2 just makes the file readable if you open it yourself."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def load_history(path=HISTORY_FILE):
    """Load a previously saved conversation back into `history`, so a new
    run continues where the last one left off instead of starting blank."""
    global history
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)


if __name__ == "__main__":
    load_history()  # picks up where the last run left off, if history.json exists

    q = input("Your question: ")
    first, second = second_opinion(q)
    print("\n--- Claude ---\n", first)
    print("\n--- ChatGPT's review ---\n", second)

    save_history()
