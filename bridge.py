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

# Step 21: set by app.py to a function(provider, model, input_tokens,
# output_tokens, cost) that saves each call to the database. bridge.py itself
# doesn't know about the database - it just calls this if it's set.
usage_callback = None


def _record_usage(provider, model, input_tokens, output_tokens):
    if usage_callback is None:
        return
    try:
        usage_callback(provider, model, input_tokens, output_tokens,
                       _cost(model, input_tokens, output_tokens))
    except Exception as e:
        # Never let a logging problem break an answer.
        print(f"Couldn't log usage: {e}")


# Running totals for this process's lifetime - resets when the app restarts.
usage_totals = {
    "claude_input_tokens": 0,
    "claude_output_tokens": 0,
    "openai_input_tokens": 0,
    "openai_output_tokens": 0,
}


# 8192 instead of 800: newer Claude models spend part of this budget
# "thinking" before answering, and on a meaty or long-running task (reviewing
# a whole document, a debate several rounds deep where the transcript-so-far
# keeps growing) that thinking alone can eat a smaller cap entirely, leaving
# nothing for the actual answer. This is a ceiling, not a target - raising it
# doesn't cost more unless the model actually needed the extra room.
MAX_TOKENS = 8192

CUTOFF_NOTE = "\n\n[Answer cut off - it ran out of its token budget. Consider raising MAX_TOKENS in bridge.py.]"


# Step 21: every ask_* function takes an optional `system` - a standing
# instruction sent alongside the conversation. app.py uses it for memory.
# Claude takes it as a separate `system` parameter; OpenAI as a first
# message with role "system".

# ---- Image input: one neutral content format, adapted per provider ----
#
# A message's `content` is normally just a string. When an image is
# attached, app.py instead builds it as a small list of blocks in this
# app's own neutral shape - {"type": "text", "text": ...} or {"type":
# "image", "media_type": ..., "data": <base64>} - so the same shared
# `history` still works for both providers (the whole point of this app).
# Claude and OpenAI each want that image wrapped differently, so these two
# adapters translate it right before the request goes out; a plain string
# message passes through untouched.

def _to_claude_messages(messages):
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append(m)
            continue
        blocks = []
        for b in content:
            if b["type"] == "image":
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": b["media_type"], "data": b["data"]}})
            else:
                blocks.append({"type": "text", "text": b["text"]})
        out.append({"role": m["role"], "content": blocks})
    return out


def _to_openai_messages(messages):
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append(m)
            continue
        blocks = []
        for b in content:
            if b["type"] == "image":
                blocks.append({"type": "image_url", "image_url": {"url": f"data:{b['media_type']};base64,{b['data']}"}})
            else:
                blocks.append({"type": "text", "text": b["text"]})
        out.append({"role": m["role"], "content": blocks})
    return out


def _claude_kwargs(messages, system, max_tokens):
    kwargs = {"model": CLAUDE_MODEL, "max_tokens": max_tokens or MAX_TOKENS, "messages": _to_claude_messages(messages)}
    if system:
        kwargs["system"] = system
    return kwargs


def _openai_messages(messages, system):
    return ([{"role": "system", "content": system}] if system else []) + _to_openai_messages(messages)


def ask_claude(messages, system=None, max_tokens=None):
    response = claude.messages.create(**_claude_kwargs(messages, system, max_tokens))
    usage_totals["claude_input_tokens"] += response.usage.input_tokens
    usage_totals["claude_output_tokens"] += response.usage.output_tokens
    _record_usage("anthropic", CLAUDE_MODEL, response.usage.input_tokens, response.usage.output_tokens)

    # Newer Claude models sometimes "think" before answering, which shows up
    # as an extra ThinkingBlock ahead of the actual answer in response.content.
    # So we look for the text block specifically, instead of assuming it's
    # always content[0].
    for block in response.content:
        if block.type == "text":
            text = block.text
            # stop_reason == "max_tokens" means it was cut off mid-answer
            # (possibly mid-sentence) rather than finishing naturally -
            # flagging that visibly beats a truncated answer that silently
            # looks complete.
            if response.stop_reason == "max_tokens":
                text += CUTOFF_NOTE
            return text

    # No text block at all - most likely max_tokens ran out mid-thought,
    # before any answer text was produced. Returning a visible message here
    # instead of "" means this shows up clearly in the UI instead of looking
    # like a silent, confusing blank.
    return "[Claude gave no answer text - it ran out of its token budget while thinking. Try a shorter question, or raise MAX_TOKENS in bridge.py.]"


def ask_chatgpt(messages, system=None, max_tokens=None):
    response = chatgpt.chat.completions.create(
        model=OPENAI_MODEL,
        messages=_openai_messages(messages, system),
        max_completion_tokens=max_tokens or MAX_TOKENS,
    )
    usage_totals["openai_input_tokens"] += response.usage.prompt_tokens
    usage_totals["openai_output_tokens"] += response.usage.completion_tokens
    _record_usage("openai", OPENAI_MODEL, response.usage.prompt_tokens, response.usage.completion_tokens)

    text = response.choices[0].message.content or ""
    if response.choices[0].finish_reason == "length":
        text += CUTOFF_NOTE
    return text


# ---- Step 20: streaming versions ----
#
# These do the exact same thing as ask_claude() / ask_chatgpt() above, but
# instead of blocking until the whole answer is ready and returning it as
# one string, they're generators: each `yield` hands back the next small
# chunk of text as soon as the model produces it. A caller that wants the
# old all-at-once behavior can still do "".join(ask_claude_stream(messages)).
# Streaming doesn't use more tokens or cost more - it's the exact same
# request, just delivered progressively instead of in one lump.

def ask_claude_stream(messages, system=None, max_tokens=None):
    with claude.messages.stream(**_claude_kwargs(messages, system, max_tokens)) as stream:
        # .text_stream yields only the actual answer text, skipping over any
        # "thinking" content - same underlying distinction as the ThinkingBlock
        # check in ask_claude() above, just handled by the SDK during streaming.
        for chunk in stream.text_stream:
            yield chunk

        final_message = stream.get_final_message()
        usage_totals["claude_input_tokens"] += final_message.usage.input_tokens
        usage_totals["claude_output_tokens"] += final_message.usage.output_tokens
        _record_usage("anthropic", CLAUDE_MODEL, final_message.usage.input_tokens, final_message.usage.output_tokens)

        if final_message.stop_reason == "max_tokens":
            yield CUTOFF_NOTE


def ask_chatgpt_stream(messages, system=None, max_tokens=None):
    finish_reason = None
    stream = chatgpt.chat.completions.create(
        model=OPENAI_MODEL,
        messages=_openai_messages(messages, system),
        max_completion_tokens=max_tokens or MAX_TOKENS,
        stream=True,
        stream_options={"include_usage": True},  # asks for a final usage-only chunk
    )
    for chunk in stream:
        if chunk.usage is not None:
            usage_totals["openai_input_tokens"] += chunk.usage.prompt_tokens
            usage_totals["openai_output_tokens"] += chunk.usage.completion_tokens
            _record_usage("openai", OPENAI_MODEL, chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
        if chunk.choices:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason

    if finish_reason == "length":
        yield CUTOFF_NOTE


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


# Shared, reusable prompt pieces - defined once here so both the plain
# (non-streaming) functions below and app.py's streaming version of the same
# flows use identical wording, instead of two copies that can drift apart.
REVIEW_INSTRUCTION = "Critique the answer above - point out mistakes or gaps - then give your own improved answer."


def build_compare_prompt(question, claude_answer, chatgpt_answer):
    return (
        "Two AI models independently answered the question below, without "
        "seeing each other's response. Compare their answers.\n\n"
        # Quick win 4: ask explicitly for a short bulleted list of the
        # meaningful differences up front, before the fuller prose comparison.
        # This is still one call (no extra cost) - just a more structured
        # answer - which is what lets the UI show a scannable list above the
        # side-by-side answers instead of only a paragraph.
        "Start your reply with a short bulleted list (at most 5 bullets) "
        "titled 'Key differences' - only the points where they meaningfully "
        "disagree or one covers something the other missed. Then, below "
        "that, give the fuller comparison: where they agree, where they "
        "disagree, and which parts of each seem more reliable.\n\n"
        f"Question: {question}\n\n"
        f"Model A (Claude):\n{claude_answer}\n\n"
        f"Model B (ChatGPT):\n{chatgpt_answer}"
    )


def build_debate_message(question, transcript, role_instruction):
    so_far = "\n\n".join(f"{name}: {reply}" for name, reply in transcript)
    message = f"Question: {question}\n\n"
    if so_far:
        message += f"Debate so far:\n{so_far}\n\n"
    message += f"Your role: {role_instruction}"
    return message


def second_opinion(question, primary="claude"):
    """One model answers, the other is shown the same shared context plus
    that answer, and asked to critique and improve it. `primary` picks who
    answers first - "claude" (default) or "chatgpt".

    Step 17: the reviewer gets `history` itself (the original question, any
    earlier conversation, everything the first model saw) plus one short
    instruction turn - not just a standalone question+answer pair - so its
    critique is grounded in the same information the first model had."""
    first = ask(question, target=primary)

    reviewer_messages = history + [{"role": "user", "content": REVIEW_INSTRUCTION}]
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


def debate(question, rounds=3, claude_role="Proposer", chatgpt_role="Critic", start="claude"):
    """Let the two models go back and forth, each playing an assigned role.
    `rounds` is a hard cap on how many replies get generated in total, so a
    run can never rack up an open-ended bill. `start` (Step 23) picks who
    opens the debate - "claude" (default) or "chatgpt".

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
    speaker = start

    for i in range(rounds):
        speaker_name = "Claude" if speaker == "claude" else "ChatGPT"
        role_instruction = ROLE_INSTRUCTIONS[roles[speaker]]
        message = build_debate_message(question, transcript, role_instruction)

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

    compare_prompt = build_compare_prompt(question, claude_answer, chatgpt_answer)
    comparison = ask_claude([{"role": "user", "content": compare_prompt}])
    return claude_answer, chatgpt_answer, comparison


# ---- Step 22: final combined answer ("get the conclusion") ----
#
# After a single review, independent-answers comparison, debate or recipe,
# the app can ask for one more thing: a settled takeaway instead of just the
# raw transcript. This is a plain follow-up call (using whichever text the
# app builds from that turn), not a new mode of its own.

CONCLUSION_INSTRUCTION = (
    "Based on the discussion above, give one final takeaway in three short "
    "parts: (1) one recommended answer, stated plainly, (2) any "
    "disagreements between the two models that are still unresolved, and "
    "(3) anything the user should double-check themselves rather than "
    "trust as already settled. Be concise - this is a summary, not a new "
    "essay."
)


def build_conclusion_prompt(question, discussion_text):
    return f"Question: {question}\n\nDiscussion so far:\n{discussion_text}\n\n{CONCLUSION_INSTRUCTION}"


# ---- Step 27: "continue from this answer" quick actions ----
#
# Short, one-click follow-ups shown as buttons under a finished answer.
# Each maps to an instruction appended to the same shared context that
# produced the answer, sent back to the model that gave it (except
# "other_model", which is deliberately sent to the *other* model instead).

FOLLOWUP_PRESETS = {
    "explain": "Explain your last answer above in simpler, plain-language terms, as if to someone new to the topic.",
    "challenge": "Play devil's advocate against your own last answer above - argue the strongest case against it.",
    "practical": "Turn your last answer above into a short list of concrete, practical next steps.",
}

OTHER_MODEL_INSTRUCTION = (
    "The message above is what the other AI model just said. Give your own "
    "perspective on it - where you agree, where you disagree, and anything "
    "you'd add."
)


# ---- Step 29: prompt presets and a built-in multi-step recipe ----
#
# Quick, one-click starting points. Each preset pre-picks the mode (and, for
# Debate, the roles) that tends to suit that kind of question, plus a short
# instruction prepended to whatever the user actually typed - the user's own
# question is always kept, never replaced.

PROMPT_PRESETS = {
    "Review my code": {
        "mode": "Single review",
        "primary": "Claude",
        "prefix": "Review the following code for bugs, readability and best practices.",
    },
    "Fact-check this": {
        "mode": "Debate",
        "rounds": 2,
        "claude_role": "Proposer",
        "chatgpt_role": "Fact-checker",
        "prefix": "Fact-check the following claim(s) carefully.",
    },
    "Explain like I'm new": {
        "mode": "One model only",
        "which_model": "Claude",
        "prefix": "Explain the following as if I'm completely new to the topic - plain language, no jargon.",
    },
    "HAN assignment feedback": {
        "mode": "Single review",
        "primary": "ChatGPT",
        "prefix": "Give feedback on the following as if grading a HAN assignment - be specific about what would raise the grade.",
    },
}


# ---- Depth control: one toggle instead of setting mode + rounds + token
# budget separately. Each preset bounds the work (and cost) it can do - a
# "Quick check" can never accidentally turn into a long, expensive debate.
DEPTH_PRESETS = {
    "Quick check": {
        "mode": "One model only", "which_model": "Claude", "max_tokens": 1024,
    },
    "Thorough review": {
        "mode": "Single review", "primary": "Claude", "max_tokens": 4096,
    },
    "Deep debate": {
        "mode": "Debate", "rounds": 4, "claude_role": "Proposer", "chatgpt_role": "Critic",
        "max_tokens": 8192,
    },
}


# ---- Confidence tags: each model marks its own claims, instead of every
# sentence reading equally certain. This does NOT try to line up "the same
# claim" across both models (that's a harder job - see the roadmap's
# Disagreement map idea) - it's each model's own honesty about itself,
# shown with a bit of color so a "[guessing]" tag actually stands out.
CONFIDENCE_TAG_INSTRUCTION = (
    "For each substantive claim you make, tag it inline right after the "
    "claim with exactly one of: [sure], [fairly sure], or [guessing] - "
    "reflecting your actual confidence, not just to fill in the tag."
)

# The "Draft -> Critique -> Revise -> Final check" recipe from the roadmap:
# a fixed chain of steps, each one model speaking in turn, every step seeing
# the original question plus the full chain so far (same shared-context
# pattern as debate(), reusing build_debate_message below).
RECIPE_STEPS = [
    ("claude", "Draft", "Write a first draft answer to the question."),
    ("chatgpt", "Critique", "Critique the draft above - point out gaps, mistakes or weak reasoning. Do not rewrite it yet."),
    ("claude", "Revise", "Revise your draft using the critique above. Produce the full improved answer."),
    ("chatgpt", "Final check", "Do a final check of the revised answer above - confirm it's solid, or flag anything that's still wrong or missing."),
]


def run_recipe(question):
    """Non-streaming version of the Draft -> Critique -> Revise -> Final
    check recipe, for terminal use / parity with debate() above. app.py has
    its own streaming version of the same fixed steps.

    Returned as (speaker, step_label, reply) triples - unlike debate()'s
    (speaker, reply) pairs - since here the label (Draft/Critique/Revise/
    Final check) varies step to step instead of being one constant role
    per speaker."""
    transcript = []          # (speaker_name, reply) - what build_debate_message needs
    labeled_transcript = []  # (speaker_name, label, reply) - what's actually returned
    for speaker, label, instruction in RECIPE_STEPS:
        message = build_debate_message(question, transcript, instruction)
        speaker_name = "Claude" if speaker == "claude" else "ChatGPT"
        reply = ask_claude([{"role": "user", "content": message}]) if speaker == "claude" \
            else ask_chatgpt([{"role": "user", "content": message}])
        transcript.append((speaker_name, reply))
        labeled_transcript.append((speaker_name, label, reply))
    return labeled_transcript


# ---- Smarter collaboration batch (27 Sept 2026) ----
#
# All of these are just more prompt-building helpers and small instruction
# strings, in the same spirit as CONCLUSION_INSTRUCTION / REVIEW_INSTRUCTION
# above - app.py wires each one into an optional toggle or a quick-action
# button. None of them need new infrastructure.

DISAGREEMENT_MAP_INSTRUCTION = (
    "Analyze the discussion above for disagreement between the two models. "
    "First, score the overall level of agreement in one line: fully agree "
    "/ mostly agree / partially disagree / fundamentally disagree. Then "
    "list the specific points where they clash, and for each one classify "
    "*why*: different facts, different assumptions, different priorities, "
    "or different interpretations - since each kind needs a different "
    "resolution."
)


def build_disagreement_map_prompt(question, discussion_text):
    return f"Question: {question}\n\nDiscussion so far:\n{discussion_text}\n\n{DISAGREEMENT_MAP_INSTRUCTION}"


CHALLENGE_ASSUMPTIONS_INSTRUCTION = (
    "Before anyone answers, point out any questionable assumptions in the "
    "question below - including ones you suspect most people would share "
    "without noticing. If there's truly nothing worth flagging, say so in "
    "one line. Do not answer the question itself here."
)


def build_challenge_assumptions_prompt(question):
    return f"Question: {question}\n\n{CHALLENGE_ASSUMPTIONS_INSTRUCTION}"


# "Ask before debating": a quick check before round 1 - either a clarifying
# question or this exact phrase, matched case-insensitively so app.py can
# tell whether to pause for an answer or just carry straight on.
NO_CLARIFICATION_NEEDED = "no clarifying questions needed"

CLARIFY_INSTRUCTION = (
    "Before debating the question below, decide whether one important "
    "detail is missing that would change the answer. If so, ask exactly "
    "one or two clarifying questions and nothing else. If not, reply with "
    f"exactly: {NO_CLARIFICATION_NEEDED.capitalize()}."
)


def build_clarify_prompt(question):
    return f"Question: {question}\n\n{CLARIFY_INSTRUCTION}"


# "Adaptive debate length": a cheap yes/no check run after each round once
# there's enough transcript to judge. Kept to a one-word answer so it's a
# small, fast call - the caller (app.py) matches "YES" case-insensitively.
STALL_CHECK_INSTRUCTION = (
    "Compare these two most recent replies from a debate. Have they "
    "stopped adding meaningfully new information - i.e. are they mostly "
    "repeating earlier points rather than advancing the discussion? "
    "Reply with exactly one word: YES or NO."
)

CONSENSUS_CHECK_INSTRUCTION = (
    "Look at this debate transcript so far. Have both sides now converged "
    "on a shared conclusion they'd both sign off on? Reply with exactly "
    "one word: YES or NO."
)


def build_stall_check_prompt(last_two_replies_text):
    return f"{last_two_replies_text}\n\n{STALL_CHECK_INSTRUCTION}"


def build_consensus_check_prompt(transcript_text):
    return f"{transcript_text}\n\n{CONSENSUS_CHECK_INSTRUCTION}"


# "Steelmanning": appended to a debater's role instruction from round 2
# onward (round 1 has nothing yet to steelman).
STEELMAN_INSTRUCTION = (
    "Before responding, first restate the other side's most recent point "
    "fairly and accurately in your own words - a real steelman, not a "
    "caricature - then give your response."
)

# "Tests instead of opinions": an optional addition to the system prompt,
# same mechanism as CONFIDENCE_TAG_INSTRUCTION.
TESTS_NOT_OPINIONS_INSTRUCTION = (
    "Where the question is checkable - by a calculation, an experiment, "
    "or a small test - propose that test explicitly rather than just "
    "asserting an opinion."
)

# "More roles and personas": one more fixed role alongside Proposer/Critic/
# Fact-checker. Custom, user-saved personas (a name + a free-text
# instruction) are stored in the database (db.py's personas table) and
# merged into the same role-instruction lookup by app.py at ask-time.
ROLE_INSTRUCTIONS["Devil's advocate"] = (
    "Argue against the previous statement no matter how good it is - your "
    "job is to find the strongest possible objection, not to be balanced."
)

THIRD_OPTION_INSTRUCTION = (
    "Looking at the discussion above, the models have likely converged on "
    "(or you can see) two obvious choices. Propose a materially different "
    "third option that still meets the original requirements - not a "
    "compromise between the two, a genuinely different approach."
)


def build_third_option_prompt(question, discussion_text):
    return f"Question: {question}\n\nDiscussion so far:\n{discussion_text}\n\n{THIRD_OPTION_INSTRUCTION}"


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
