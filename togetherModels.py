# Docs for v1 can be found by changing the above selector ^
from together import Together
import os

client = Together(
    api_key=os.environ.get("TOGETHER_API_KEY"),
)

models = client.models.list()

for model in models:
    print(model.id)