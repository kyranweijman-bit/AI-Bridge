# AI Bridge - feature roadmap

A running list of feature ideas discussed for the Claude/ChatGPT bridge app (Streamlit app in the `AI-Bridge` GitHub repo, hosted on Streamlit Community Cloud).

Guiding promise: two AIs working together should give you something better than two separate answers - make their collaboration useful, visible and controllable.

Last reorganized: 2 Oct 2026.

**Chapters**
1. Next steps
2. Memory
3. Documents (large documents, 200+ pages)
4. Decisions still open
5. Backlog - bigger additions
6. Backlog - a bit crazy, but buildable
7. Notes worth remembering
8. Built so far

---

## 1. Next steps

Suggested order, combining the earlier priority list (evidence-backed review, project rooms, third model, accounts) with the open foundation fix and the 2 Oct large-document findings. Cheap, high-impact items first.

### Now - small, cheap, high impact
1. **Check two things in the code first** (no build, just reading): are text attachments resent on every follow-up turn in a chat, and does anything already truncate long extracted text? The answers decide how much items 3-5 save.
2. **Reliable follow-up memory (foundation fix).** Follow-up questions should remember the reviewer's contribution and debate results. Right now those aren't fully written back into the ongoing chat history, so a follow-up after a debate only partly "knows" what was concluded. With the database this is now a matter of which rows get `in_context = true`. (See chapter 2.)
3. **Prompt caching for attachments.** Put the document at the very start of the context, identical on every call, and mark it cacheable for Claude (OpenAI caches long repeated prefixes automatically). Cached rereads cost roughly 10% of normal input price on Claude and start faster. Small change, immediate savings on debates, recipes and follow-ups. (Details: chapter 3, option 1.)
4. **Housekeeping: model + pricing check.** Revisit `OPENAI_MODEL` in `.env` (gpt-5.4 still works, GPT-6 series exists since late Sept 2026) and update the `PRICING` dict in `bridge.py` to match whatever's in use.

### Next - the bigger wins
5. **Send only the relevant passages.** Use the existing tagged passages (Built 20/51) and send only the 10-30 most relevant to the question, starting with simple keyword search (BM25 - free, no new infrastructure). Keep a "use whole document" checkbox for questions that need everything. Upgrade to pgvector embeddings later if keyword matching isn't good enough. (Details: chapter 3, option 2.)
6. **Cost estimate + large-document guardrails.** Show an estimated cost before running ("This document is ~130k tokens; a 4-round debate ≈ $X") using the existing `PRICING` dict; default large documents to cheaper modes; don't resend the full document on follow-ups. (Details: chapter 3, option 5.)
7. **Evidence-backed review.** Extract every factual claim into a table (claim, who made it, does the other model agree), let the Fact-checker consult real sources/web search and attach supporting passages, and label each claim supported / disputed / unchecked. Agreement between the models alone doesn't count as verification. Not free/simple - needs a real web-search tool wired to the Fact-checker, which is its own integration and its own per-call cost. (Last remaining item from the "smarter collaboration" idea list.)

### After that
8. **Cross-chat memory** - once a direction is picked (chapter 2). Suggested start: Option A, automatic extraction into the existing Memory panel.
9. **Project rooms.** Group chats, documents, instructions, decisions and the working brief around one ongoing project, with shared notes (preferences, past conclusions) both models can read across chats in that room. Extends the per-chat working brief (Built 40) across a group of chats.
10. **A third AI model** in the debate/review/independent-answers flow. Architecture is model-agnostic - any model just needs an `ask_x(messages) -> text` function. A local Ollama model is a cheap, private third voice - free if self-hosted on a spare machine; a hosted third model carries its own separate bill.
11. **Per-person accounts** once the app has more than a couple of regular users, so people sharing access don't all see the same chat list (`chats.owner` column is reserved for this). Route still to be picked - see chapter 4.
12. **Branch-compare / merge view.** Built 43 can fork a chat, but there's no UI yet to view two branches side by side or merge results back together.

---

## 2. Memory

### What exists today
- **Within one chat:** the chat's own history (Built 1), plus any final conclusion folded back into context (Built 18). Reviewer contributions and debate results are only partly carried into follow-ups yet - that's the foundation fix, next step 2.
- **Memory panel (Built 16):** notes sent to both models as a system prompt on every call, scoped to all chats or one chat. Only remembers what you type in yourself, or save with "Add to memory" (Built 26).
- **Working brief (Built 40):** per-chat goal / constraints / decisions / open questions, sent on every call in that chat.
- **Across chats:** nothing carries over automatically. The only cross-chat carry-over is Memory panel notes saved with "All chats" scope (asked and confirmed 2 Oct 2026).

### Cross-chat memory - options (discussed 2 Oct 2026, not yet picked)
- **Option A - Automatic extraction.** After each chat, one cheap extra API call decides what's durable (a fact, preference, decision) and auto-saves it into the existing "All chats" memory list. Reuses what's already built; still reviewable/editable/deletable via the Memory panel. Cost: one extra cheap call per chat.
- **Option B - Full semantic search (pgvector embeddings).** Every message across every chat gets embedded, and each new question pulls in whichever past snippets - from ANY chat - are actually relevant. More complete, more infrastructure: a Postgres vector extension, an embedding model, embedding cost per message, plus a retrieval step per question. (Shares infrastructure with the embeddings upgrade of next step 5.)
- **Option C - Manual "save key facts from this chat" button.** Same idea as Option A's extraction, but you trigger it yourself.

Suggested starting point: Option A first, with Option B as a later upgrade if extraction alone misses things. Project rooms (next step 9) would add a middle layer: memory shared across the chats of one project.

---

## 3. Documents (large documents, 200+ pages)
Assessment from 2 Oct 2026, made from this roadmap rather than from reading the code - see next step 1 for what to verify.

**Current state.** Built 20 splits files into tagged passages (good for citations), but every passage is still sent on every call - nothing picks only the relevant ones. A 200-page document is roughly 100k words / ~130k tokens.
- **Size:** fits Claude's context window; for ChatGPT it depends on the context limit of the model in `OPENAI_MODEL`. At 300-400 pages (or dense text/spreadsheets) one or both models can hit the limit - likely an API error rather than a graceful degrade.
- **Cost multiplies per mode:** every call pays for the whole document again - One model only 1 call, Single review 2, Independent answers 3, Recipe 4, Debate 2 per round plus a growing transcript. A 4-round debate over a 200-page PDF is ~8 calls of 130k+ input tokens each - easily a few dollars per question, more if follow-ups resend the attachment. The daily spending limit (Built 28) is the current safety net.
- **Speed:** time-to-first-token goes up noticeably with that much input.
- **Edit my file (Built 48) breaks down:** the 8192-token output cap is roughly 10-15 pages, so a full edited 200-page file can't come back (hits "[Answer cut off]"). Realistically only for short documents.
- **Scanned PDFs:** `pypdf` text extraction only, no OCR - image-only pages come through empty.

**Options to keep it affordable** (cheapest to build first):
1. **Prompt caching** (next step 3) - biggest win for the least work. Put the document at the very start of the context, identical on every call, and mark it cacheable for Claude (OpenAI caches long repeated prefixes automatically). Cached rereads cost a fraction of normal input price (roughly 10% on Claude) and start faster. Helps most for debates/recipes and quick follow-ups; caches expire after a few minutes idle.
2. **Send only the relevant passages** (next step 5) - biggest win overall. Send only the 10-30 most relevant passages, so a 200-page doc costs about the same as a 10-page one and citations still work. Two ways to pick them: simple keyword search (e.g. BM25 - free, no new infrastructure) or embeddings via pgvector in Supabase (better at matching different wording; embedding a 200-page doc once costs cents, and the same setup could later power cross-chat memory Option B). Keep a "use whole document" checkbox for questions that need everything. Overlaps with "Personal document library" (chapter 5).
3. **One model reads, the debate gets a summary** - one model reads the full document once and produces a dense extract of what matters for the question; the review/debate then runs on that extract. Full price once, cheap after that.
4. **Cheaper model for the heavy reading** - route the step that touches the full document to a cheap, fast model and keep the expensive models for reasoning over the extracted passages. Fits "Smart routing" (chapter 5).
5. **Guardrails** (next step 6) - show an estimated cost before running using the existing `PRICING` dict; default large documents to cheaper modes (one model / single review rather than debate); don't resend the full document on follow-ups, send relevant passages instead.

Together, options 1 and 2 should bring a 200-page debate from several dollars to well under a dollar.

---

## 4. Decisions still open

### Cross-chat memory
Option A, B or C - see chapter 2.

### Per-person accounts - which route (deferred 27 Sept 2026)
- **Lightweight:** a "who are you" name picker that just filters the existing shared chat list by the `chats.owner` column (trivial, no login).
- **Real login:** per-person login via Supabase Auth (free at this app's scale, but a bigger change - login/logout flow, password resets, migrating existing chats to an owner).

---

## 5. Backlog - bigger additions
Not scheduled yet. (Project rooms, third model, accounts and branch-compare are already in chapter 1.)

1. Personal document library: search relevant passages across all your files instead of sending one attachment (builds on Built 20; the single-document version is next step 5).
2. Artifact workshop / collaborative build: instead of debating, the models split a task - one drafts a document, plan or code change, the other edits/reviews, then they swap - with the revisions shown. Can extend to agent handoff (one plans, the other executes with tools like code execution or search, the planner reviews). (Built 48's "edit my file" is the simple, one-shot version.)
3. Smart routing: the app reads the question and picks the mode (quick question -> one model; risky factual one -> debate with fact-checker), and picks models per stage by your quality/speed/cost preference - including a cheap-first pass that only escalates to expensive models when the cheap ones disagree.
4. Rubric grading: upload an assessment rubric (e.g. the P4 criteria) and both models grade your document against it independently, then reconcile their scores.
5. Personal evaluation set: a set of questions with known good outcomes, used to measure whether changes to AI-Bridge actually improve it.
6. Recipe editor: save your own multi-step recipes, beyond the two built-in ones (Built 25, 50).
7. Per-topic stats: break the model-vs-model dashboard (Built 44) down by topic (code/writing/facts/maths).
8. Images in Debate and Recipe mode (Built 46 only covers Single review, Independent answers and One model only).
9. Voice mode: speak your question and hear the debate read aloud in two voices, podcast-style. Not free/simple - needs a text-to-speech service and its own per-call cost.
10. Shareable debate links: a read-only public page of a single debate to show classmates or teachers. Not free/simple - means exposing something publicly from what's currently a fully private, password-gated app, which needs its own careful design pass before building.

---

## 6. Backlog - a bit crazy, but buildable
1. Courtroom mode: one model prosecutes, one defends, a third model (or you) judges, with a cross-examination round where they must answer each other's direct questions.
2. AI negotiation table: assign competing priorities (e.g. quality vs. delivery speed) and have the models negotiate a compromise that satisfies your minimum requirements.
3. Alternate-futures room: give the same plan to an optimist, a skeptic and a practical operator; explore plausible outcomes and the early signals that would tell them apart.
4. Assumption switchboard: change one key assumption (budget, deadline, audience) and see which parts of the recommendation need updating.
5. Failure rehearsal / red team: "It's six months later and this failed - what probably happened?" Both models compete to find the most likely failure paths (scored on realism and severity), which are turned into preventive actions.
6. Idea evolution tournament: generate several ideas (or 2-3 versions per model), critique them, pit them against each other, combine the strongest parts, and repeat for a bounded number of generations - keeping the family tree of how the winner developed.
7. Disagreement inbox / time-travel review: save unresolved questions and past conclusions across projects; revisit them after a set time or when new evidence or constraints are added, and check whether the models still agree.

---

## 7. Notes worth remembering

### Setup and configuration
1. OpenAI model lineup has moved on since `gpt-5.4` was picked (GPT-6 series exists as of late Sept 2026) - gpt-5.4 still works but is worth revisiting in `.env`'s OPENAI_MODEL (next step 4).
2. Pricing table for cost tracking lives in `bridge.py` (`PRICING` dict) and needs manual updates if models change - it's not fetched live. Cost is stored per call in `usage_log` at the time of the call.
3. Dependencies added along the way: `fpdf2` (Built 48 - only for laying out a fresh PDF from edited text; reading PDFs is still `pypdf`) and `openpyxl` (Built 51). If deploying by hand rather than redeploying all files: `pip install` them, or on Streamlit Cloud push the updated `requirements.txt` and reboot the app.
4. `api.py` (Built 47) is a separate FastAPI script, not deployed on Streamlit Cloud (that platform only runs one process); needs its own `API_KEY` in `.env` plus `pip install fastapi uvicorn` to run it.

### Database
5. Database choice (26 Sept 2026): Postgres for long-term fit (queries across chats, pgvector, easy to move hosts); Supabase as today's host; kept portable by using only `DATABASE_URL` + one `db.py`. The first scaling limit is expected to be Streamlit Community Cloud, not the database. Supabase free projects pause after ~1 week idle.
6. Schema changes so far - all applied automatically by `init_schema()` on the app's next start, no manual migration: `settings` and `votes` tables (27 Sept morning, Built 27-33); `personas`, `briefs`, `decisions` tables (27 Sept afternoon); `files.original_bytes bytea` column (27 Sept night, Built 48). New mode/kind values (e.g. "recipe", "conclusion", "file_edit") needed no migration - `messages.mode` and `messages.kind` are free text. `db.add_extra`'s `role` column is repurposed to hold the original filename on `file_edit` rows, since a turn can carry several of them (one per attached file, and for Independent answers, one per model per file).
7. NUL-byte Postgres fix (2 Oct 2026): Postgres `text` columns can't store `\x00`, which caused insert failures for certain PDF-extracted text. Fixed with a blanket safety net in `db.py`'s `_query()` that strips `\x00` from every string parameter (bytes pass through untouched), plus stripping it at the source in PDF/docx extraction. No schema change, no data loss.

### Testing
8. The mock-Streamlit integration test harness (not part of the deployed app) covers 28 scenarios end-to-end as of 2 Oct 2026, up from 21 - the 7 new ones exercise Built 49/50 (stopping early in Single review, Independent answers at both checkpoints, and Recipe; self-check in Single review and the short recipe). These tests caught the two `db.py` bugs in Built 50 before they reached a real deploy.

### History of decisions
9. Accounts (27 Sept 2026): asked what could be added for free including accounts; decided to skip accounts for that round and do the other 7 free items instead (Built 27-33). Routes for later: chapter 4.
10. Smarter-collaboration batch (27 Sept 2026, afternoon): built all 14 items identified as buildable without new paid infrastructure (Built 34-47). The 4 flagged as not free/simple - evidence-backed review, voice mode, shareable debate links, smarter memory (pgvector) - were explicitly left for later.
11. "Edit my file" (27 Sept 2026, evening and later that night): added Built 48, then extended it to multiple files, every mode, and best-effort `.docx`/`.pdf` formatting carry-over.
12. Memory scope (2 Oct 2026): asked whether the app remembers things across chats - it doesn't by default. Details and options: chapter 2.
13. Priorities: earlier priority lists (27 Sept morning and evening) are superseded by chapter 1, which folds them in together with the 2 Oct large-document findings.

---

## 8. Built so far
Numbering kept stable - other sections refer to these as "Built N".

1. Shared multi-turn conversation history.
2. Single-review mode (one model answers, the other critiques) with swappable "who answers first."
3. Independent-answers mode: both models answer the same shared context without seeing each other's response first, then one model compares the two.
4. Debate mode: adjustable round count (1-6), assignable roles per model (Proposer / Critic / Fact-checker), full running transcript + original question rebuilt into context every round.
5. "One model only" mode - ask just Claude or just ChatGPT directly.
6. Reviewers/debaters now see the same shared context (original question, attachments, prior conversation) rather than a stripped-down summary.
7. Live streaming: answers appear word-by-word as they're generated (via ask_claude_stream/ask_chatgpt_stream + st.write_stream) instead of a spinner followed by the full text at once. Same cost either way - streaming doesn't use more tokens.
8. A Claude-style sidebar with multiple saved chats (create/switch/delete).
9. File upload with real text extraction for .pdf and .docx (not just plain text).
10. Download buttons on every answer.
11. Cost visibility in the sidebar (now persistent - see 15).
12. Password gate + hosted on Streamlit Community Cloud, reachable from any device.
13. Both models now use a higher output-token budget (8192) and show a visible "[Answer cut off]" note instead of silently truncating or returning blank text if a response runs out of budget mid-answer.
14. Permanent database storage (written 26 Sept 2026; live once Supabase is set up per `SETUP_DATABASE.md`): PostgreSQL via a single `DATABASE_URL` (Supabase Session pooler), all DB code in `db.py`, schema in `schema.sql` (auto-created on start, RLS on to block Supabase's public API). Chats are stored as one row per message (question / answer / review / comparison / debate reply, with model, role, round, `in_context` flag) rather than a JSON blob, so search, stats and branching become simple queries. `migrate_json.py` copies old `chats/*.json` in. Attached files: extracted text always stored; originals optionally in Supabase Storage bucket `files`.
15. Persistent usage log: one row per API call (via `bridge.usage_callback`), sidebar shows today / this chat / all time and per-model totals.
16. Memory panel: notes sent to both models as a system prompt on every call, either for all chats or for one chat.
17. Chat organization: search across titles and message text, pin chats to the top, rename chats.
18. Final combined answer: a "Get final conclusion" button under any turn (single review, independent answers, debate or recipe) asks for one settled takeaway - a recommended answer, any disagreements between the models that are still unresolved, and anything to double-check yourself - and folds it into the chat's context for follow-ups, instead of leaving you with only the raw transcript.
19. Debate mode: choose who starts (Claude or ChatGPT), not just who plays which role.
20. Proper document support: attach multiple files at once (not just one), each broken into tagged passages - PDF by page, .docx by paragraph range, anything else by line range - so answers can cite exactly where something came from, e.g. "[report.pdf, page 3]", instead of reading the whole document as one undifferentiated blob.
21. Export a whole conversation as one readable Markdown file - every question, its attached filenames, every answer/review/comparison/debate-or-recipe reply, and any conclusion, in order.
22. Retry and resume: if a model call fails partway through a single review, independent answers, debate or recipe, the steps that already succeeded are kept and only the failed step is retried, instead of losing the whole exchange.
23. Continue from any answer: "Explain this", "Challenge this", "Make it practical" and "Ask the other model" buttons under the most recent answer (single review, independent answers, one-model-only) send a one-click follow-up without retyping the question.
24. Independent Answers mode shows the two answers side by side, and the comparison call now leads with a short bulleted list of the key differences before the fuller prose comparison.
25. Prompt presets ("Review my code", "Fact-check this", "Explain like I'm new", "HAN assignment feedback") set the best mode/roles in one click and prepend a matching instruction to your question. Plus one built-in multi-step recipe, "Draft -> Critique -> Revise -> Final check", as a fifth mode - a fixed, ready-made chain rather than a recipe *editor* for saving your own (that part's still not built).
26. "Add to memory" button under any turn saves its conclusion (or its answer, if no conclusion was generated yet) straight into that chat's memory, without opening the Memory panel.
27. Stop and intervene in a debate: with "pause after each round" on (default), a debate runs one round, then parks itself with Continue / Stop here / Cancel - Continue can carry an added note or correction ("Assume my budget is €500") that's folded into the next round's instructions. Unchecking it runs straight through all rounds automatically, as before.
28. Spending limit: a daily $ limit (sidebar Usage panel, stored in a new `settings` table, blank = no limit) checked before every model call, including mid-debate and mid-recipe - once today's cost reaches it, further calls are blocked with a clear reason until it's raised or the day rolls over.
29. Depth control: one "Quick check / Thorough review / Deep debate" toggle that sets mode, rounds and a bounded token budget together (1024 / 4096 / 8192). A prompt preset (25) takes priority over it if both are set.
30. Stage indicator: a caption above each debate round or recipe step ("Round 2 of 4 - ChatGPT as Critic", "Step 3 of 4 - Claude (Revise)") shows where the discussion is as it streams.
31. Blind judging: an opt-in toggle for Independent Answers mode shows the two answers as "Answer A" / "Answer B" until you vote (or choose to reveal without voting); votes are stored in a new `votes` table for a future stats dashboard. Off by default, so it doesn't change how existing independent-answer turns look.
32. Confidence tags: an opt-in toggle has each model tag its own claims inline as [sure] / [fairly sure] / [guessing], shown as small colored badges once the answer is saved. This is each model grading its own confidence, not a cross-model check of which specific claims actually clash (that's the Disagreement map, Built 34).
33. Copy as Markdown / LaTeX toggle: a per-turn "View as" control (Rendered / Markdown / LaTeX) switches every answer in that turn into a copy-friendly code block (Streamlit's code blocks have a built-in copy icon) or a minimal LaTeX document.
34. Disagreement map: a "Disagreement map" button under any two-model turn (single review, independent answers, debate, recipe - not solo) scores the overall level of agreement in one line, lists the specific points where the models clash, and classifies *why* for each one - different facts, assumptions, priorities or interpretations.
35. Challenge my assumptions: an opt-in checkbox on any question runs one extra step first - before anyone answers, Claude points out questionable assumptions in the question itself, including ones you might share without noticing.
36. Ask before debating: an opt-in checkbox for Debate mode runs one quick check before round 1 - if an important detail is missing, you're shown the clarifying question and can answer it (folded into round 1) or skip; if nothing's missing, the debate carries straight on with no pause.
37. Adaptive debate length: a "Debate length" choice next to Rounds - Fixed rounds (as before), "Stop early once replies stop adding new info", or "Consensus-or-bust" (keep going until both sides converge on a shared conclusion). Either alternative only ever *shortens* the debate versus the rounds cap you set - it never runs past it.
38. Steelmanning: an opt-in checkbox for Debate mode - from round 2 onward, each side must fairly restate the other's most recent point in its own words before responding, to catch the two sides arguing past each other.
39. Tests instead of opinions: an opt-in checkbox (any mode) - for checkable questions, asks the models to propose a calculation, experiment or small test that would settle it, rather than just asserting an opinion.
40. Editable working brief: a per-chat text block (sidebar "Working brief") for your goal, constraints, decisions and open questions - sent to both models on every call in that chat alongside memory, and editable at any time.
41. More roles and personas: a Devil's advocate debate role (must disagree, even with a good answer) plus saveable personas (sidebar "Personas" - a name + a free-text instruction, e.g. "strict teacher", "skeptical engineer") assignable to either model's Debate role alongside the fixed ones.
42. Missing-third-option button: a "Suggest a third option" button under any two-model turn asks the models to develop a materially different alternative beyond the two obvious choices they converged on, that still meets the original requirements.
43. Branching conversations: a "Branch from here" button under any turn forks the chat up to that point into a brand-new chat, so the two can diverge independently - fork only for now; no side-by-side branch-compare or merge view yet (next step 12).
44. Model-vs-model stats dashboard: sidebar panel showing overall blind-judging win counts, from the votes Built 31 collects - overall only for now; a per-topic breakdown (code/writing/facts/maths) isn't built yet.
45. Decision journal: "Log a decision from this turn" under any turn records what you chose and why; a sidebar "Decision journal" panel lists every logged decision across every chat, with a field to fill in the outcome later once you know what happened.
46. Image input: attach a PNG/JPG/WEBP/GIF (up to 5 MB each) alongside or instead of text files - sent as real visual input to Single review, Independent answers and One model only mode (Debate and Recipe mode's shared-context format is plain text only, so images aren't wired into those two yet). The image itself is never resent on later follow-up questions in the same chat (that would be a silent, ongoing token cost) - only a short text placeholder noting it was attached.
47. API / webhook endpoint: a separate script, `api.py` (FastAPI) - deliberately *not* part of the Streamlit app or deployed on Streamlit Cloud, since that platform only runs one process and can't also host a second server. Run it locally or anywhere on your own network instead (e.g. reachable by Home Assistant, a Discord bot, a phone shortcut): `POST /ask` with `mode` one of solo_claude / solo_chatgpt / single / independent / debate, protected by its own `API_KEY` in `.env`, logging to the same `usage_log` table, and optionally saving the answer into an existing chat (`chat_id`) so it shows up in the Streamlit sidebar too.
48. Edit my file, and download it again (27 Sept 2026, evening; extended the same night to multiple files, every mode, and formatting carry-over): attach up to 5 non-image files and check "Ask for the edited file(s) back" - the model(s) return the complete corrected file(s), with one marker per filename so the app can pull out just the edited content even if it also explains what it changed, and a "Download edited ..." button appears under the answer for each edited file once it's done. Works in every mode: One model only and Single review edit directly (Single review uses the reviewer's, i.e. final, polished answer); Independent answers gives you back Claude's AND ChatGPT's edited versions separately so you can pick; Debate and Recipe run one extra quick wrap-up call at the end that produces the final edited file(s) from the whole discussion, instead of repeating the full file content in every round/step. Formatting carry-over, where feasible: a `.docx` is rebuilt from the ORIGINAL uploaded file as a template - each existing paragraph's text is swapped in place, keeping that paragraph's style and its first run's character formatting (bold/italic/font/etc.), with any extra lines appended as new plain paragraphs; a `.pdf` is rebuilt fresh but at least matches the original's page size (read via `pypdf`'s mediabox) instead of defaulting to A4. Still NOT preserved, and not attempted: tables, headers/footers, inline images, or formatting on any run after the first in a `.docx` paragraph; a PDF is always a fresh simple text layout, never true fixed-layout reflow of the original (genuinely hard to do well, out of scope). Anything else (plain text, code, markdown, etc.) stays plain text with its original extension - no formatting to carry over there. The original file's bytes are now kept (a new `files.original_bytes` column) only for files attached with this feature turned on, specifically so the carry-over has something to rebuild from.
49. Stop and intervene in every multi-step mode, not just Debate (2 Oct 2026): the "pause after each step" checkbox (on by default, Debate had it since Built 27) now covers Single review, Independent answers, and Recipe too. Single review pauses after the first answer, with Continue to review / "Stop here - keep just this answer" / Cancel; Independent answers pauses twice - after Claude's answer ("Stop here - keep just Claude's answer") and again after both answers but before the comparison ("Stop here - skip the comparison"); Recipe (both the long and short version, see 50) pauses between every step, the same Continue/Stop here/Cancel as Debate already had. Stopping early turns what would've been a two-model turn into a plain solo turn (or, if both independent answers already ran, keeps both with no comparison) - so one clearly-good-enough or clearly-useless first answer doesn't force every remaining API call to run anyway.
50. Two new ways to run a multi-model exchange (2 Oct 2026): a short, 2-step "Recipe: Make -> Check" mode alongside the existing 4-step "Draft -> Critique -> Revise -> Check" - pick who makes it, the other model checks it, no Revise/Final-check round, for when the full recipe is overkill. And a self-check option - for Single review ("Have the same model check its own work, instead of the other one") and for the new short recipe - where the SAME model that answered is asked to review/check its own work in a separate follow-up call, instead of automatically swapping to the other model. Building self-check surfaced and fixed two real pre-existing bugs in `db.py`'s row <-> turn storage code, both only visible once the same model could appear twice in one turn: (a) `_rows_to_turn` merged the answer and review rows into one model-keyed dict, which silently dropped data whenever the same model filled both roles; (b) `_turn_to_rows` used `turn.get("first_answer", turn["claude"] if ... )` - Python evaluates that fallback expression even when `"first_answer"` IS present, so every self-check turn crashed with `KeyError: 'claude'` on save. Both fixed and covered by new automated tests (self-check single review, self-check short recipe) alongside the stop/pause tests for 49.
51. Real Excel reading (2 Oct 2026): attached `.xlsx`/`.xlsm` files are now read as actual spreadsheet data via `openpyxl` (new dependency) - sheet by sheet, grouped into chunks of 40 rows, tagged like "[Waiting_times.xlsx, sheet 'Sheet1', rows 1-40]" the same way PDF pages and .docx paragraphs already were. Before this, the app could only see the file's raw XML/binary bytes, not the actual cell values - a legacy `.xls` still isn't supported (needs saving as `.xlsx` first). Separately clarified (no code change needed): code files (`.py`, `.js`, or any other language) already worked via the existing generic text extraction fallback - only the file-uploader's label/help text was misleadingly narrow about it, now says so explicitly.
52. Fresh "Ask a question" box on a new chat (2 Oct 2026): starting a new chat (or switching to a different existing one) now always shows an empty question box, instead of carrying over whatever you'd last typed in another chat. Same fix pattern already used for the per-chat "Working brief" box - the text input is now keyed by chat ID, so each chat gets its own box instead of one shared field for the whole app.
