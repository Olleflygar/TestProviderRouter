## Onboarding: plug Nygen Router into your existing workflow

Nygen Router is designed to be used where you would normally use an LLM or model client.

```python
router = ProviderRouter(...)

# Use router where you would normally use an LLM/model client.
response = router.invoke("Write a short product description.")

print(response.text)
```

Instead of hard-coding one provider, you give Nygen Router multiple provider options. The router can then choose which provider should handle each request based on eligibility, recent performance, latency, cost, and other application-specific metrics.

---

## Plain Python

Use the router directly when you are calling model APIs yourself.

```python
from nygen_router import ProviderRouter, ProviderConfig, ApiProtocol

router = ProviderRouter(
    providers=[
        ProviderConfig(
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="some-model",
            base_url="https://provider-a.example.com/v1",
            api_key_env="PROVIDER_A_API_KEY",
        ),
        ProviderConfig(
            name="provider_b",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="some-model",
            base_url="https://provider-b.example.com/v1",
            api_key_env="PROVIDER_B_API_KEY",
        ),
    ]
)

response = router.invoke("Write a short product description.")

print(response.text)
```

The application keeps one model-call interface, while Nygen Router decides which configured provider should handle the request.

---

## LangChain

Use the LangChain adapter anywhere you would normally pass a chat model.

```python
from nygen_router import ProviderRouter, ProviderConfig, ApiProtocol
from nygen_router.integrations.langchain import ChatNygenRouter

router = ProviderRouter(
    providers=[
        ProviderConfig(
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="some-model",
            base_url="https://provider-a.example.com/v1",
            api_key_env="PROVIDER_A_API_KEY",
        ),
        ProviderConfig(
            name="provider_b",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="some-model",
            base_url="https://provider-b.example.com/v1",
            api_key_env="PROVIDER_B_API_KEY",
        ),
    ]
)

llm = ChatNygenRouter(router)

response = llm.invoke("Write a short product description.")

print(response.content)
```

For existing LangChain workflows, the rest of the chain can stay the same.

```python
chain = prompt | llm | parser

result = chain.invoke({
    "product": "wireless headphones"
})
```

---

## CrewAI

Use the CrewAI adapter as the LLM for an agent.

```python
from crewai import Agent
from nygen_router import ProviderRouter, ProviderConfig, ApiProtocol
from nygen_router.integrations.crewai import NygenCrewAILLM

router = ProviderRouter(
    providers=[
        ProviderConfig(
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="some-model",
            base_url="https://provider-a.example.com/v1",
            api_key_env="PROVIDER_A_API_KEY",
        ),
        ProviderConfig(
            name="provider_b",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="some-model",
            base_url="https://provider-b.example.com/v1",
            api_key_env="PROVIDER_B_API_KEY",
        ),
    ]
)

llm = NygenCrewAILLM(router)

researcher = Agent(
    role="Researcher",
    goal="Find concise and accurate information",
    backstory="You are a careful research assistant.",
    llm=llm,
)
```

The CrewAI agent, tasks, tools, and orchestration stay the same. Nygen Router only handles provider selection for the model calls.

---

## Runtime provider selection

For each request, Nygen Router can:

```text
1. Check which providers are eligible.
2. Filter providers by required capabilities, such as tool calling or streaming.
3. Score the remaining providers using application-specific rules.
4. Send the request to the selected provider.
5. Record runtime metrics such as latency, cost, errors, and provider health.
6. Use recent performance data to improve future routing decisions.
```

This means routing is based on how providers perform inside your application, not only on static benchmarks.
