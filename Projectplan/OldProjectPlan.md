ProjectPlan.txt
Nygen ProviderRouter PR Plan

Project goal
------------
Build nygen-router, a lightweight Python provider router for LLM calls.

The router prioritizes provider routing, not model routing. The user chooses the model they want to run, and the router chooses the best configured provider for that model based on runtime observations such as latency, success rate, rate limits, and tool-calling support.

Cost is not part of the router's core routing/scoring logic. The main focus
of this project is a pluggable router that works with minimal setup out of
the box, and is easy to customize if someone wants different routing logic.
An automatic token-price scraper will not be built (provider pricing changes
too fast to scrape reliably at this project's scale). Manually-configured
token pricing may be added later as an optional, user-supplied customization
(see PR 6), but it is deferred and does not gate the core sprint.

Implementation status (as of 2026-07-13)
-----------------------------------------
PR 1, PR 2, and PR 3 are implemented and merged (ProviderRouterPR1/src/nygen_router/).
The three PR sections below are rewritten from "planned" to "shipped": they
describe what the code and tests actually do, verified directly against the
source and test suite, not restated from the original proposal.

Superseded (2026-07-13): the normalized RouterRequest/ChatMessage/TokenUsage/
RouterResponse request-response model and the request-driven capability hard
filter described in the PR 1-3 sections below were replaced by the
CallVariant / openai-SDK redesign (its own section, placed immediately after
PR 3). The PR 1-3 sections are left as-is below, not rewritten again -- they
remain an accurate historical record of what those PRs shipped at the time,
per this doc's own rule of updating a test/description only when a later PR
deliberately changes the behavior it describes, never deleting the record of
what came before. PR 4 onward is still the forward-looking plan and is
unchanged by the redesign except for the two new backlog entries it adds
(PR 21, PR 22), appended after PR 20. (Update 2026-07-14: that claim did
not survive contact with PR 4 -- its schema has since been revised to match
the redesign, adding backlog entries PR 23 and PR 24; see PR 4's second
revision note.)

Verified: `ruff check .`, `mypy src` (strict mode), and `pytest` all pass;
combined coverage across PR1-3 is 93% (branch coverage on), meeting the 90%+
target set. 

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

Testing philosophy
-------------------
These rules apply to every PR from here on, not just PR 1.

1. No monkeypatching of internal collaborators. Do not use
   `monkeypatch.setattr(...)` or `unittest.mock.patch(...)` to reach into a
   module and swap out a class/function/attribute it references internally
   (e.g. patching `nygen_router.router.OpenAICompatibleAdapter`). Patching
   internals like this couples the test suite to implementation details/module
   paths instead of the public API, so an unrelated refactor (renaming an
   import, restructuring a module) silently breaks tests that never should
   have known that detail existed.

   Instead, production code must expose a real seam for tests to use:
   a constructor parameter, an injectable factory/protocol, or an already-
   public extension point. Example already in this codebase:
   `OpenAICompatibleAdapter(config, transport=httpx.MockTransport(handler))` --
   the adapter accepts a `transport` argument, so tests inject a fake HTTP
   transport without patching anything. `ProviderRouter` follows the same
   pattern via its `adapter_factory` constructor argument, used instead of
   patching the adapter class it references internally.

   `monkeypatch.setenv` / `monkeypatch.delenv` for environment variables are
   not covered by this rule -- they set process state that the code under
   test is meant to read, not a fake replacing a real collaborator.

   Every PR after this one tends to add more collaborators to fake (policies,
   storage backends, health state) -- each of those needs its own constructor-
   level seam, following this same pattern, rather than a new patch target.

2. Do not delete existing tests as the project grows unless completely
   necessary, and only after careful consideration. Each PR's suggested test
   files (see each PR section below) are additive: new PRs get new test
   files alongside the existing ones, which keep acting as regression
   coverage for earlier behavior.

   A test may be updated (not deleted) when a later PR deliberately changes
   the behavior it was asserting -- e.g. PR 2 changes `invoke()` from "pick
   the first enabled provider, validate only that one" to "filter the whole
   list, then select among survivors," which changes what the PR 1 selection
   test's assertions mean. In that case, update the test to match the new,
   intentional behavior; do not delete it outright, and do not delete or
   weaken tests just because they are inconvenient to keep passing.

Review process
--------------
Per the supervisor meeting: share progress via GitHub and tag a PR for review
only when it's ready. Keep each PR small and scoped to one module, with a
short (~10 minute) intro when handing it off for review, rather than
bundling several modules' worth of change into one PR. PR1, PR2, and PR3
(and the prompt files that drove them, Projectplan/prompt.txt,
prompt_pr2.txt, prompt_pr3.txt) already follow this pattern -- keep doing so
for PR4 onward.

Recommended package structure
-----------------------------
nygen_router/
  __init__.py
  router.py
  types.py
  config.py
  errors.py
  capabilities.py
  filters.py
  health.py
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

As of PR3, the implemented files are: __init__.py, router.py, types.py,
config.py, errors.py, capabilities.py, filters.py, health.py,
adapters/{__init__.py, base.py, openai_compatible.py}, and
policies/{__init__.py, base.py, round_robin.py}. Everything else in this
tree (scoring.py, metrics.py, storage/, integrations/, observability/, the
remaining adapters, and score_based.py) is still planned, starting at PR4.

PR 1: Provider configs and real provider calls -- SHIPPED
------------------------------------------------------------
Goal:
Get the first real providers up and running, and prove the router can call
provider APIs through user-supplied provider configs and API keys.

What shipped (verified against src/nygen_router/):
- ApiProtocol StrEnum with three members: OPENAI_CHAT, OPENAI_RESPONSES,
  ANTHROPIC_MESSAGES. Only OPENAI_CHAT has a working adapter; the other two
  are reserved now (this addresses the supervisor meeting's requirement to
  support both the OpenAI protocol convention and the Response API -- the
  enum slot exists so the PR12 adapter doesn't need a breaking change) --
  see the Response API discussion note below.
- ProviderCapabilities and ProviderConfig, both Pydantic models, live in
  config.py (not a separate capabilities.py -- that file is reserved for
  PR2's hard-filter helper, see below). ProviderConfig validates: name/model
  non-empty, base_url required for OPENAI_CHAT, at least one of api_key
  (SecretStr) / api_key_env required, timeout_seconds > 0.
  resolve_api_key() prefers the explicit key and falls back to the named
  environment variable, raising MissingApiKeyError with a setup hint
  otherwise -- this is exactly the "explicit code-level key vs.
  environment-variable" option pair from the supervisor meeting, both
  wired through one method.
- RouterRequest, ChatMessage, TokenUsage, RouterResponse in types.py
  (Pydantic, extra="forbid"). RouterResponse.attempts (list[ProviderAttempt])
  and RouterResponse.excluded (list[EligibilityResult]) shipped in the
  schema from PR1 as planned, always empty/single-entry until PR2/PR3
  populate them for real.
- ProviderAdapter Protocol (adapters/base.py) and OpenAICompatibleAdapter
  (adapters/openai_compatible.py), built directly on httpx -- no OpenAI SDK
  dependency. Takes an injectable transport: httpx.BaseTransport, so tests
  use httpx.MockTransport instead of patching anything internal.
- errors.py error hierarchy: NygenRouterError base; ConfigError,
  MissingApiKeyError, UnsupportedProtocolError, NoProvidersConfiguredError
  for setup problems; ProviderError, ProviderTimeoutError,
  ProviderConnectionError, ProviderHTTPError, ProviderResponseError for call
  failures. ProviderHTTPError/ProviderResponseError carry the provider's
  verbatim message, HTTP status + reason phrase, and any error type/code
  field the provider returned, chained from the original httpx exception via
  raise ... from -- nothing is rephrased into a generic router message.
- ProviderRouter (router.py): constructor takes providers plus
  adapter_factory and policy as constructor-injectable seams (used by every
  test instead of patching an internal class). invoke() wraps a plain
  string into a RouterRequest and returns a RouterResponse.

Response API discussion (from the supervisor meeting, not yet resolved):
the meeting notes flag the Response API as "the emerging standard, most
tools moving toward it, OpenAI itself now adopting it." The current plan
only adds an OPENAI_RESPONSES adapter at PR12, after round robin (PR3),
DuckDB-backed storage (PR4), health/cooldowns (PR5), and scoring (PR6-10). Given the
emphasis in the meeting, it may be worth moving the Responses adapter
earlier in the sequence (e.g. right after PR3, before storage/scoring) --
this is a scope/sequencing question, not something resolved by this
rewrite, and is worth an explicit decision before PR4 starts.

Files actually added by PR1:
ProviderRouterPR1/
  pyproject.toml
  README.md
  AGENT.md
  src/nygen_router/
    __init__.py
    config.py
    types.py
    errors.py
    router.py
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

Minimum usage example (updated to build capabilities as a
ProviderCapabilities model, matching how config.py and the real tests do it
-- not a raw dict, per the supervisor meeting's "avoid raw dicts, use
dataclasses/enums/Pydantic models" rule):

from nygen_router import ProviderRouter, ProviderConfig, ProviderCapabilities, ApiProtocol

router = ProviderRouter(
    providers=[
        ProviderConfig(
            name="provider_a",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="some-model",
            base_url="https://provider-a.example.com/v1",
            api_key_env="PROVIDER_A_API_KEY",
            capabilities=ProviderCapabilities(supports_chat=True),
        ),
        ProviderConfig(
            name="provider_b",
            protocol=ApiProtocol.OPENAI_CHAT,
            model="some-model",
            base_url="https://provider-b.example.com/v1",
            api_key_env="PROVIDER_B_API_KEY",
            capabilities=ProviderCapabilities(supports_chat=True),
        ),
    ]
)

response = router.invoke("Say hello")
print(response.text)

Tests (tests/test_config.py, tests/test_openai_compatible_adapter.py,
tests/test_router_pr1.py):
- valid config accepted; empty name/model rejected; missing base_url
  rejected for OPENAI_CHAT; missing api_key and api_key_env together rejected
- explicit api_key resolves; api_key_env resolves from the environment; a
  missing env var raises MissingApiKeyError
- adapter builds the chat/completions payload (model, messages, bearer auth
  header); parses response text and token usage; rejects null/missing content
- adapter maps httpx timeout/connection/HTTP errors onto
  ProviderTimeoutError/ProviderConnectionError/ProviderHTTPError, preserving
  the original exception as __cause__
- router invokes the first enabled provider and normalizes a plain string
  into a RouterRequest

Note on CapabilityError: an earlier iteration of PR1 raised a dedicated
CapabilityError directly from capabilities.py when a provider lacked a
required capability (tool calls, streaming, JSON mode). It was retired the
moment PR2 landed (commit 137764e) and replaced by the EligibilityResult /
FilterReason exclusion model described below -- confirmed by diffing
errors.py across that commit. test_router_pr1.py was updated in place (not
deleted) to match, per the testing-philosophy rule that a test may change
when a later PR deliberately changes the behavior it asserts.

Also added, not in the original plan: tests/test_live_provider.py -- a live
integration test that sends one real request to a configured
OpenAI-compatible provider (DeepInfra by default) and asserts on the reply.
It is skipped via pytest.mark.skipif when its API key env var is unset, so
plain pytest stays offline and passes without credentials, while still
giving a way to prove real connectivity when a key is present.

PR 2: Essential hard filters -- SHIPPED
-----------------------------------------
Goal:
Before routing, filter out providers that cannot satisfy the request.

What shipped (verified against src/nygen_router/):
- capabilities.py: missing_capability(config, request) -- returns the first
  FilterReason the provider fails against the request's requires_tools /
  requires_streaming / requires_json_mode flags, or None if every required
  capability is present. (ProviderCapabilities itself, the Pydantic model,
  already shipped in PR1's config.py -- this file only holds the PR2 filter
  helper, contrary to the original suggested-files list which implied the
  model lived here.)
- filters.py: filter_eligible_providers(providers, request,
  supported_protocols=..., disabled_this_run=...) -- splits providers into
  (eligible, excluded). Checks in order: enabled, not auth-disabled this run
  (see PR3's health.py), API key resolvable, protocol supported, then
  missing_capability(). Each excluded provider yields exactly one
  EligibilityResult with its first failing FilterReason and a
  human-readable detail string.
- FilterReason (StrEnum) and EligibilityResult live in types.py, alongside
  the other response-schema types (not a separate file): DISABLED,
  AUTH_DISABLED_THIS_RUN, MISSING_API_KEY, UNSUPPORTED_PROTOCOL,
  MISSING_TOOLS, MISSING_STREAMING, MISSING_JSON_MODE.
- ProviderRouter.invoke() changed exactly as planned: filter the whole
  provider list first, then order/select among survivors, instead of
  validating only the first provider. If filtering excludes everyone,
  invoke() raises NoEligibleProvidersError(excluded), whose message
  enumerates every excluded provider with its own specific reason -- never
  one blended message.
- disabled_this_run is supplied by the router from its own per-run health
  state (ProviderHealthState, shipped in PR3 below), so an auth failure
  earlier in the run also becomes a hard-filter exclusion on the next call,
  through the same EligibilityResult/FilterReason machinery as a static
  config problem.

Confirmed: CapabilityError -- raised directly from capabilities.py in an
earlier PR1 iteration -- was retired the moment this PR landed (commit
137764e), replaced by the exclusion model above. See the note in PR1.

Files actually added by PR2:
src/nygen_router/capabilities.py
src/nygen_router/filters.py
tests/test_filters.py

(FilterReason/EligibilityResult additions to types.py, and the invoke()
control-flow change in router.py, are part of PR2 too, but land in files
PR1 already created.)

Tests (tests/test_filters.py):
- disabled provider excluded; provider without a resolvable API key
  excluded; unsupported protocol excluded
- provider without tools/streaming/JSON-mode excluded when the request
  requires them; a fully capable provider is eligible
- all providers filtered out raises NoEligibleProvidersError, whose message
  names each excluded provider with its own specific reason
- a successful call still reports every filtered-out provider in
  RouterResponse.excluded, and populates RouterResponse.attempts with the
  one provider actually invoked

PR 3: Round robin with current-run memory only -- SHIPPED
-------------------------------------------------------------
Goal:
Make the router actually rotate between providers during the current Python
process, and fall back to the next eligible provider on a retryable failure.

What shipped (verified against src/nygen_router/):
- policies/base.py: Policy as a typing.Protocol with one method,
  order(eligible) -> list[ProviderConfig] -- not an abstract base class.
- policies/round_robin.py: RoundRobinPolicy. Holds a private _index counter;
  order() rotates eligible so a different provider leads each call
  (i = index % len(eligible), then index += 1). It indexes into whatever is
  eligible right now, not into the original config list, so it self-heals
  automatically if the eligible set shrinks or grows between calls. No
  persistence -- the counter lives only for the life of the
  RoundRobinPolicy instance / Python process, exactly as planned.
- ProviderRouter(providers=[...]) needs no code change to get rotation +
  fallback: the constructor defaults policy to RoundRobinPolicy() when none
  is passed, matching the "same call signature as PR1" requirement. policy
  is an explicit constructor argument for swapping in a different policy
  later (PR9).
- health.py: ProviderHealthState, a dataclass with one field so far
  (auth_disabled: bool = False). Held on ProviderRouter as
  self._health: dict[str, ProviderHealthState], not inside the policy --
  exactly as planned, so the same state is visible to
  filter_eligible_providers (via disabled_this_run) and to any policy. PR5
  will extend this dataclass in place with cooldown_until /
  consecutive_failures rather than relocating it.
- ErrorCategory (StrEnum: TIMEOUT, RATE_LIMIT, AUTH, SERVER_ERROR,
  BAD_REQUEST, UNKNOWN) and categorize_error(exc) in errors.py. HTTP status
  mapping: 429->RATE_LIMIT, 401/403->AUTH, 408->TIMEOUT, >=500->SERVER_ERROR,
  400/422->BAD_REQUEST, everything else->UNKNOWN; an httpx-level timeout
  (ProviderTimeoutError) is always TIMEOUT.
- ProviderRouter.invoke()'s fallback loop iterates self._policy.order(eligible);
  on each provider's exception it records a ProviderAttempt(success=False,
  error=exc) (the real exception object, never rephrased), then branches by
  categorize_error(exc):
  - AUTH: marks self._health[provider.name] = ProviderHealthState(auth_disabled=True)
    (benches it starting next call -- this call already had it selected)
    and continues to the next provider.
  - BAD_REQUEST: stops the whole loop immediately (break) rather than
    continuing to the next provider. This is a real design decision beyond
    what the original plan specified -- the plan only said BAD_REQUEST
    "usually do not retry" without saying whether that meant "skip this
    provider" or "stop the whole call"; the shipped behavior is the latter,
    documented in AGENT.md and in router.py's comments: a 400/422 is almost
    always the request itself, so trying other providers would just add
    noise, not a fix.
  - everything else (TIMEOUT, RATE_LIMIT, SERVER_ERROR, UNKNOWN): continues
    to the next eligible provider.
  If every tried provider fails (or BAD_REQUEST cut the loop short),
  invoke() raises RouterExhaustedError(attempts), whose message enumerates
  each attempted provider with its own real failure.
- On success, the response is returned via
  response.model_copy(update={"attempts": attempts, "excluded": excluded})
  -- attempts contains every provider tried this call, in order, each with
  its real outcome.

Files actually added by PR3:
src/nygen_router/health.py
src/nygen_router/policies/__init__.py
src/nygen_router/policies/base.py
src/nygen_router/policies/round_robin.py
tests/test_round_robin.py
tests/test_fallback.py

Tests:
- tests/test_round_robin.py: rotation advances the starting provider across
  successive calls; rotation only considers eligible providers; an injected
  custom policy is honored instead of the default; ordering an empty
  eligible list returns empty
- tests/test_fallback.py: fallback tries the next provider on timeout, rate
  limit (429), a plain 404, HTTP 408 (treated as timeout), a 5xx server
  error, and an unrecognized/unknown error; an auth error (401/403) disables
  the provider for the rest of the run; a bad request (400/422) stops the
  run immediately without trying further providers, even after an earlier
  provider already failed; when every attempted provider fails,
  RouterExhaustedError names each one with its own distinct reason; a
  successful fallback's RouterResponse is JSON-serializable and keeps the
  real unwrapped error on the failed attempt(s)

Verified: pytest -q -> 55 passed; coverage report -> 93% (branch coverage
on) across PR1-3; ruff check . -> all checks passed; mypy src (strict) -> no
issues in 14 source files.

PR 3R: CallVariant / openai-SDK request-response redesign -- SHIPPED
----------------------------------------------------------------------
Goal:
Replace the normalized RouterRequest/RouterResponse request-response model
from PR1-3 with native, provider-specific pass-through, per a supervisor-
supplied design doc (Provider_router_design.pdf): "the router chooses where a
native API call is executed, it does not replace the provider API with a
generalized LLM interface." This is a full pivot of the request/response
contract every other module was built on, not an additive PR, so it lands as
one cohesive change rather than split across several.

What shipped (verified against src/nygen_router/):
- types.py: RouterRequest, ChatMessage, TokenUsage, RouterResponse deleted.
  New CallVariant(protocol, operation, arguments) -- a Pydantic model
  (extra="forbid") carrying a dotted SDK operation string (e.g.
  "chat.completions.create") and an opaque arguments: dict[str, object],
  never inspected or validated beyond basic shape. ProviderRouter.invoke()
  now takes calls: list[CallVariant] and returns Any -- the winning
  provider's raw SDK response object (e.g. openai.types.chat.ChatCompletion),
  completely untouched. There is no response wrapper: a successful call has
  no .attempts/.excluded anymore (that data is still tracked internally
  during the call, for RouterExhaustedError/NoEligibleProvidersError's
  enumerated messages, just not attached to a success).
- adapters/openai_compatible.py: the PR1 httpx-based adapter was scrapped
  entirely (not migrated incrementally) and rebuilt on the official `openai`
  Python SDK, lazily imported inside invoke() so the core package stays
  importable without it (pip install "nygen-router[openai]"). Dispatch is
  dynamic: CallVariant.operation is split on "." and walked via getattr onto
  an openai.OpenAI(base_url=..., api_key=...) client (used against any
  OpenAI-compatible base_url, not just OpenAI itself) -- chosen over a
  hardcoded per-operation method map so a new operation needs zero adapter
  changes. The router (not the adapter) resolves which CallVariant applies
  per provider attempt and injects the provider's configured model into
  arguments["model"] fresh each attempt (never mutating the CallVariant in
  place, since the same variant is reused across every same-protocol attempt
  in one fallback loop); a CallVariant whose arguments already contains
  "model" raises ModelArgumentConflictError before any provider is contacted.
  httpx is no longer a core dependency (it arrives transitively via the
  openai extra, and is used directly only as an injectable http_client test
  seam, the same role transport played in PR1).
- errors.py: new UnsupportedOperationError / InvalidOperationArgumentsError
  (ProviderError subclasses -- a bad operation string or arguments that don't
  match its resolved signature, discovered lazily at dispatch time, chained
  from the real AttributeError/TypeError); ModelArgumentConflictError /
  DuplicateCallVariantProtocolError (ConfigError subclasses, raised once
  upfront before any provider is contacted); ProviderSDKNotInstalledError
  (ConfigError, wraps a ModuleNotFoundError with an install hint). deleted
  ProviderResponseError (dead code once responses are never parsed).
  categorize_error() gained ErrorCategory.INVALID_OPERATION, grouped with
  BAD_REQUEST in the STOP set. openai SDK exceptions map onto the existing
  error hierarchy exactly like PR1's httpx-based mapping did (APITimeoutError
  -> ProviderTimeoutError, APIConnectionError -> ProviderConnectionError,
  APIStatusError -> ProviderHTTPError, everything else -> ProviderError), all
  chained via raise ... from so the original SDK exception stays reachable as
  __cause__/.original -- the "one base type catches everything" contract is
  preserved for every case, including dispatch failures. One correctness
  fix discovered while building this: openai.APIStatusError.message is an
  SDK-synthesized summary ("Error code: 404 - {...}"), not the provider's own
  text -- the verbatim message has to be pulled out of exc.body instead (see
  _verbatim_message in adapters/openai_compatible.py).
- filters.py/capabilities.py: capability-based hard filtering (the PR2
  requires_tools/requires_streaming/requires_json_mode mechanism) is dropped
  -- capabilities.py's missing_capability() had no other purpose and was
  deleted as dead code. filter_eligible_providers() dropped its request
  param and gained a keyword-only requested_protocols param; a provider whose
  protocol has an adapter but no matching CallVariant in this call is
  excluded via a new FilterReason.NO_MATCHING_CALL_VARIANT, through the same
  EligibilityResult machinery as every other exclusion. ProviderCapabilities
  (the Pydantic model on ProviderConfig) is unchanged and still there --
  simply unused by any filter for now; see PR 21 below.
- Test suite: every test file was updated in place (none deleted wholesale);
  three individual tests were deleted because the specific behavior they
  asserted was deliberately removed by this redesign, not because they were
  inconvenient: a response-JSON-serializability test (no more returned
  wrapper object to serialize), a string-to-message-normalization test (no
  more normalization -- arguments is opaque), and a capability-exclusion test
  (capability filtering is gone, see above). tests/test_openai_compatible_adapter.py
  was rewritten essentially from scratch: httpx.MockTransport is now wrapped
  in an httpx.Client and injected via the adapter's http_client constructor
  param (openai.OpenAI's own documented test seam, the direct analogue of
  PR1's transport param), and HTTP-error-family tests drive real openai SDK
  exceptions end-to-end through mocked HTTP responses rather than
  constructing router errors by hand.

Two deliberately deferred follow-ups, not part of this redesign -- see their
own backlog entries below: PR 21 (restoring pre-flight capability filtering,
now driven from a call's own arguments instead of dedicated boolean flags)
and PR 22 (pre-flight CallVariant dispatch validation, so a bad operation/
arguments typo is caught once upfront instead of costing one wasted live
provider attempt).

Files touched:
ProviderRouterPR1/
  pyproject.toml (httpx removed from core deps; openai added as its own
    extras group and to dev)
  README.md, AGENT.md (usage examples, hard-filtering and error sections
    rewritten for the new design)
  src/nygen_router/
    __init__.py (export changes)
    types.py (CallVariant added; RouterRequest/ChatMessage/TokenUsage/
      RouterResponse removed)
    errors.py (new error types; ProviderResponseError removed)
    router.py (new invoke() signature and _prepare_variants() upfront pass)
    filters.py (requested_protocols param; NO_MATCHING_CALL_VARIANT)
    capabilities.py (deleted)
    adapters/base.py, adapters/openai_compatible.py (rewritten)
  tests/ (all seven files updated; test_openai_compatible_adapter.py
    rewritten)

Verified: pytest -q -> 53 passed; coverage report -> 97% (branch coverage
on); ruff format ./ruff check . -> all checks passed; mypy src (strict) ->
no issues in 13 source files. Also verified in isolated venvs: a bare
`pip install -e .` (no extras) still allows `from nygen_router import
ProviderRouter`, and `pip install -e ".[openai]"` (no dev extras) runs a full
invoke() end-to-end.

PR 4: DuckDB-backed metrics storage (default), behind a swappable interface
-----------------------------------------------------------------------------
Goal:
Persist observational metrics so score-based routing (PR7-10) has real
history to work from, using DuckDB as the embedded, no-server-to-run default
-- swappable for SQLite or any other SQL-compatible backend behind one
shared interface.

Revision note (supervisor meeting, 2026-07-09): the original plan defaulted
to Python's stdlib sqlite3 module here, with any storage-backend abstraction
deferred to PR13 and DuckDB arriving later still (PR14) as an optional
analytics-only backend. The supervisor's meeting notes call for DuckDB as
the default local option and for a design that lets users plug in any
SQL-compatible database. This section is rewritten accordingly: DuckDB is
now the default, behind a small MetricsStore protocol introduced in this PR
(scoped to exactly what PR4 needs: record + query), so swapping backends
later is a matter of passing a different metrics_store, not a rewrite. This
does not pull PR13's full scope (schema versioning, migrations, a heavier
storage framework) forward -- PR13 is unchanged and still comes later, on
top of the same seam this PR establishes.

Revision note (design discussion, 2026-07-14): reconciled this section with
the PR 3R CallVariant redesign -- the 2026-07-13 superseded-note's claim
that "PR 4 onward is unchanged by the redesign" did not hold for this PR's
schema. input_tokens / output_tokens / required_tools / request_size_bucket
are dropped from the provider_attempts table: post-redesign none of them
has a data source (the router no longer parses responses, the
requires_tools flag no longer exists, and size buckets would need argument
inspection). Each column returns in the PR that creates its data source --
tokens in PR 24, request_size_bucket in PR 11, required_tools (if still
wanted) in PR 21 -- via the same additive-schema-change pattern PR 6
already uses for estimated_cost_usd. The same discussion pinned the
previously-unspecified details (timestamps, ids, what gets recorded,
latency measurement, missing-duckdb behavior, write mode, DB path), all
folded into the scope below. Streaming-specific metrics (TTFT, stream flag,
total duration, usage capture) belong to PR 23, not here: in this PR a
streaming call is recorded with latency measured around adapter.invoke()
(roughly time-to-stream-open) and success meaning "stream started" -- a
documented approximation PR 23 replaces.

Scope:
- storage/base.py: MetricsStore protocol -- record_attempt(event:
  MetricsEvent) -> None and query_recent(*, since, provider_name=None,
  model=None) -> list[MetricsEvent], chronological ascending. This is the
  minimum interface every backend (DuckDB, SQLite, and later Postgres/
  Supabase) implements, so callers switch backends by passing a different
  metrics_store, never by changing router code. The contract deliberately
  stays this small: aggregation happens in Python over what query_recent
  returns (pinned in PR 7), never in per-backend SQL, so a custom backend
  stays trivial to implement.
- metrics.py: MetricsEvent, a dataclass (not a raw dict) describing one
  row: id (uuid4 hex, default_factory), timestamp (timezone-aware UTC
  datetime via datetime.now(timezone.utc), default_factory), provider_name,
  model, protocol, success, latency_ms, error_type. Stores serialize the
  timestamp as ISO-8601 UTC text -- generated in Python, never in SQL -- so
  TEXT comparison stays chronologically correct and both engines behave
  identically.
- storage/duckdb.py: DuckDBMetricsStore(path=...), the new default.
  Lazy-imports duckdb inside its own methods (never at module import time),
  following the same lazy-SDK-import pattern already used for provider
  adapters -- so `from nygen_router import ProviderRouter` keeps working
  with duckdb not installed (the project's non-negotiable lightweight-
  import rule). path is a constructor parameter defaulting to
  ~/.nygen_router/metrics.duckdb, created on first use -- a real file, not
  an in-memory database, so history survives across separate runs of a
  script (needed for the "router learns over time" demo target). The
  default path is documented single-process: DuckDB allows one writing
  process per file, so a second process sharing the file fails its writes
  (harmlessly -- see Important behavior below). Users who need several
  local processes sharing one store are pointed at SQLiteMetricsStore;
  multi-machine setups at PR 14's Postgres backend.
- Missing-duckdb warning: DuckDBMetricsStore's constructor checks
  importlib.util.find_spec("duckdb") (an availability check, not an import,
  so the lazy-import rule stays intact) and emits one stdlib logging
  warning with an install hint when the package is absent. Silent
  no-persistence is not acceptable: without this, a user who believes they
  have history gets an empty "router learns over time" demo with zero
  signal. PR 19 remains the PR for the richer per-event logging; this
  single warning is pulled forward.
- storage/sqlite.py: SQLiteMetricsStore(path), kept as a fully-supported
  alternative (Python stdlib only, no extra install) implementing the same
  MetricsStore protocol -- and the recommended option when several local
  processes must share one store, since SQLite handles cross-process file
  locking natively (a real differentiator from DuckDB, stated in the
  README).
- Writes are synchronous, on the calling thread: one row per provider
  attempt is sub-millisecond for an embedded database. Deferred idea
  (backlog, not this PR): async/batched writes behind a bounded in-process
  queue drained by a background writer thread -- takes writes off the hot
  path but adds thread lifecycle, shutdown flushing, and test-determinism
  machinery, and does not solve cross-process locking (the queue lives
  inside one process). Revisit within PR 13 if synchronous inserts ever
  show up in practice.
- New optional dependency group: `pip install nygen-router[duckdb]`.
  Without it, ProviderRouter still constructs (with the construction-time
  warning above) and invoke() still works: the default DuckDBMetricsStore's
  write attempt fails with an ImportError, which the storage-failure
  handling below treats like any other storage failure -- the successful
  provider response is still returned. Document nygen-router[duckdb] in the
  README as the recommended "batteries-included" install.
- First table (provider_attempts), same columns regardless of engine --
  only columns with a real data source today; later PRs add theirs (tokens:
  PR 24, request_size_bucket: PR 11, required_tools: PR 21, cost: PR 6,
  stream/total_duration_ms: PR 23):
CREATE TABLE provider_attempts (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    model TEXT NOT NULL,
    protocol TEXT NOT NULL,
    success INTEGER NOT NULL,
    latency_ms REAL,
    error_type TEXT
);

What gets recorded, and by whom:
- The router records exactly one MetricsEvent per ProviderAttempt --
  successes and failures both -- from the fallback loop, immediately after
  each attempt resolves. error_type carries the ErrorCategory value string
  on failure and NULL on success. Exclusions (filtered-out providers) are
  not recorded: scoring needs attempt outcomes, and exclusions are
  reconstructible from config/health.
- latency_ms is one time.perf_counter() window around adapter.invoke(),
  recorded exactly as measured -- nothing added, subtracted, or adjusted --
  on failures too (a timeout's duration is itself signal).
- Every record_attempt call is wrapped in its own try/except so a storage
  error can never disturb the LLM call, and is skipped entirely when
  metrics_store=None. Storage remains observable without flooding or
  contaminating provider output: the router logs one short warning on the
  first write failure, keeps retrying later writes without repeating that
  warning, writes the full exception and traceback only at DEBUG level, and
  logs one INFO recovery message with the number of unrecorded attempts if a
  later write succeeds. A missing DuckDB dependency is already warned about
  during DuckDBMetricsStore construction, so its failed writes do not produce
  a second warning. Explicit metrics_store=None is intentional and silent.

Suggested files:
src/nygen_router/metrics.py
src/nygen_router/storage/__init__.py
src/nygen_router/storage/base.py
src/nygen_router/storage/duckdb.py
src/nygen_router/storage/sqlite.py
tests/test_metrics_store.py (shared protocol-conformance tests,
  parametrized over both backends -- structured so a user can point the
  same suite at a custom backend; the README's "bring your own backend"
  section explains how, part of making swappability practical rather than
  theoretical)
tests/test_duckdb_storage.py
tests/test_sqlite_storage.py
tests/test_router_metrics.py (router-to-store wiring)

Important behavior:
Storage failure should not break a successful LLM response. If the provider
succeeds but metrics storage fails -- including "duckdb is not installed" --
return the provider response.

Default storage (works out of the box once the duckdb extra is installed;
degrades to a construction-time warning plus no persistence, not a crash,
otherwise):
ProviderRouter defaults to metrics_store=DuckDBMetricsStore() pointed at
~/.nygen_router/metrics.duckdb. Because None must keep meaning "disable
persistence entirely", the constructor distinguishes "not passed" from None
with a module-level sentinel default rather than None. `metrics_store`
remains fully overridable: pass SQLiteMetricsStore(path), a different
MetricsStore implementation, or metrics_store=None to disable persistence
entirely.

Tests:
- MetricsStore protocol conformance run against both DuckDBMetricsStore and
  SQLiteMetricsStore, so the two backends can't silently diverge in behavior
- creates schema on first use; records success event; records failure event
  (error_type = the ErrorCategory value string); queries recent events in
  chronological order; ignores events outside the lookback window
- timestamps round-trip as timezone-aware UTC; ids are unique
- the router records one event per attempt, successes and failures both, in
  fallback order, and records nothing for excluded providers
- router continues if metrics storage fails, including a simulated "duckdb
  not installed" ImportError (via an injected fake store raising it -- no
  monkeypatching of import machinery)
- constructing DuckDBMetricsStore without duckdb available logs one warning
  (asserted via caplog, driven through a constructor seam) and does not
  raise
- default metrics_store writes to ~/.nygen_router/metrics.duckdb when not
  otherwise configured and duckdb is installed; a custom path is honored
- metrics_store=None disables persistence with no file created
- DuckDB-specific tests are skipped (not failed) when the duckdb package is
  not installed, matching the pattern already planned for PR14's other
  optional backends

PR 5: Health state and cooldowns
--------------------------------
Goal:
Avoid repeatedly calling providers that are temporarily bad, and make
every bench visible with its real cause -- never silently.

Revision note (design interview, 2026-07-15): this section was rewritten
from the original four-line behavior sketch after a full design review;
every decision below is pinned. Two corrections to the old text are
folded in: the auth-disabled scope item already shipped in PR 3
(health.py, FilterReason.AUTH_DISABLED_THIS_RUN, covered by
test_fallback.py), so this PR extends it rather than introducing it; and
the old test list asserted on RouterResponse.excluded, which the PR 3R
redesign deleted -- exclusions are now observable via
NoEligibleProvidersError.exclusions, the bench logging below, and the new
health_report().

Revision note (implementation, 2026-07-15): PR 5 shipped as pinned with
one deliberate exception -- the scope of the bench-logging dedup, now
corrected in the Transparency section below. The pinned text read "dedup
per provider ... every subsequent bench of that same provider logs at
DEBUG", leaving reset_health() as the only way to re-arm the WARNING.
Read literally that is a transparency hole: a provider that benches,
recovers, and benches again hours later reports the second, genuinely
separate outage at DEBUG only -- invisible at the default log level.
That contradicts this PR's own goal that nothing is ever benched
silently, and it does so precisely for the long-lived routers this plan
names as the primary use case. The dedup is therefore scoped to a bench
EPISODE rather than to the provider's lifetime: record_success() also
clears the warning flag -- a fourth thing it clears, beyond the
consecutive_failures / cooldown_until / last_error listed in the state
scope below -- so each distinct outage warns exactly once, while repeat
benches within one outage still collapse to DEBUG. The anti-spam
property the dedup existed for is unchanged, since a persistently broken
provider never succeeds in between. reset_health() still re-arms the
warning by dropping the entry. Nothing else about the decision changed:
the WARNING still carries provider name, trigger, duration, and the
verbatim last error.

Scope -- state (health.py):
- ProviderHealthState gains cooldown_until (monotonic float | None),
  consecutive_failures (int), and last_error (str | None -- the
  stringified real error from the most recent counted or benching
  failure), alongside the shipped auth_disabled flag.
- Transition logic lives on the dataclass, not in the router loop:
  record_failure(category, error_text, config, now) and
  record_success(); the fallback loop only calls these. This also fixes
  a latent bug: router.py currently *replaces* the state object on an
  auth failure (self._health[name] =
  ProviderHealthState(auth_disabled=True)), which would silently zero an
  existing failure count once that field exists -- PR 5 switches to
  get-or-create + mutate.

Behavior per ErrorCategory:
- RATE_LIMIT: enter cooldown for rate_limit_cooldown_seconds
  immediately. Neither increments nor resets consecutive_failures (a 429
  is flow control, not "provider is off").
- TIMEOUT, SERVER_ERROR, CONNECTION (new), UNKNOWN: increment
  consecutive_failures; reaching failure_threshold (default 3) enters
  cooldown for failure_cooldown_seconds. UNKNOWN counts on purpose: its
  two real families -- provider-specific HTTP statuses (404 from a wrong
  base_url or a model this provider doesn't host, 413 payload limits)
  and the defensive catch-all -- are both per-provider problems.
  Genuine caller-side typos never reach here: bad operation strings /
  arguments are INVALID_OPERATION and malformed requests are
  BAD_REQUEST, both STOP categories that abort the run loudly before
  health is involved.
- AUTH: full-run bench exactly as shipped in PR 3, now also recording
  last_error. Does not increment the counter.
- BAD_REQUEST, INVALID_OPERATION (STOP): never touch health state -- the
  fault is the call, not the provider.
- SUCCESS: resets consecutive_failures to 0, clears cooldown_until and
  last_error.
- New ErrorCategory.CONNECTION: categorize_error() gains an isinstance
  branch so ProviderConnectionError maps to "connection" instead of
  falling through to UNKNOWN -- "provider unreachable" is the strongest
  something-is-off signal there is, and metrics/scoring (PR 7-8) should
  see it by name. It behaves like the other counted categories.

Configuration (HealthConfig, in health.py, exported from the package
root):
- Pydantic model, extra="forbid": rate_limit_cooldown_seconds: float =
  60.0, failure_cooldown_seconds: float = 60.0 (same default on purpose;
  two knobs so they can diverge later), failure_threshold: int = 3.
  Durations validated > 0, threshold >= 1.
- ProviderRouter gains health: HealthConfig | Mapping[str, object] |
  None = None. None means HealthConfig() -- zero configuration required
  by default. A mapping is validated into HealthConfig at the boundary
  (typos raise immediately via extra="forbid"), so overrides need no
  import; the README documents the typed form first and the dict form as
  the quick path -- the same accept-and-validate pattern
  ProviderConfig.capabilities already has.

Clock:
- Cooldowns run on time.monotonic (immune to wall-clock jumps), injected
  as a constructor seam: clock: Callable[[], float] = time.monotonic on
  ProviderRouter -- the same seam pattern as adapter_factory/policy, per
  the testing philosophy (no monkeypatching, no sleeps; tests inject a
  fake clock and advance it). Reports expose remaining seconds, never
  absolute deadlines. Documented caveat: monotonic excludes suspend time
  on most platforms, so a laptop suspend stretches a cooldown's wall
  duration -- benign for the long-running active workflows this router
  targets.

Filtering (filters.py, types.py):
- Cooldown/auth-benched exclusions are hard filters, not a separate
  mechanism: filter_eligible_providers() consults health state directly,
  replacing disabled_this_run: Collection[str] with health: Mapping[str,
  ProviderHealthState] and now: float (a deliberate signature change to
  an internal function; its tests update in place, per the
  testing-philosophy update rule).
- New FilterReason.IN_COOLDOWN, one member for both triggers; the detail
  string distinguishes them and carries the evidence: "in cooldown
  (12.3s remaining) after 3 consecutive failures; last error:
  <verbatim>" vs "... after rate limiting; ...". The
  AUTH_DISABLED_THIS_RUN detail gains the stored last_error the same
  way. When everything is benched, NoEligibleProvidersError therefore
  enumerates root causes, not just cooldown states.
- Same-call semantics unchanged from PR 3's auth behavior: a bench taken
  mid-invoke() applies from the next call; the current fallback loop
  already holds its ordered list.

Transparency (one slice pulled forward from PR 19, same precedent as
PR 4's missing-duckdb warning -- silent benching is not acceptable):
- Every bench is logged with provider name, trigger, duration, and the
  verbatim last error. Dedup per bench EPISODE, adapting PR 4's pattern:
  the first bench of an episode = WARNING, repeat benches within that
  same episode = DEBUG, first success after a bench = one INFO recovery
  line, which also re-arms the warning so the next distinct outage warns
  again (see the implementation revision note above). A user with a
  typo'd base_url sees the provider's own 404 text in the first warning.
- reset_health(provider_name: str | None = None) on ProviderRouter:
  clears cooldown, failure count, auth bench, and last_error -- "treat
  this provider as brand new" -- for one provider or (None) all. An
  unknown name raises ConfigError: a typo'd reset that silently no-ops
  is the exact silent failure this PR exists to prevent. Use case: quota
  upgraded or API key fixed mid-run, retry now instead of waiting out
  the bench. Never touches stored metrics -- MetricsStore has no delete
  path; every recorded attempt survives forever. Stated consequence:
  once PR 8-9 land, a just-reset provider may still rank low from its
  recorded history within the lookback window; reset means "may be tried
  again now" (hard filter), not "forget what happened" (scoring) --
  design principle 8's filter/score split.
- health_report() -> dict[str, ProviderHealthReport] (frozen dataclass:
  auth_disabled, consecutive_failures, cooldown_remaining_seconds: float
  | None, last_error: str | None), one entry for every configured
  provider, healthy ones showing zeros/None; defensive copies only --
  no live mutable state escapes. You cannot decide whether to reset
  without being able to look. Exported from the package root alongside
  HealthConfig.

Invariants (pinned; the second emerges from "the count resets only on
success"):
- One invoke() never loops: the fallback loop iterates a finite ordered
  list in which each eligible provider appears at most once; the ceiling
  is one attempt per eligible provider, then RouterExhaustedError. PR 23
  mirrors the same guarantee for mid-stream restarts.
- Probe-per-window: cooldown expiry does not reset
  consecutive_failures. A still-broken provider whose bench lapses is
  re-benched by its next counted failure immediately (the count is still
  >= threshold), so after the first bench a persistently failing
  provider costs one failed probe per cooldown window, not three. With
  every provider misconfigured, steady state is at most N probes per
  window plus instant, zero-network NoEligibleProvidersError fast-fails
  enumerating each root cause. A global "total failures, then give up
  forever" counter was considered and rejected: for overnight
  long-running workflows it would turn a transient multi-hour outage
  into a permanently dead router; a caller who wants give-up-after-N can
  count the enumerated exceptions themselves.

Metrics: no schema change and no recording change. Every attempt is
still recorded exactly as PR 4 ships it, before any health decision;
exclusions (including cooldown skips) stay unrecorded per PR 4's pinned
decision. The only visible metrics effect is ProviderConnectionError now
recording error_type "connection" instead of "unknown".

Relationship to PR3:
Extends the same ProviderHealthState / self._health tracker in place --
never relocating it. It remains owned by ProviderRouter, in memory,
shared by every policy, living and dying with the router instance: two
router objects have independent health, a process restart starts clean,
and an application that constructs a new ProviderRouter per request
accumulates no health signal at all (stated in the README; the
protection targets long-lived routers, the project's primary use case).

Deferred (backlog, not this PR):
- Honor Retry-After on 429s instead of the fixed knob (parse both
  delta-seconds and HTTP-date forms, cap absurd values) -- cleanly
  additive later; ProviderHTTPError already carries the raw response.
- max_attempts_per_call: bounds worst-case single-call latency
  (len(eligible) x timeout_seconds) -- a latency cap, not loop
  protection; typical 2-5 provider setups don't need it.
- Escalating/exponential cooldowns for repeat benches.

Suggested files:
src/nygen_router/health.py (HealthConfig, ProviderHealthReport, extended
  ProviderHealthState with record_failure/record_success)
src/nygen_router/errors.py (ErrorCategory.CONNECTION)
src/nygen_router/types.py (FilterReason.IN_COOLDOWN)
src/nygen_router/filters.py (health-consulting signature)
src/nygen_router/router.py (clock + health constructor params,
  reset_health, health_report, bench logging, get-or-create health
  writes)
src/nygen_router/__init__.py (export HealthConfig, ProviderHealthReport)
tests/test_health.py (new: state transitions, config validation, clock)
tests/test_fallback.py, tests/test_filters.py,
  tests/test_router_metrics.py (updated in place)

Tests:
- a 429 benches the provider for rate_limit_cooldown_seconds and leaves
  consecutive_failures untouched
- three counted failures (mixes of timeout / server error / connection /
  unknown) bench for failure_cooldown_seconds
- ProviderConnectionError categorizes as CONNECTION and records
  error_type "connection"
- RATE_LIMIT, AUTH, BAD_REQUEST, INVALID_OPERATION do not increment the
  counter
- success resets the counter and clears cooldown_until / last_error
- a benched provider is excluded with FilterReason.IN_COOLDOWN and a
  detail carrying trigger, remaining seconds, and the verbatim last
  error
- the auth-bench exclusion detail carries the verbatim last error
- advancing an injected fake clock past the cooldown makes the provider
  eligible again; its next counted failure re-benches it immediately
  (probe-per-window: expiry did not reset the count)
- with every provider benched, invoke() raises NoEligibleProvidersError
  with zero adapter invocations, enumerating each provider's last error
- bench logging: the first bench of an episode is one WARNING with the
  verbatim error, a repeat bench with no intervening success logs DEBUG,
  first success after a bench logs one INFO recovery, and a bench after
  that recovery warns again (asserted via caplog)
- health_report() returns an entry per configured provider (healthy ones
  zeros/None), reports remaining seconds, and returns defensive copies
  (mutating the report does not change router state)
- reset_health() clears one provider / all providers, makes a benched
  provider immediately eligible, raises ConfigError on an unknown name,
  and leaves stored metrics rows untouched
- an auth failure on a provider with an existing failure count preserves
  that count (regression for the replace-write fix)
- HealthConfig: defaults apply with nothing passed; an equivalent dict
  is accepted and validated; an unknown key, non-positive duration, or
  zero threshold is rejected
- a bench taken mid-call does not affect the current call's already-
  ordered fallback loop; it applies from the next invoke()

PR 6: Token cost calculation (deferred, optional)
--------------------------------------------------
Status:
Deferred and out of the core/near-term scope. Cost is not a factor in the
router's routing or scoring decisions (see Project goal). This PR exists as
a possible later, opt-in customization for users who want cost visibility,
not as something the core sprint depends on. It does not gate PR 7-10 or
the "80% sprint" boundary below -- PR 7's stats, PR 8's scoring factors, and
PR 9's routing policy are all designed to work with zero cost data.

Goal (if/when built):
Calculate estimated cost from user-provided pricing.

Scope:
- TokenPricing
- cost calculation helper
- cost field in metrics event
- adds the estimated_cost_usd column to the provider_attempts schema (PR 4
  ships without this column; this PR adds it via a schema change)

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
Turn raw provider_attempts events into a per-provider stats bundle that
PR 8's score calculator can consume, correctly once PR 23's streaming
support (the `stream` column) has shipped.

Revision note (design interview, 2026-07-23): this section, together with
PR 8-10, was walked through in a full design interview and every decision
below is pinned. Two changes from the original one-line scope: aggregation
is now query-only (no window logic inside it -- callers pass an
already-filtered event list, matching the pinned "aggregation happens in
Python over query_recent's output" rule below) and the per-provider stats
are split by call type (regular vs. streaming) rather than blended,
because PR 23's own design already measures and records the two
differently (time-to-complete vs. time-to-first-chunk, success decided at
different moments) -- blending them into one number would hide
operation-specific weaknesses a streaming-heavy workflow needs visible.
This PR does not depend on PR 6, PR 11, PR 21, or PR 24's still-unshipped
columns, but it does depend on PR 23 having shipped, since it reads the
`stream` column PR 23 adds to provider_attempts -- confirm PR 23 is merged
before starting this PR.

Provider identity fix (bundled here as a prerequisite, not a separate PR):
ProviderRouter.__init__ now rejects two configured providers sharing the
same `name` with a ConfigError naming every duplicate, raised at
construction, before any call is made. Per-provider metrics identity is
keyed by name (matching how health tracking already works, and preserving
the case where two entries share one base_url/model but use separate API
keys for separate rate-limit quotas -- those must keep separate
histories). Renaming a provider in configuration therefore starts its
recorded history over; this is accepted as a self-healing cost bounded by
the lookback window, and is documented in the README.

Scope:
- src/nygen_router/stats.py (new file):
  - ProviderStats, a frozen dataclass, one entry per provider:
      provider_name: str
      regular_attempt_count: float
      regular_success_count: float
      regular_success_rate: float | None       (None iff regular_attempt_count == 0)
      regular_avg_latency_ms: float | None      (None iff regular_success_count == 0; successful attempts only)
      streaming_attempt_count: float
      streaming_success_count: float
      streaming_success_rate: float | None
      streaming_avg_ttft_ms: float | None        (time-to-first-chunk, PR 23's latency_ms meaning for stream rows; completed attempts only)
      recent_error_count: int
      rate_limit_count: int
      timeout_count: int
    The four count fields are typed float, not int, even though PR 7
    always produces whole numbers -- reserved purely so PR 10 can populate
    them with fractional decayed weights later without a breaking type
    change to this dataclass. recent_error_count / rate_limit_count /
    timeout_count stay plain, always-exact integer tallies -- diagnostic
    only, never fed into scoring (see PR 8's note on why recent-error-count
    was dropped as a scoring input) -- and PR 10's decay never touches
    them.
  - aggregate_stats(events, provider_names, *, weight_fn=None) ->
    dict[str, ProviderStats]: one entry for every name in provider_names,
    including providers with zero matching events (all counts 0, all
    rates/averages None) -- mirrors health_report()'s "every configured
    provider gets an entry" precedent, since PR 8's optimistic-blend needs
    a real entry to fall back on. A provider whose regular figures are
    populated and streaming figures are all-zero (or vice versa) is normal
    and expected. weight_fn: Callable[[MetricsEvent], float] | None is
    accepted but unused by this PR -- every event counts as weight 1.0
    when it is None -- reserved purely so PR 10 can supply a decay-based
    weight function without changing this signature. Events for names not
    in provider_names are ignored. Split into regular/streaming buckets via
    event.stream (PR 23's column: False/0 = regular, True/1 = streaming).
- Average latency is computed over successful (or, for streaming,
  completed) attempts only, per the standing PR 23 pin -- a provider that
  fails fast must never look fast. Streaming rows' latency_ms
  (time-to-first-chunk) and non-streaming rows' latency_ms (full-response
  time) are never blended into the same average -- fully resolved by the
  regular/streaming split above, superseding the old "decided when this PR
  starts" placeholder.
- No cost stat (see PR 6 -- deferred, optional, out of core scope). No
  per-model grouping: a ProviderConfig fixes one model per provider entry,
  so provider identity already implies model identity; MetricsStore's
  existing query_recent model filter remains available to anyone who wants
  to slice manually.

Tests:
- a provider with only regular-call events gets populated regular_* fields
  and all-zero/None streaming_* fields, and vice versa
- success rate and average latency are computed correctly from a mix of
  successes and failures
- average latency excludes failed/incomplete attempts (a fast failure does
  not pull the average down)
- a provider with zero events gets a fully-zero/None entry rather than
  being omitted
- events for a provider not in provider_names are ignored
- recent_error_count / rate_limit_count / timeout_count tally correctly
  from a mix of ErrorCategory values
- regular and streaming attempts for the same provider are never blended
  into one figure
- weight_fn, when supplied, is applied per-event instead of a flat 1.0 (a
  minimal test using a trivial weight function, e.g. always 2.0, proving
  the seam works -- real decay logic is PR 10's)
- constructing ProviderRouter with two providers sharing a name raises
  ConfigError naming both

PR 8: Basic score calculator
----------------------------
Goal:
Turn one provider's ProviderStats into a single comparable score, with
zero I/O.

Revision note (design interview, 2026-07-23): pinned in full, superseding
the "recent errors" and "exploration bonus" scoring factors named in the
original one-line scope -- both are removed, not merely renamed.
- Recent-error-count is dropped as a scoring input. Within one lookback
  window, recent_error_count is mathematically attempt_count x (1 -
  success_rate) -- entirely derivable from a success rate the score
  already uses -- and it is also volume-sensitive in a way success rate is
  not: a provider tried more often (simply because it keeps winning the
  ranking) would accumulate a larger raw count at an identical success
  rate, unfairly dragging its score down for having been used more. It
  remains a plain, unweighted stat on ProviderStats (PR 7) for
  diagnostics; it is not read anywhere in this PR.
- The "exploration bonus" as a separate, togglable feature is dropped and
  replaced by the optimistic-start blend described below, which serves the
  same purpose (new/thin-history providers still get tried) as an
  always-on property of how every score is computed, not an opt-in extra.
- Rate-limit hits are not a separate scoring input either: a provider
  actively rate-limited is already excluded entirely by the hard filter
  before scoring runs, and once eligible again its rate-limited attempts
  already lowered its success rate -- a dedicated penalty on top would
  double-count the same event and would contradict the health design's own
  treatment of a 429 as flow control, not a reliability signal.

Scope:
- src/nygen_router/scoring.py (new file):
  - ScoreWeights, a Pydantic model (extra="forbid"), the single settings
    object for every tunable number this PR introduces:
      success_weight: float = 1.0                     (validated >= 0)
      speed_weight: float = 1.0                        (validated >= 0)
      regular_latency_reference_ms: float = 2000.0     (validated > 0)
      streaming_ttft_reference_ms: float = 500.0       (validated > 0)
      optimistic_start: float = 0.75                   (validated 0 <= x <= 1)
      optimistic_start_pretend_attempts: float = 5.0   (validated > 0)
    A model_validator rejects success_weight == 0 and speed_weight == 0
    simultaneously (the weighted average below would be undefined);
    setting exactly one of the two to 0 is valid and simply drops that
    factor.
  - ProviderScore, a frozen dataclass: provider_name: str, total: float,
    success_quality: float, speed_quality: float. The two quality
    components are kept on the result (not just the total) so a low score
    is explainable, not a black box.
  - calculate_provider_score(stats: ProviderStats, weights: ScoreWeights,
    *, use_streaming: bool = False) -> ProviderScore:
    - Picks the regular_* or streaming_* fields from stats depending on
      use_streaming.
    - success_quality: the picked success_rate blended toward
      optimistic_start -- blended = (pretend * optimistic_start +
      real_count * observed) / (pretend + real_count), using the picked
      attempt_count as real_count and the picked success_rate as observed
      (0.0 when real_count is 0, which the formula's own weighting makes
      irrelevant). This single formula needs no special case for "no data
      at all": with real_count == 0 it reduces exactly to
      optimistic_start.
    - speed_quality: the picked avg_latency_ms (None when there were zero
      successes of that type) is converted via quality = reference_ms /
      (reference_ms + latency_ms) -- 0.5 at the reference point,
      approaching 1 as latency approaches 0, never reaching a hard 0 --
      then blended toward optimistic_start the same way, using the picked
      success_count as real_count (latency only exists for successes, so
      success_count, not attempt_count, is the right evidence size here).
      When avg_latency_ms is None, the blend still reduces to
      optimistic_start since real_count is 0.
    - total: the weighted average of success_quality and speed_quality
      using weights.success_weight / weights.speed_weight --
      (success_weight * success_quality + speed_weight * speed_quality) /
      (success_weight + speed_weight), never a plain sum. A weighted
      average, not a sum, is required specifically so the result always
      stays between 0 and 1 regardless of the absolute weight values, and
      so weights can be entered as plain relative importance without
      needing to sum to any particular total.

Rules (unchanged from the original plan, now with a concrete mechanism to
enforce them):
- calculate_provider_score must not call providers, must not write to
  storage, must not import anything from adapters/ or storage/ -- it is a
  pure function of (ProviderStats, ScoreWeights, bool) -> ProviderScore.

Note: cost is deliberately not a default scoring factor (see Project goal
and PR 6). If manual cost tracking is ever built, it could be wired in as
an additional optional weight in ScoreWeights later, but it is not part of
the core scoring model.

Tests:
- higher success rate improves the score, holding everything else fixed
- lower latency improves the score, holding everything else fixed
- a provider with zero relevant attempts scores exactly optimistic_start
  on both components
- a provider with few attempts (fewer than
  optimistic_start_pretend_attempts) scores closer to optimistic_start
  than its raw observed numbers would suggest; a provider with many
  attempts scores close to its raw observed numbers
- setting a weight to 0 removes that component's influence entirely;
  setting both to 0 raises at ScoreWeights construction time
- use_streaming=True reads the streaming_* fields and use_streaming=False
  reads the regular_* fields, from the same ProviderStats
- the total score is always between 0 and 1 for any valid weights and any
  valid stats
- ScoreWeights: defaults apply with nothing passed; each numeric field's
  invalid range (negative weight, non-positive reference, out-of-[0,1]
  optimistic_start, non-positive pretend-attempts) is rejected

PR 9: Score-based routing policy
--------------------------------
Goal:
Use PR 7's aggregation and PR 8's scoring to rank eligible providers,
falling back through the ranked order exactly as round robin already
falls back through its rotated order.

Revision note (design interview, 2026-07-23): pinned in full; supersedes
RoutingProfile (folded into PR 15's routing profiles, not built here) and
the "unknown provider is occasionally explored" test (superseded by PR 8's
always-on optimistic-start blend, which needs no separate exploration
mechanism or test).

- Policy interface change (src/nygen_router/policies/base.py): a
  deliberate, one-time breaking change, taken now while no policy code
  outside this package exists.
    RoutingContext, a frozen dataclass: metrics_store: MetricsStore |
    None -- always the router's own store, built fresh by the router on
    every invoke() call, so a policy can never read from a store other
    than the one the router itself writes to (the mismatch that motivated
    this design is structurally impossible under this shape). Deliberately
    grown additively in future PRs (e.g. a request-size bucket in PR 11)
    -- never repurposed for anything except per-call runtime data.
    Policy.order gains a second parameter: def order(self, eligible:
    list[ProviderConfig], context: RoutingContext) ->
    list[ProviderConfig]. RoundRobinPolicy.order is updated to accept and
    ignore context; its existing tests are updated in place for the new
    signature (no behavior change).
  - router.py's invoke() builds context =
    RoutingContext(metrics_store=self._metrics_store) fresh each call and
    passes it to self._policy.order(eligible, context).
- src/nygen_router/policies/score_based.py (new file):
    ScoreBasedPolicy(
        *,
        weights: ScoreWeights | None = None,          # None -> ScoreWeights()
        lookback_hours: float = 336.0,                 # 14 days; validated > 0
        use_streaming: bool = False,                   # which call type this policy scores for; see below
        tie_break_policy: Policy | None = None,        # None -> RoundRobinPolicy()
        now: Callable[[], datetime] = lambda: datetime.now(UTC),   # clock seam, same pattern as ProviderRouter's clock=
    )
  - order(self, eligible, context) -> list[ProviderConfig]:
    1. rotated = self._tie_break_policy.order(list(eligible), context) --
       establishes both the tie-break order and the graceful-degradation
       order in one call.
    2. If context.metrics_store is None or not rotated, return rotated
       unchanged (no history to learn from -> behaves exactly like the
       tie-break policy, by construction, with no special-case code).
    3. since = self._now() - timedelta(hours=self._lookback_hours); query
       context.metrics_store.query_recent(since=since) inside a
       try/except. On any exception, log once (dedup: first failure this
       policy instance logs WARNING, later ones DEBUG, reusing the PR
       4/PR 5 dedup pattern) and return rotated unchanged -- a broken
       store degrades routing to round robin, it never breaks a call.
    4. stats = aggregate_stats(events, [p.name for p in rotated]) (PR 7);
       score each provider via calculate_provider_score(stats[p.name],
       self._weights, use_streaming=self._use_streaming).total (PR 8).
    5. return sorted(rotated, key=score, reverse=True) using Python's
       stable sort -- providers with equal scores keep their relative
       order from step 1's rotation, which is exactly how ties are
       broken. This one stable sort is the entire tie-break mechanism; no
       separate grouping logic is needed or should be written.
  - use_streaming is deliberately a one-time constructor setting, not
    inferred per call. The router cannot know before selecting a provider
    whether the call about to be made is a streaming call without
    inspecting the call's own arguments, and PR 23 deliberately avoids
    exactly that (its own text: "Detection by response type, not argument
    inspection... needs no new exception here") -- it only learns a call
    was a stream after a provider has already responded, too late for
    ranking. Rather than carve out a new, narrow argument-inspection
    exception to solve this, the router instead assumes a given policy
    instance is used for predominantly one call type or the other over its
    lifetime -- a real limitation, explicit and configured by the user,
    not inferred or silently guessed. A ProviderRouter whose workload
    genuinely mixes both types on one instance would need two
    ScoreBasedPolicy instances (one per call type) selected by the caller,
    or accept that whichever type is not configured is scored against the
    wrong history -- documented plainly in the README, not hidden.
  - Correct behavior, unchanged from the original plan: this policy ranks
    all eligible providers and returns the full ranked list;
    ProviderRouter's existing fallback loop (unchanged by this PR) tries
    them in that order, falling back on failure exactly as it already
    does under round robin.
- src/nygen_router/__init__.py: export Policy, RoutingContext,
  ScoreBasedPolicy, ScoreWeights, ProviderScore, ProviderStats,
  aggregate_stats, calculate_provider_score (Policy and RoutingContext
  were not previously exported; this PR is the first to need them from
  outside policies/base.py).

Tests:
- the best-scoring eligible provider is ordered first; fallback proceeds
  through the full ranked list on failure exactly as round robin does
- providers with equal scores are ordered by the tie-break policy's own
  order (default: round robin's rotation) -- verified by asserting the
  relative order of two providers whose stats are engineered to produce
  identical scores
- with an empty or unavailable metrics store (None, and a fake store whose
  query_recent raises), ordering falls back to exactly the tie-break
  policy's order, and the failure is logged (caplog) with dedup: first
  failure WARNING, repeats DEBUG
- score-based routing only ever ranks the list it is given -- providers
  already excluded by hard filtering never appear in its output
  (regression-style test using the same eligible/excluded split
  filter_eligible_providers already produces)
- use_streaming=True scores using each provider's streaming history;
  use_streaming=False (the default) uses regular-call history; a policy
  configured for one never lets the other's history influence its ranking
- lookback_hours bounds what history is queried (an old event outside the
  window does not affect the ranking) -- using the injected now= seam to
  make this deterministic, no monkeypatching of datetime
- RoundRobinPolicy.order accepts a RoutingContext argument and its
  existing rotation behavior is unchanged
- a custom Policy implementation (satisfying the new two-argument order
  signature) can still be injected via ProviderRouter(policy=...) exactly
  as before

PR 10: Recency weighting
------------------------
Goal:
Let recent performance count for more than old performance smoothly,
rather than a hard in/out cutoff, without breaking anyone using PR 9's
flat-window default.

Revision note (design interview, 2026-07-23): pinned as a strict extension
of PR 9, not a redesign -- PR 9's flat lookback_hours window remains the
default behavior; this PR adds an optional, off-by-default alternative.
The original plan's "start simple... add exponential decay if needed" is
resolved: build it now, opt-in, so users who want smoother behavior do not
need to wait for a future PR, while everyone else's behavior is untouched.

Scope:
- src/nygen_router/stats.py: no signature change (aggregate_stats's
  weight_fn parameter already exists, unused, since PR 7). This PR is the
  first caller to supply a real weight_fn.
- src/nygen_router/policies/score_based.py: ScoreBasedPolicy gains one new
  constructor parameter, half_life_hours: float | None = None (validated
  > 0 when not None).
  - When None (default): behavior is byte-identical to PR 9 --
    lookback_hours bounds the query, every event within it counts
    equally. This must be verified by a regression test, not just
    asserted in prose.
  - When set: half_life_hours takes over entirely from lookback_hours for
    this policy instance (the two are not combined -- mixing a hard outer
    bound the user tunes with a smooth inner one adds a confusing
    interaction for no real benefit). The query's since bound is derived
    automatically as self._now() - timedelta(hours=6 * half_life_hours)
    -- six half-lives, a fixed internal constant (not user-configurable),
    chosen because weight at that age is 0.5**6 ~= 1.6%, small enough that
    any missed contribution beyond it is immaterial. A weight function
    weight(age_hours) = 0.5 ** (age_hours / half_life_hours) is built from
    self._now() and passed as aggregate_stats's weight_fn -- age_hours is
    computed per-event as (self._now() - event.timestamp) in hours.
  - Every place PR 7/PR 9 treated a count as "how much real evidence
    exists" (PR 8's blend, via ProviderStats' float-typed count fields)
    automatically becomes the decayed effective count once weight_fn is
    supplied -- older evidence contributes a fraction of an attempt
    rather than a whole one, so a run of successes from three weeks ago
    no longer overrides a bad run from this morning the way an
    equally-weighted count would. This requires no change to PR 8's
    calculate_provider_score, which was already written against
    float-typed, possibly-fractional counts from PR 7 onward for exactly
    this reason.
- recent_error_count / rate_limit_count / timeout_count on ProviderStats
  stay plain, unweighted, exact tallies of what happened within the
  queried window regardless of half_life_hours -- decay only affects the
  scoring-relevant fields, never these diagnostic counts.

Tests:
- with half_life_hours=None, results are identical to an equivalent PR 9
  run with the same lookback_hours (regression test, not just an
  assertion of intent)
- an event from many half-lives ago contributes a negligible, not zero,
  amount to the score (assert the effect is small, not that it is absent)
- a recent failure lowers the score more than an equally-old failure from
  several half-lives back
- a recent success raises the score more than an equally-old success from
  several half-lives back
- the query bound sent to the metrics store's query_recent reflects the
  six-half-life derivation, not lookback_hours, when half_life_hours is
  set
- an invalid half_life_hours (zero or negative) is rejected at
  ScoreBasedPolicy construction
- recent_error_count / rate_limit_count / timeout_count are unaffected by
  half_life_hours (still exact, unweighted tallies)

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

Schema: this PR adds the request_size_bucket column to provider_attempts
(PR 4 ships without it -- additive schema change, per the PR 6 pattern).
Estimating request size means inspecting CallVariant.arguments -- the same
kind of narrow, deliberate exception to the "router never interprets
provider-shaped arguments" principle that PR 21 makes; reuse PR 21's
machinery where possible.

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

Note (PR 23 design interview, 2026-07-17): this adapter's
NormalizedStream wrapper -- reading the Responses API's typed terminal
events (e.g. response.completed) -- is what closes PR 23's documented
truncation-detection blind spot for responses streams. Until this PR
lands, a responses stream dispatched through an OPENAI_CHAT provider
runs with exception-based fallback but no truncation verdict, flagged
at runtime by PR 23's unrecognized-shape WARNING.

Tests:
- responses adapter builds correct payload
- responses adapter parses text output
- responses adapter parses token usage if present
- router can use responses protocol provider

PR 13: Storage backend abstraction upgrade
------------------------------------------
Goal:
Upgrade the MetricsStore protocol introduced in PR4 (currently just
record_attempt/query_recent, backing DuckDB and SQLite) to support
remote/managed SQL-compatible backends -- Supabase, Postgres, and others --
plus schema versioning as the provider_attempts shape evolves (e.g. PR6's
estimated_cost_usd column).

Revision note (design discussion, 2026-07-15): two decisions pinned for
this PR. First, the SQL implementation layer must stay agnostic:
MetricsStore (PR 4) remains the only abstraction the router and routing
logic ever see, and whichever SQL technology implements a given store --
SQLAlchemy Core, SQLAlchemy ORM, or direct DBAPI SQL -- is a private
detail inside that store. No sessions, engines, ORM models, or raw rows
appear in any public signature; reads are converted to MetricsEvent (or
successor) dataclasses before being returned to routing logic. Second,
the choice between SQLAlchemy Core and SQLAlchemy ORM (or a mixed
Core/ORM approach) is deliberately left open: it is an explicit
discussion to have when this PR starts, not something this note resolves.
Deferring it is safe because the PRs before this one are independent of
this seam (and largely of each other) -- landing PR 5-12 first does not
make this PR harder, so the decision loses nothing by waiting.

Scope:
- SQL schema versioning / migrations
- storage initialization for remote/managed backends (connection handling,
  credentials -- distinct from DuckDB/SQLite's embedded, file-based setup)
- richer stats query interface

Do not overbuild a full ORM too early. PR4 already established the minimal
storage protocol and two embedded backends; this PR extends that seam
rather than introducing it.

PR 14: Supabase/Postgres backend (cloud default)
--------------------------------------------------
Goal:
Add the cloud-managed storage option -- Supabase (managed Postgres) as the
default cloud backend, per the supervisor meeting, for teams that want
shared/centralized routing history instead of a local DuckDB/SQLite file.
DuckDB, the local default, already shipped in PR4, so this PR no longer
needs to add it.

Scope:
- PostgresMetricsStore (works against both self-hosted Postgres and
  Supabase, since Supabase is managed Postgres), implementing PR4's
  MetricsStore protocol
- optional dependency group (e.g. pip install nygen-router[postgres])
- tests skipped if the postgres driver / a live connection is not available

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
- most_reliable
- tool_heavy

Note: no "cheapest" profile -- cost is deferred/optional (see PR 6) and
isn't part of the core scoring model, so a cost-driven profile doesn't apply
unless/until PR 6 ships.

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

PR 21: Capability-based hard filtering v2 (automatic inference)
-----------------------------------------------------------------
Goal:
Restore pre-flight capability exclusion, dropped by the PR 3R CallVariant
redesign (see that section above) when the normalized request's requires_tools
/ requires_streaming / requires_json_mode flags went away.

Scope:
Infer what a call's own CallVariant.arguments need (e.g. "tools" present in
arguments, "stream": True, "response_format" present) and compare that
against each provider's already-shipped, currently-unused
ProviderConfig.capabilities (supports_tools / supports_streaming /
supports_json_mode), producing the same EligibilityResult/FilterReason
exclusion machinery the old requires_* mechanism did -- new FilterReason
members analogous to the retired MISSING_TOOLS / MISSING_STREAMING /
MISSING_JSON_MODE. This necessarily means filter_eligible_providers() (or its
caller) inspects argument keys per protocol to know what a given key implies
-- a deliberate, scoped exception to the "router never interprets
provider-shaped arguments" principle, justified because pre-flight exclusion
is worth the cost of this one narrow translation.

Schema: if the required_tools metrics column is still wanted, this PR is
where it gains a data source and gets added to provider_attempts (PR 4
ships without it -- additive schema change, per the PR 6 pattern).

Tests:
- provider without tools excluded when a CallVariant's arguments include
  "tools"
- provider without streaming excluded when arguments include "stream": True
- provider without JSON mode excluded when arguments include a
  response_format
- a fully capable provider remains eligible

PR 22: Pre-flight CallVariant dispatch validation
----------------------------------------------------
Goal:
Deferred from PR 3R (see that section above): catch a bad operation string or
mismatched arguments once, before the fallback loop starts, instead of
discovering it lazily on the first live provider attempt.

Scope:
Before ordering/trying any provider, resolve each supplied CallVariant's
operation via the same getattr walk the adapter uses, and check arguments
binds to the resolved callable's real signature via
inspect.signature(...).bind(...) -- which raises immediately, with no network
call, if a required keyword is missing or an unexpected one is present. Only
validate CallVariants for protocols that currently have at least one eligible
provider (no point validating a variant nobody will use). On failure, raise
the same UnsupportedOperationError/InvalidOperationArgumentsError used today,
just before any provider has been attempted -- so a caller's typo never costs
a wasted provider attempt or triggers an early STOP mid-fallback that strands
otherwise-untried, still-eligible providers.

Tests:
- a bad operation string is caught before any provider is invoked
- arguments that don't bind to the resolved operation's signature are caught
  before any provider is invoked
- a CallVariant for a protocol with zero eligible providers is not validated
- a valid CallVariant still proceeds through the normal fallback loop

PR 23: RouterStream -- streaming fallback and observation
------------------------------------------------------------
Goal:
Make streaming calls first-class (decided 2026-07-14, with PR 4's
revision). Until this PR, invoke() returns the raw SDK stream and the
router's involvement ends there: a stream that dies mid-generation after a
successful start gets no fallback, no specific error, and its PR 4 metrics
event only says "stream started". For the router's primary use case --
long-running automated workflows, possibly overnight -- dying without
fallback after a successful start is the worst failure mode, so mid-stream
fallback is the default behavior.

Revision note (sequencing decision, 2026-07-15): pulled forward -- this PR
is implemented immediately after PR 5, before the scoring chain (PR 7-10),
not at its backlog position. Two reasons: mid-stream death without
fallback is the worst failure mode for the primary use case (previous
paragraph), and PR 4 records a streaming call's success at stream open,
so scoring built in PR 7-10 would otherwise learn from systematically
optimistic streaming data. Two consequences of landing here: this PR's
stream / total_duration_ms columns become the first additive schema
change to hit existing persistent metrics files (ahead of PR 24 and
PR 6), so this PR establishes the on-init check-and-ALTER-TABLE-ADD-COLUMN
convention those later PRs reuse; and PR 24 now lands second of the
PR 23 / PR 24 pair, so PR 24 is the one that wires both usage sources
into the token columns (per the usage-capture bullet below).

Revision note (design interview, 2026-07-17): the design was walked
end-to-end before implementation and every open decision is pinned
below; the usage-capture bullet and the streaming test bullet are
amended in place where they contradicted the sequencing note.

Revision note (empty-stream correction, 2026-07-27): a fully consumed stream
that yields zero chunks is now a `ProviderStreamInterruptedError`, even if its
wrapper reports completion or an unrecognized shape. The earlier rule could
record such an attempt as successful despite producing no usable response,
and left streaming TTFT undefined for a counted success. The empty attempt is
recorded as a streaming failure with NULL TTFT and measured total duration,
counts toward provider health, and follows the configured
`stream_failure_policy`: restart by default or raise when requested. Consumer
close remains unchanged and never invents a failure for an outcome the caller
did not observe.

- Seam: the SDK-specific streaming knowledge (stream detection,
  mid-iteration exception mapping, chunk interpretation) lives behind
  the adapter boundary as a wrapped stream. adapters/base.py gains
  NormalizedStream, an ABC (not a Protocol -- the router needs a
  reliable isinstance): __next__ yields the SDK's raw chunks unchanged
  and re-raises any SDK/transport exception mapped onto the router
  error hierarchy (chained via raise ... from); completed reports
  whether a completion marker was seen; usage holds the
  provider-reported usage object, if any; close() propagates. The
  openai adapter's invoke() wraps an openai.Stream in OpenAIChatStream
  (same file) before returning; router.py detects streams via
  isinstance(response, NormalizedStream) and stays entirely SDK-free.
  The adapter's existing except-chain is extracted into one shared
  _map_sdk_exception helper used by both invoke() and the wrapper --
  one copy of the mapping -- gaining httpx transport-error branches
  (raw transport errors can escape unwrapped during SSE iteration; the
  dispatch-specific TypeError/AttributeError clauses stay in invoke()
  only). The ProviderAdapter protocol itself is unchanged: a custom
  adapter that returns a raw stream passes through untouched exactly
  as today (no mid-stream fallback); returning a NormalizedStream is
  the documented opt-in.
- Truncation verdict only with evidence after at least one chunk: the silent-truncation
  contract applies only when the wrapper recognized the chunk shape
  (saw choices-bearing chunks). A stream whose chunks were never
  recognized keeps exception-based fallback, metrics, and close
  propagation, but its clean end counts as completed -- the router
  never invents a failure it cannot evidence (PR 24's
  unfamiliar-shapes posture). The first such stream per operation
  string per router logs one WARNING stating that truncation detection
  is not available for that operation's stream shape; repeats log
  DEBUG (the PR 4/PR 5 dedup pattern). Documented residual risk,
  accepted: an OpenAI-compatible provider that streams recognized
  chunks but never sends finish_reason is indistinguishable from a
  graceful truncation; if a real provider ever bites, the remedy is a
  per-provider opt-out knob added on evidence (the Retry-After
  precedent). See the note added to PR 12: its own wrapper closes this
  blind spot for responses streams. Independently, zero chunks is direct
  evidence that no usable response was produced and is always a failure.
- ProviderStreamInterruptedError categorizes as a new
  ErrorCategory.STREAM_INTERRUPTED member -- visible by name to
  metrics and scoring (the CONNECTION precedent) -- counted toward
  failure_threshold (a chronically truncating provider benches), and
  never a STOP category.
- Health mirrors metrics in time: record_success() fires only at
  completed stream end; a mid-stream failure records at failure time;
  stream open touches neither. Success-at-open would let a provider
  whose streams open fine and die mid-generation oscillate 0->1 and
  never reach the threshold.
- Consumer close: close() always stops the underlying stream, never
  triggers a restart, is idempotent, and is terminal -- later
  iteration raises StopIteration (Python's closed-generator
  semantics). At close, the observed outcome is recorded: success iff
  the completion marker was already seen (the break-on-finish_reason
  pattern still feeds scoring history); otherwise nothing is recorded
  -- the one documented exception to one-event-per-attempt, because an
  attempt whose outcome the caller declined to observe has no outcome
  to record. A bare break without close() runs no router code and
  records nothing; the README recommends the context manager for
  observed early stops.
- on_restart is a ProviderRouter constructor argument (no per-call
  form, matching stream_failure_policy), receiving one frozen
  dataclass: failed provider name, the real error, next provider name,
  chunks already delivered, restart count -- an object so fields can
  be added later without breaking existing callbacks. A callback's own
  exception propagates, never swallowed.
- Structure: the rule-carrying steps (metrics recording, health
  transitions, per-provider argument building, failure classification)
  each exist exactly once as shared helpers; invoke() and RouterStream
  keep two thin, separate loops -- no attempt-engine rewrite of the
  just-shipped invoke().
- Timing on failures: latency_ms on stream rows always means TTFT and
  is NULL when no chunk ever arrived -- never repurposed for another
  quantity; total_duration_ms is recorded for failed streams too (open
  to death -- a fixed-time proxy cutoff is invisible without it) and
  the duration also appears in the failure log line. A streaming call
  that fails before the stream opens is necessarily recorded stream=0
  (detection is by response type and no response exists), so the
  stream flag means "a stream actually opened", not "the caller wanted
  streaming".
- Restart anomaly, pinned defensively: if a restart's invoke() returns
  something that is not a NormalizedStream, that response is closed if
  possible and the attempt is recorded as a failure with a synthesized
  ProviderError naming the cause; fallback continues. Chunks of two
  generations must never interleave, and a non-stream response cannot
  be yielded to a consumer iterating chunks.
- Scoring guard (recorded here, applied in PR 7 -- see the note added
  there): average latency must be computed over successful attempts
  only, and streaming TTFT must not be blended with non-streaming
  full-response latency.

Why the logic lives in a wrapper: for a streaming call the failure happens
after invoke() has already returned -- the exception surfaces inside the
consumer's own iteration loop, when no router code is on the call stack.
The only code of ours that runs during streaming is the object being
iterated, so that object (RouterStream) is the only physically possible
home for streaming fallback. From the consumer's side nothing changes:
they write `for chunk in router.invoke(...)` exactly as against the raw
SDK stream.

Scope:
- Detection by response type, not argument inspection: after
  adapter.invoke() returns, if the result is the SDK's stream type the
  router wraps it in a RouterStream carrying the not-yet-tried ranked
  providers; otherwise the response is returned untouched exactly as
  today. The "router never interprets provider-shaped arguments" principle
  needs no new exception here.
- RouterStream is a pass-through iterator: yields the raw SDK chunks
  unchanged (never a router-invented marker object), no buffering, no text
  accumulation, constant memory over arbitrarily long streams. Per-chunk
  work is one finish_reason check plus capturing the final usage chunk --
  microseconds against the 10-50ms network interval between chunks;
  time-to-first-token is unaffected. close() and context-manager support
  propagate to the underlying SDK stream, so a consumer's break / Ctrl-C
  cleans up naturally.
- Completion contract: a stream succeeded iff it yielded at least one chunk
  and iteration ended after a chunk carrying a finish_reason. A zero-chunk
  stream failed to produce a usable response. A non-empty stream that ends
  cleanly without a completion marker was silently truncated by the provider
  -- there is no HTTP status and no SDK exception for either case, so the router
  synthesizes its own
  ProviderStreamInterruptedError (new ProviderError subclass, with its own
  ErrorCategory mapping). Mid-iteration transport/SDK exceptions go
  through categorize_error as usual. (Verify at implementation time
  exactly which exception types the openai SDK raises during stream
  iteration -- flagged medium-confidence in the design discussion.)
- Fallback at any point: on a mid-stream exception or silent truncation,
  RouterStream records the failed attempt and continues the same ranked
  provider order the router computed -- each eligible provider tried at
  most once (the natural restart guardrail; no separate max-restarts knob
  unless a real need appears), STOP categories abort immediately as
  always. A restart after chunks were already yielded means the new
  provider regenerates from scratch -- generations cannot be spliced -- so
  the consumer's accumulated partial output must be discarded, which is
  why:
- Restarts are observable, never silent: an optional on_restart callback
  (fired only when chunks had already been yielded -- a restart at zero
  chunks has nothing to discard), a stdlib logging warning when a restart
  happens with no callback registered, and a restarts counter on the
  wrapper. Silently corrupted accumulated output would be worse than a
  crash for an overnight workflow.
- stream_failure_policy: StrEnum constructor argument on ProviderRouter --
  RESTART (default: the behavior above, zero setup configuration required)
  or RAISE (record the failed attempt, then re-raise the provider's real
  error, per the transparency principle, for callers who want to stop on
  any mid-stream failure). STOP categories raise under both policies. An
  enum rather than a bool so a third mode can be added without a breaking
  change; no per-call override for now.
- Metrics (additive schema change to provider_attempts, PR 6 pattern):
  adds stream INTEGER NOT NULL DEFAULT 0 and total_duration_ms REAL. For
  streams, latency_ms means time-to-first-token (the right cross-provider
  responsiveness signal -- total duration depends on output length, which
  varies call to call); total_duration_ms is recorded for completed
  streams. A stream's metrics event is written at stream end by
  RouterStream, not at invoke() return, replacing PR 4's documented
  "success means stream started" approximation.
- Usage capture: when the caller's arguments include stream_options
  {"include_usage": true}, the final chunk carries usage; the adapter's
  stream wrapper pockets that object and exposes it as
  NormalizedStream.usage -- a property present on the ABC from birth,
  so PR 24 never has to make a breaking change to an already-shipped
  base class. Nothing about usage is persisted in this PR: the token
  columns and their recording are PR 24's, which lands second and
  wires both usage sources into them (per the 2026-07-17 revision
  note above).

Tests:
- a non-streaming call returns the identical raw response object, exactly
  as before this PR
- chunks pass through unchanged and in order
- a mid-stream exception falls back to the next provider; on_restart fires
  and the restarts counter increments when chunks had been yielded
- a failure before any chunk was yielded falls back without firing
  on_restart
- a stream ending without finish_reason is recorded as
  ProviderStreamInterruptedError and falls back
- a restart with no callback registered logs a warning
- stream_failure_policy=RAISE re-raises the provider's real error
  immediately, no fallback
- a STOP-category failure aborts under both policies
- exhausting every provider raises RouterExhaustedError enumerating each
  attempt's own real reason
- close()/breaking the loop closes the underlying SDK stream
- for streams: latency_ms records TTFT (NULL if no chunk ever arrived)
  and stream=1; total_duration_ms recorded on completion and on
  mid-stream failure (open to death); the event is written at stream
  end; the wrapper's usage property holds the provider-reported usage
  object after an include_usage stream completes (recording it into
  token columns is PR 24's test)
- an unrecognized-shape stream: a clean end counts as completed, a
  mid-stream exception still falls back, and the first such stream per
  operation logs one WARNING (repeats DEBUG)
- close() before the completion marker records nothing; close() after
  the marker records the observed success; a closed stream raises
  StopIteration on further iteration; close() is idempotent
- a provider one failure below the threshold whose stream opens fine
  and dies mid-generation reaches the threshold and benches (health
  records at stream end, not open)
- STREAM_INTERRUPTED increments consecutive_failures and is never a
  STOP category

PR 24: Non-streaming response usage extraction
-------------------------------------------------
Goal:
Restore input_tokens/output_tokens metrics, dropped from PR 4 when the
PR 3R redesign made responses opaque (decided 2026-07-14, with PR 4's
revision).

Scope:
- A best-effort getattr read of response.usage (prompt_tokens /
  completion_tokens) on the response object the router already holds
  between adapter.invoke() returning and invoke() returning -- no copy, no
  wrapper, no mutation; the caller receives the identical raw SDK object.
  Unfamiliar or absent shapes yield None, never an error.
- This is a documented, scoped exception to the raw-pass-through rule --
  reading, never altering -- in the same spirit as PR 21's
  argument-inspection exception.
- Adds input_tokens INTEGER and output_tokens INTEGER (both nullable) to
  provider_attempts (additive schema change, PR 6 pattern). See PR 23 for
  the streaming side of usage capture.

Tests:
- usage extracted and recorded when present
- absent or unfamiliar usage shapes record NULL without error
- the returned response object is the identical object the adapter
  produced
- token columns populated in the recorded MetricsEvent

Recommended sprint boundary
---------------------------
For an 80% sprint target, aim to complete PR 1 through PR 10.

Sequencing update (2026-07-15): PR 23 (RouterStream) is pulled forward and
implemented immediately after PR 5, before PR 7-10 -- see PR 23's revision
note. The implementation order is therefore PR 5, PR 23, then PR 7-10.

That gives the project:
- real provider calls
- hard filters
- round robin
- fallback
- DuckDB-backed metrics storage
- health/cooldowns
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
The router stored latency, success, and token usage.
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
