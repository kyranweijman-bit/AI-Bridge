# AI bridge - setup

1. Get an Anthropic API key (console.anthropic.com) and an OpenAI API key
   (platform.openai.com). These are separate from ChatGPT Plus / Claude Pro
   and billed per use.

2. In this folder, create a virtual environment and install dependencies:

   ```
   python -m venv .venv
   .venv\Scripts\Activate.ps1        (Windows PowerShell)
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`, then fill in your two API keys and the
   current model names (check the docs links inside the file - model names
   change over time).

4. Run `python test_claude.py` - should print a one-sentence greeting.

5. Run `python test_openai.py` - should also print a one-sentence greeting.

6. Run `python bridge.py`, type a question, and you'll get Claude's answer
   plus ChatGPT's review of it.

`.env` is listed in `.gitignore` so your keys never get committed if you
push this folder to GitHub.
