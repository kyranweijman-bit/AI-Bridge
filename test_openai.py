"""
Step 5: confirm the OpenAI API works on its own before wiring anything else up.
"""

import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# OpenAI() automatically picks up OPENAI_API_KEY from the environment.
client = OpenAI()

response = client.chat.completions.create(
    model=os.environ["OPENAI_MODEL"],
    messages=[{"role": "user", "content": "Say hello in one sentence."}],
)

print(response.choices[0].message.content)
