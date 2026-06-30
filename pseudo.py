from providerRouter import providerRouter


".env file":
#Some api keys for the providers
OPENAI_API_KEY=sk-proj-1234567890
ANTHROPIC_API_KEY=sk-proj-1234567890
GEMINI_API_KEY=sk-proj-1234567890
GEMINI_API_KEY=sk-proj-1234567890




llm = providerRouter(preffered_model="gpt-4o-mini")



openai_model = {
    "provider": "openai",
    "name": "gpt-4o-mini",
    "apiKey": providerRouter.choose_api_key("gpt-4o-mini").apiKey,
    "baseUrl": "https://api.openai.com/v1",
    "modelSettings": {"temperature": 0.0, "max_tokens": 256},
}

ollama_model = {
    "provider": "openai",  # OpenAI-compatible client
    "name": "qwen3:32b",
    "apiKey": "",  # often empty for local Ollama
    "baseUrl": "http://127.0.0.1:11434/v1",
    "modelSettings": {"temperature": 0.0, "max_tokens": 256},
}


#internal code 

class providerRouter:
    def __init__(self, preffered_model="gpt-4o-mini"):
        self.preffered_model = preffered_model

    def route(self, messages):
        return self.preffered_model.route(messages)





Example task without provider router:
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

llm = ChatOpenAI(
    model="gpt-5.5",
    api_key=os.getenv("OPENAI_API_KEY"),
    temperature=0.2,
)

messages = [
    ("system", "You are a helpful data analyst."),
    ("human", "Analyze this data: Jan revenue 10000, Feb revenue 12000, Mar revenue 9000."),
]

response = llm.invoke(messages)

print(response.content)




Example task with provider router:
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

llm = providerRouter(preffered_model="gpt-4o-mini")

messages = [
    ("system", "You are a helpful data analyst."),
    ("human", "Analyze this data: Jan revenue 10000, Feb revenue 12000, Mar revenue 9000."),
]

response = llm.invoke(messages)

print(response.content)

class providerRouter:
    def __init__(self, preferred_model, providers=None):
        self.preferred_model = preferred_model
        self.providers = providers or ["openai", "azure", "fireworks", "together"]

    def invoke(self, messages, **kwargs):
        # 1. Analyze the incoming request
        features = self.analyze_query(messages, kwargs)

        # 2. Find providers that can run this model
        candidates = self.get_candidate_providers(
            model=self.preferred_model,
            features=features,
        )

        # 3. Choose provider based on this specific input
        provider = self.choose_provider(
            model=self.preferred_model,
            providers=candidates,
            features=features,
        )

        # 4. Call the selected provider
        response = provider.call(
            model=self.preferred_model,
            messages=messages,
            **kwargs,
        )

        # 5. Store observed performance
        self.update_provider_score(
            provider=provider,
            model=self.preferred_model,
            features=features,
            response=response,
        )

        return response




#creates an instance of the routing class and stores internal configuration, for example:
router = ProviderRouter(preferred_model="gpt-4o-mini")



#Test for github desktop

def testcommitfunction():
    assert 1 == 1