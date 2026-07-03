import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(
    api_key=os.getenv("Together_API_KEY"),
    base_url="https://api.together.xyz/v1",
)

response = client.chat.completions.create(
    model="openai/gpt-oss-20b",
    messages=[{"role": "user", "content": "Say hi to me."}],
    max_tokens=64,
)

print(response.choices[0].message.content)
