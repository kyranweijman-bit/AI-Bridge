"""
Step 4: confirm the Claude API works on its own before wiring anything else up.
"""

import os
from dotenv import load_dotenv
from anthropic import Anthropic

# Reads the .env file in this folder and loads ANTHROPIC_API_KEY, CLAUDE_MODEL, etc.
load_dotenv()

# Anthropic() automatically picks up ANTHROPIC_API_KEY from the environment,
# so the key never has to be typed into this file.
client = Anthropic()

response = client.messages.create(
    model=os.environ["CLAUDE_MODEL"],
    max_tokens=300,
    messages=[{"role": "user", "content": "Say hello in one sentence."}],
)

print(response.content[0].text)
