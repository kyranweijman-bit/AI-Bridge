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

# Step 14: cost visibility. $ per million tokens, input/output, for models
# actually used here. Pricing changes over time and this isn't fetched live,
# so treat it as a good-faith estimate - check the providers' own pricing
# pages if you need an exact figure. Model names not listed here still work
# fine to ask questions, they just won't have a cost estimate shown.
PRICING = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-opus-5": {"input": 4.00, "output": 20.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-fable-5-1": {"input": 10.00, "output": 50.00},
    "gpt-5.4": {"input": 2.50, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.25, "output": 2.00},
    "gpt-5.5": {"input": 5.00, "output": 30.00},
}

# Running totals for this process's lifetime - resets when the app restarts.
usage_totals = {
    "claude_input_tokens": 0,
    "claude_output_tokens": 0,
    "openai_input_tokens": 0,
    "openai_output_tokens": 0,
}


def ask_claude(messages):
    response = claude.messages.create(
        # 4096 instead of 800: newer Claude models spend part of this budget
        # "thinking" before answering, and on a meaty question (reviewing a
        # whole document, say) that thinking alone can eat the entire old
        # 800-token cap, leaving nothing for the actual answer.
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=messages,
    )
    usage_totals["claude_input_tokens"] += response.usage.input_tokens
    usage_totals["claude_output_tokens"] += response.usage.output_tokens

    # Newer Claude models sometimes "think" before answering, which shows up
    # as an extra ThinkingBlock ahead of the actual answer in response.content.
    # So we look for the text block specifically, instead of assuming it's
    # always content[0].
    for block in response.content:
        if block.type == "text":
            return block.text

    # No text block at all - most likely max_tokens ran out mid-thought.
    # Returning a visible message here instead of "" means this shows up
    # clearly in the UI instead of looking like a silent, confusing blank.
    return "[Claude gave no answer text - it likely ran out of its token budget while thinking. Try a shorter question, or raise max_tokens in bridge.py.]"


def ask_chatgpt(messages):
    response = chatgpt.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
    )
    usage_totals["openai_input_tokens"] += response.usage.prompt_tokens
    usage_totals["openai_output_tokens"] += response.usage.completion_tokens

    return response.choices[0].message.content


def _cost(model_name, input_tokens, output_tokens):
    """Returns an estimated $ cost, or None if this model isn't in PRICING."""
    rates = PRICING.get(model_name)
    if rates is None:
        return None
    return (input_tokens / 1_000_000) * rates["input"] + (output_tokens / 1_000_000) * rates["output"]


def get_usage_summary():
    """Returns a dict with token counts and estimated $ cost so far, for
    each model, plus a combined total. Cost is None for an unrecognized
    model instead of silently showing $0."""
    claude_cost = _cost(CLAUDE_MODEL, usage_totals["claude_input_tokens"], usage_totals["claude_output_tokens"])
    openai_cost = _cost(OPENAI_MODEL, usage_totals["openai_input_tokens"], usage_totals["openai_output_tokens"])

    total_cost = None
    if claude_cost is not None or openai_cost is not None:
        total_cost = (claude_cost or 0) + (openai_cost or 0)

    return {
        "claude": {
            "input_tokens": usage_totals["claude_input_tokens"],
            "output_tokens": usage_totals["claude_output_tokens"],
            "cost": claude_cost,
        },
        "openai": {
            "input_tokens": usage_totals["openai_input_tokens"],
            "output_tokens": usage_totals["openai_output_tokens"],
            "cost": openai_cost,
        },
        "total_cost": total_cost,
    }


def ask(question, target="claude"):
    """Add a question to the shared history, get an answer from `target`,
    and save that answer back into the history."""
    history.append({"role": "user", "content": question})
    answer = ask_claude(history) if target == "claude" else ask_chatgpt(history)
    history.append({"role": "assistant", "content": answer})
    return answer


def second_opinion(question, primary="claude"):
    """One model answers, the other is shown the same shared context plus
    that answer, and asked to critique and improve it. `primary` picks who
    answers first - "claude" (default) or "chatgpt".

    Step 17: the reviewer gets `history` itself (the original question, any
    earlier conversation, everything the first model saw) plus one short
    instruction turn - not just a standalone question+answer pair - so its
    critique is grounded in the same information the first model had."""
    first = ask(question, target=primary)

    reviewer_messages = history + [{
        "role": "user",
        "content": "Critique the answer above - point out mistakes or gaps - then give your own improved answer.",
    }]
    reviewer = ask_chatgpt if primary == "claude" else ask_claude
    second = reviewer(reviewer_messages)
    return first, second


# Step 18: named roles a debater can be assigned. Applied as an instruction
# on every turn, so "Critic" actually behaves differently from "Proposer"
# instead of just reacting generically.
ROLE_INSTRUCTIONS = {
    "Proposer": "Propose an answer or idea, building on the discussion so far.",
    "Critic": "Critically challenge the previous statement - identify weaknesses, risks, or unstated assumptions.",
    "Fact-checker": "Check the factual accuracy of the previous statement - flag anything questionable or incorrect, and correct it if you can.",
}


def debate(question, rounds=3, claude_role="Proposer", chatgpt_role="Critic"):
    """Let the two models go back and forth, each playing an assigned role.
    `rounds` is a hard cap on how many replies get generated in total, so a
    run can never rack up an open-ended bill.

    Step 18: every round's message is rebuilt from scratch - the original
    question (which includes any attachment text, since that's baked into
    `question` before this is called), the full debate transcript so far,
    and this speaker's role instruction - instead of only the last reply.
    That keeps both models grounded in the same shared context turn to
    turn rather than slowly drifting off it. This is still a separate side
    conversation from `ask()` / `second_opinion()` above - it doesn't touch
    the shared `history` list."""
    roles = {"claude": claude_role, "chatgpt": chatgpt_role}
    transcript = []
    speaker = "claude"

    for i in range(rounds):
        speaker_name = "Claude" if speaker == "claude" else "ChatGPT"
        role_instruction = ROLE_INSTRUCTIONS[roles[speaker]]

        so_far = "\n\n".join(f"{name}: {reply}" for name, reply in transcript)
        message = f"Question: {question}\n\n"
        if so_far:
            message += f"Debate so far:\n{so_far}\n\n"
        message += f"Your role: {role_instruction}"

        if speaker == "claude":
            reply = ask_claude([{"role": "user", "content": message}])
            next_speaker = "chatgpt"
        else:
            reply = ask_chatgpt([{"role": "user", "content": message}])
            next_speaker = "claude"

        transcript.append((speaker_name, reply))
        print(f"\n--- Round {i + 1}: {speaker_name} ({roles[speaker]}) ---\n{reply}")
        speaker = next_speaker

    return transcript


def independent_answers(messages, question):
    """Step 19: both models answer using the exact same shared context
    (`messages` - the original question, any attachment, and the rest of
    the conversation so far), without seeing each other's answer first, so
    neither one's phrasing or assumptions can bias the other's. Afterward,
    one model (Claude, here) is asked to compare the two independent
    answers - where they agree, where they disagree, and which parts of
    each seem more reliable."""
    claude_answer = ask_claude(messages)
    chatgpt_answer = ask_chatgpt(messages)

    compare_prompt = (
        "Two AI models independently answered the question below, without "
        "seeing each other's response. Compare their answers: where do they "
        "agree, where do they disagree, and which parts of each seem more "
        "reliable?\n\n"
        f"Question: {question}\n\n"
        f"Model A (Claude):\n{claude_answer}\n\n"
        f"Model B (ChatGPT):\n{chatgpt_answer}"
    )
    comparison = ask_claude([{"role": "user", "content": compare_prompt}])
    return claude_answer, chatgpt_answer, comparison


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
