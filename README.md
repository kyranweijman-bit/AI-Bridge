# AI Bridge - setup

A Streamlit app where Claude and ChatGPT work together on your questions -
single review, independent answers, debate, recipes, and more (see
`roadmap.md` for the full feature list). Runs locally or hosted on
Streamlit Community Cloud; chats are stored permanently in Postgres
(Supabase).

## 1. API keys

Get an Anthropic API key (console.anthropic.com) and an OpenAI API key
(platform.openai.com). These are separate from ChatGPT Plus / Claude Pro
and billed per use.

## 2. Database (Supabase)

1. Create a free project at supabase.com.
2. Project Settings -> Database -> Connection string -> copy the **Session
   pooler** URI (not "Direct connection").
3. That's your `DATABASE_URL` below. The app creates every table it needs
   automatically on first run (see `schema.sql`) - no manual migration step.
4. Free Supabase projects pause after about a week of no activity; opening
   the app again (or visiting the Supabase dashboard) wakes it back up.

Original uploaded files (not just their extracted text) can optionally also
be kept in Supabase Storage - see the `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`
notes in `.env.example`. This is optional; skip it and the app still works,
keeping original file bytes in the database itself when needed (e.g. for
"edit my file").

## 3. Install and configure

In this folder, create a virtual environment and install dependencies:

```
python -m venv .venv
.venv\Scripts\Activate.ps1        (Windows PowerShell)
pip install -r requirements.txt
```

Copy `.env.example` to `.env`, then fill in your API keys, current model
names (check the docs links inside the file - model names change over
time), a password for the app's password gate, and your `DATABASE_URL`
from step 2.

## 4. Run it

```
streamlit run app.py
```

Opens in your browser at `localhost:8501`. Log in with the `APP_PASSWORD`
you set.

## 5. (Optional) Deploy to Streamlit Community Cloud

1. Push this folder to a GitHub repo (see `git-commands.txt`).
2. On share.streamlit.io, create a new app pointing at `app.py` in that repo.
3. In the app's Settings -> Secrets, paste in the same key/value pairs as
   your `.env` file (Streamlit Cloud doesn't read `.env` - it uses its own
   Secrets panel instead).
4. If you later update `requirements.txt` (a new dependency was added) and
   the app doesn't pick it up on its own, use the app's menu to reboot it.

## 6. (Optional) Quick sanity checks

- `python test_claude.py` - should print a one-sentence greeting from Claude.
- `python test_openai.py` - should print a one-sentence greeting from ChatGPT.
- `python bridge.py` - a bare-bones CLI version (no Streamlit, no database):
  type a question, get Claude's answer and ChatGPT's review of it, saved to
  a local `history.json`. Handy for a quick check that both API keys work
  end to end, but the real app is `streamlit run app.py` above.

`.env` is listed in `.gitignore` so your keys never get committed if you
push this folder to GitHub.

## 7. (Optional) The separate API/webhook script

`api.py` is a small FastAPI server, deliberately separate from the
Streamlit app (Streamlit Cloud can't also host a second server). Run it on
your own machine or network if you want another tool - Home Assistant, a
Discord bot, a phone shortcut - to ask a question and get an answer back.
See the comment at the top of `api.py` for setup and an example `curl` call.
It only covers the simpler modes (solo/single/independent/debate) and
doesn't yet have the newer Stop/self-check/short-recipe options the
Streamlit app has.
