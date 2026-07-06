ProjectPlan.txt
Nygen ProviderRouter PR Plan

Project goal
------------
Build nygen-router, a lightweight Python provider router for LLM calls.

The router prioritizes provider routing, not model routing. The user chooses the model they want to run, and the router chooses the best configured provider for that model based on runtime observations such as latency, success rate, rate limits, tool-calling support, and user-configured token cost.

Core design principles
----------------------
1. Keep the main import lightweight:

   from nygen_router import ProviderRouter

   This import must not require Anthropic, OpenAI SDK, LangChain, Pydantic AI, DuckDB, Supabase, OpenTelemetry, Logfire, or other optional provider/framework dependencies.

2. Core dependencies should stay small:

   - pydantic
   - httpx
   - typing-extensions if needed

3. Provider-specific SDKs must be optional and lazy-imported inside adapters.

   Example:

   class AnthropicMessagesAdapter:
       async def invoke(self, request):
           import anthropic
           ...

4. Optional dependency groups should be used:

   pip install nygen-router
   pip install nygen-router[anthropic]
   pip install nygen-router[duckdb]
   pip install nygen-router[langchain]
   pip install nygen-router[all]

5. The router should support both explicit code-level API key configuration and environment variable based API key configuration.

6. Do not build a custom API key proxy website. This is a security risk and out of scope.

7. Use typed structures instead of passing raw dictionaries throughout the core.

   Prefer:
   - Pydantic models for user-facing config validation
   - dataclasses for internal records
   - Enums for protocols, error types, providers, and capability flags
   - TypedDict only when representing external JSON shapes

8. Hard filters happen before scoring.

   Hard filters answer: can this provider satisfy the request?
   Scoring answers: among valid providers, which provider looks best?

9. One provider failure should not fail the whole request if another eligible provider exists.

10. Storage and observability failures should not break successful LLM responses.

Recommended package structure
-----------------------------
nygen_router/
  __init__.py
  router.py
  types.py
  config.py
  errors.py
  capabilities.py
  scoring.py
  metrics.py

  adapters/
    __init__.py
    base.py
    openai_compatible.py
    openai_responses.py
    anthropic_messages.py
    groq.py
    together.py

  policies/
    __init__.py
    base.py
    round_robin.py
    score_based.py

  storage/
    __init__.py
    base.py
    sqlite.py
    duckdb.py
    postgres.py

  integrations/
    langchain.py
    pydantic_ai.py

  observability/
    base.py
    opentelemetry.py
    logfire.py

PR 1: Provider configs and real provider calls
----------------------------------------------
Goal:
Get the first real providers up and running.

This PR should prove that the router can call provider APIs through user-supplied provider configs and API keys.

Scope:
- ProviderConfig
- RouterRequest
- RouterResponse
- ApiProtocol enum
- ProviderAdapter base protocol
- OpenAI-compatible adapter
- basic ProviderRouter class
- config validation
- API key loading from environment
- tests

Response transparency (schema added now, populated fully by later PRs):
- RouterResponse.attempts: list[ProviderAttempt], one entry per provider actually
  invoked during this call (provider name, success flag, and the real
  underlying error object if it failed -- never a router-rephrased summary).
  In PR1 this is always exactly one entry, since PR1 has no fallback yet.
- RouterResponse.excluded: list[EligibilityResult], one entry per provider that
  was filtered out before any call was made, with its specific reason. Always
  populated, even on a successful call. In PR1 this is always empty, since
  hard filtering does not exist until PR2.
- Adding both fields now (rather than in PR2/PR3, when they first have real
  content) avoids a breaking schema change to a public response type later.

Suggested files:
ProviderRouterPR1/
  pyproject.toml
  README.md
  AGENT.md
  src/
    nygen_router/
      __init__.py
      config.py
      types.py
      errors.py
      router.py
      capabilities.py
      adapters/
        __init__.py
        base.py
        openai_compatible.py
  tests/
    test_config.py
    test_openai_compatible_adapter.py
    test_router_pr1.py

Key implementation decisions:
- Use httpx directly for OpenAI-compatible third-party calls.
- Do not require the OpenAI SDK for OpenAI-compatible providers.
- Do not implement round robin yet.
- Do not implement SQLite yet.
- Do not implement scoring yet.
- Do not implement LangChain, Pydantic AI, DuckDB, Anthropic SDK, or observability.
- Transparency principle (applies to every PR from here on): the router must
  never wrap, rephrase, or blend a provider's own error into a generic router
  message. Individual failures/exclusions are always shown with their real,
  specific cause; only when every provider fails/is excluded does the router
  raise its own error, and that error must still enumerate each provider's
  real, distinct reason rather than a single blended message.

Minimum usage example:
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

response = router.invoke("Say hello")
print(response.text)

Tests:
- valid provider config
- missing provider name
- missing model
- missing base URL for OpenAI-compatible provider
- explicit API key resolution
- environment API key resolution
- missing API key error
- adapter builds correct HTTP payload
- adapter parses basic chat/completions response
- router invokes first enabled provider
- unsupported protocol raises clean error

PR 2: Essential hard filters
----------------------------
Goal:
Before routing, filter out providers that cannot satisfy the request.

Scope:
- ProviderCapabilities
- RequestRequirements
- EligibilityResult
- filter_eligible_providers

Essential filters:
- provider enabled
- API key available
- protocol supported
- model configured
- tool-calling support if required
- streaming support if required
- JSON mode support if required

Suggested files:
src/nygen_router/capabilities.py
src/nygen_router/filters.py
tests/test_filters.py

Important principle:
Hard filters are not scores. A provider that cannot support tools should be excluded for tool requests, not ranked lower.

Control flow change from PR1:
ProviderRouter.invoke() changes from "pick the first enabled provider, then
validate capabilities against only that one provider" to "filter the whole
provider list down to eligible candidates first, then select among survivors."
CapabilityError is retired as something invoke() raises directly -- it is no
longer reachable from the normal invoke() path. Each exclusion becomes an
EligibilityResult carrying a FilterReason enum (per the enums-for-capability-
flags principle) plus a specific human-readable detail, e.g. "missing
tool-calling support". These results populate RouterResponse.excluded on
every call (see PR1), success or failure.

If filtering excludes every configured provider, invoke() raises
NoEligibleProvidersError. Per the transparency principle, this error must
enumerate every excluded provider with its own specific FilterReason/detail
(e.g. "provider_a: missing tool-calling support; provider_c: disabled"),
never a single generic "no eligible providers" message.

Tests:
- provider without API key is excluded
- disabled provider is excluded
- provider without tools is excluded when tools are required
- provider without streaming is excluded when streaming is required
- all providers filtered out raises NoEligibleProvidersError, and the error
  message names each excluded provider with its own specific reason
- a successful call still reports any filtered-out providers in
  RouterResponse.excluded

PR 3: Round robin with current-run memory only
----------------------------------------------
Goal:
Make the router actually rotate between providers during the current Python process.

Scope:
- RoundRobinPolicy
- current-run round-robin index
- fallback to next provider on retryable error
- basic provider attempt record in memory

Suggested files:
src/nygen_router/policies/base.py
src/nygen_router/policies/round_robin.py
tests/test_round_robin.py
tests/test_fallback.py

Behavior:
For a loop like this:

for _ in range(10):
    router.invoke("hello")

The selected provider should rotate during that Python process.

No persistence yet.

Default policy (no API change required from PR1/PR2 callers):
Round-robin + fallback becomes the automatic default the moment PR3 ships.
ProviderRouter(providers=[...]) -- the exact same call signature as PR1 --
starts rotating and falling back with no code change. ProviderRouter gains an
optional `policy` constructor argument (e.g. policy=RoundRobinPolicy()) only
for explicitly overriding the default later (PR9's ScoreBasedPolicy).

Shared health/disablement state:
Per-run provider health state (starting with "auth-disabled this run" here,
extended by PR5 with cooldowns/consecutive-failure counts) lives on
ProviderRouter itself as a small tracker (e.g. self._health: dict[str,
ProviderHealthState]), not inside RoundRobinPolicy. This is so the same state
is visible to the eligibility filter (PR2) and to any policy, including
PR9's ScoreBasedPolicy later -- state must not be lost if the policy is
swapped. PR5 extends ProviderHealthState in place; it does not relocate it.

RouterResponse.attempts (see PR1) is populated with one entry per provider
actually invoked during fallback, in order, each carrying its real
success/failure outcome and the provider's real error object (never
rewrapped) if it failed.

Required fallback behavior:
provider A selected
provider A times out
router tries provider B
provider B succeeds
router returns provider B response (with attempts = [A: failed w/ real
error, B: succeeded])

Error categories:
- TIMEOUT
- RATE_LIMIT
- AUTH
- SERVER_ERROR
- BAD_REQUEST
- UNKNOWN

Suggested retry behavior:
- TIMEOUT: try next provider
- RATE_LIMIT: try next provider
- SERVER_ERROR: try next provider
- AUTH: disable provider for current run
- BAD_REQUEST: usually do not retry, because the request itself may be invalid

Tests:
- round robin rotates
- round robin ignores filtered providers
- fallback tries second provider on timeout
- fallback tries second provider on rate limit
- auth error disables provider for current run
- all providers fail raises RouterExhaustedError, and the error message names
  each attempted provider with its own real, distinct failure (never a
  blended "all providers failed" message)
- a successful fallback populates RouterResponse.attempts with every
  provider tried, including the real error for ones that failed first

PR 4: SQLite memory
-------------------
Goal:
Persist observational metrics locally using the Python standard library sqlite3 module.

Scope:
- MetricsEvent
- SQLiteMetricsStore
- schema initialization
- record attempt event
- query recent attempts

Suggested files:
src/nygen_router/metrics.py
src/nygen_router/storage/base.py
src/nygen_router/storage/sqlite.py
tests/test_sqlite_storage.py

First SQLite table:
CREATE TABLE provider_attempts (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    model TEXT NOT NULL,
    protocol TEXT NOT NULL,
    success INTEGER NOT NULL,
    latency_ms REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost_usd REAL,
    error_type TEXT,
    required_tools INTEGER NOT NULL,
    request_size_bucket TEXT NOT NULL
);

Important behavior:
Storage failure should not break a successful LLM response. If the provider succeeds but metrics storage fails, return the provider response.

Default storage (works out of the box, no config required):
ProviderRouter defaults to a SQLiteMetricsStore backed by a fixed file under
the user's home directory (~/.nygen_router/metrics.db), created automatically
on first use. This is a real file on disk, not an in-memory ":memory:"
database -- in-memory SQLite only lives for the current process and would
not persist across separate runs of a script, which would defeat the "router
learns over time" goal (PR9's final demo target). Using a fixed user-level
path (rather than a path relative to the current working directory) means
history is reused correctly regardless of which directory the program is
run from. `metrics_store` remains overridable: pass an explicit
SQLiteMetricsStore(path) for a custom location/backend, or metrics_store=None
to disable persistence entirely.

Tests:
- creates schema
- records success event
- records failure event
- queries recent events
- ignores events outside lookback window
- router continues if metrics store fails
- default metrics_store writes to ~/.nygen_router/metrics.db when not
  otherwise configured
- metrics_store=None disables persistence with no file created

PR 5: Health state and cooldowns
--------------------------------
Goal:
Avoid repeatedly calling providers that are temporarily bad.

Scope:
- in-memory provider health state
- consecutive failure count
- cooldown until timestamp
- auth-disabled state for current router instance

Suggested behavior:
- RATE_LIMIT: apply cooldown
- TIMEOUT: increment failure count
- SERVER_ERROR: increment failure count
- AUTH: disable provider for current router instance
- SUCCESS: reset provider failure count

Relationship to PR3:
This extends the same ProviderHealthState/self._health tracker introduced in
PR3 (auth-disabled) in place -- adding cooldown_until and
consecutive_failures fields -- rather than moving health state to a new
owner. It remains owned by ProviderRouter, shared by every policy.

Cooldown/auth-disabled exclusions are hard filters, not a separate
mechanism: filter_eligible_providers (PR2) consults this health state
alongside its static config checks, using the same FilterReason/
EligibilityResult machinery. A provider skipped for being in cooldown or
auth-disabled therefore also shows up in RouterResponse.excluded with its
specific reason, exactly like a PR2 static exclusion.

Tests:
- rate-limited provider enters cooldown
- provider in cooldown is filtered out, and appears in RouterResponse.excluded
  with a cooldown-specific FilterReason
- success resets failure count
- auth error disables provider for current run, and appears in
  RouterResponse.excluded with an auth-disabled FilterReason

PR 6: Token cost calculation
----------------------------
Goal:
Calculate estimated cost from user-provided pricing.

Scope:
- TokenPricing
- cost calculation helper
- cost field in metrics event

Important rule:
Do not scrape provider prices. Users configure cost per million tokens.

Example:
TokenPricing(
    input_cost_per_1m=0.15,
    output_cost_per_1m=0.60,
)

Calculation:
estimated_cost = (
    input_tokens / 1_000_000 * input_cost_per_1m
    + output_tokens / 1_000_000 * output_cost_per_1m
)

Tests:
- calculates input cost
- calculates output cost
- calculates total cost
- handles missing token counts
- handles missing pricing
- stores estimated cost in metrics event

PR 7: Metrics aggregation
-------------------------
Goal:
Turn raw events into provider stats.

Scope:
- ProviderStats
- ProviderStatsQuery
- aggregation by provider/model/lookback window

Stats to compute:
- attempt count
- success count
- success rate
- average latency
- average cost
- rate limit count
- timeout count
- recent error count

Tests:
- aggregates by provider
- aggregates by model
- uses lookback window
- handles no data
- handles partial data

PR 8: Basic score calculator
----------------------------
Goal:
Add pure score calculation.

Scope:
- ScoreWeights
- ProviderScore
- calculate_provider_score

Rules:
- The score calculator must not call providers.
- The score calculator must not write to storage.
- The score calculator must not import adapters.
- The score calculator only turns stats into scores.

First scoring factors:
- success rate
- latency
- cost
- recent errors
- exploration bonus

Tests:
- higher success rate improves score
- lower latency improves score
- lower cost improves score
- recent errors reduce score
- unknown provider gets exploration bonus

PR 9: Score-based routing policy
--------------------------------
Goal:
Use SQLite history to rank providers.

Scope:
- ScoreBasedPolicy
- RoutingProfile
- score-based candidate ranking
- fallback in ranked order

Important behavior:
Score policy should rank providers, not select only one.

Correct behavior:
rank A, B, C
try A
if A fails, try B
if B fails, try C

Tests:
- best-scoring provider is tried first
- fallback still works under score policy
- unknown provider is occasionally explored
- score policy respects hard filters

PR 10: Recency weighting
------------------------
Goal:
Recent performance should matter more than old performance.

Scope:
- lookback_hours
- optional half-life setting later
- recent error weighting

Start simple:
RoutingProfile(
    lookback_hours=72,
)

Later, add exponential decay if needed.

Tests:
- old events outside lookback do not affect score
- recent failures reduce score
- recent success improves score

PR 11: Request-size buckets
---------------------------
Goal:
Make routing more application-specific.

Scope:
Add request buckets:
- small
- medium
- large
- xlarge

Possible first definition:
- small: fewer than 2k estimated tokens
- medium: 2k to 16k estimated tokens
- large: 16k to 64k estimated tokens
- xlarge: more than 64k estimated tokens

Why this matters:
A provider may be excellent on small prompts but slow on large prompts. The router should eventually learn this.

Tests:
- small request gets small bucket
- large request gets large bucket
- stats can be queried by bucket
- score policy can prefer different providers by bucket

PR 12: OpenAI Responses API adapter
-----------------------------------
Goal:
Add the second major API protocol.

Scope:
- OpenAIResponsesAdapter
- ApiProtocol.OPENAI_RESPONSES
- request translation
- response normalization

Rule:
The router core should not change much. Only the adapter should know the protocol-specific payload format.

Tests:
- responses adapter builds correct payload
- responses adapter parses text output
- responses adapter parses token usage if present
- router can use responses protocol provider

PR 13: Storage backend abstraction upgrade
------------------------------------------
Goal:
Prepare for DuckDB, Supabase, Postgres, and other SQL-compatible backends.

Scope:
- MetricsStore protocol
- SQL schema versioning
- storage initialization
- stats query interface

Do not overbuild a full ORM too early. Start with a storage protocol or storage backend abstraction.

PR 14: DuckDB backend
---------------------
Goal:
Add optional local analytics backend.

Scope:
- DuckDBMetricsStore
- optional dependency group
- tests skipped if duckdb is not installed

Rule:
This must not affect:

from nygen_router import ProviderRouter

PR 15: Routing profiles
-----------------------
Goal:
Make the router adjustable per application.

Scope:
Add predefined routing profiles:
- balanced
- fastest
- cheapest
- most_reliable
- tool_heavy

Each profile controls:
- score weights
- lookback window
- exploration rate
- minimum sample size

PR 16: Drop-in factory interface
--------------------------------
Goal:
Make router adoption easy.

Scope:
- ProviderRouter.from_env(...)
- ProviderRouter.from_config(...)

Example:
router = ProviderRouter.from_env(
    model="some-model",
    providers=["together", "groq", "fireworks"],
    profile="balanced",
)

Support both:
- .env / environment-variable configuration
- explicit code-level configuration

PR 17: LangChain adapter
------------------------
Goal:
First framework integration.

Scope:
Add:
integrations/langchain.py

Rule:
LangChain imports ProviderRouter. ProviderRouter does not import LangChain.

PR 18: Pydantic AI adapter
--------------------------
Goal:
Second framework integration.

Scope:
Add:
integrations/pydantic_ai.py

Rule:
Pydantic AI integration must be optional.

PR 19: Logging hooks
--------------------
Goal:
Configurable debugging with standard library logging.

Events:
- provider selected
- provider succeeded
- provider failed
- fallback used
- provider filtered
- storage write failed

No print statements.

PR 20: Observability hooks
--------------------------
Goal:
Production-level tracing for serious users.

Scope:
Optional support for:
- OpenTelemetry
- Logfire
- custom callback hooks

Rule:
No observability dependency in core.

Recommended sprint boundary
---------------------------
For an 80% sprint target, aim to complete PR 1 through PR 10.

That gives the project:
- real provider calls
- hard filters
- round robin
- fallback
- SQLite memory
- health/cooldowns
- cost tracking
- metrics aggregation
- basic scoring
- recency-aware routing

Everything after that is valuable but can be considered expansion, integration, or polish.

Final demo target
-----------------
The strongest early demo is:

I configured 3 providers for the same model.
The router called them.
One failed.
The router fell back.
The router stored latency, success, token usage, and estimated cost.
After enough calls, the router started preferring the better provider.

Quality commands
----------------
These commands should eventually pass:

ruff format .
ruff check .
mypy src
pytest
coverage run -m pytest
coverage report

Non-negotiable rule
-------------------
This must always work without optional provider dependencies:

from nygen_router import ProviderRouter

If this import fails because Anthropic, OpenAI SDK, LangChain, Pydantic AI, DuckDB, Supabase, Logfire, or OpenTelemetry is not installed, the implementation is wrong.
