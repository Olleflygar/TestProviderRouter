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
- rate limit count
- timeout count
- recent error count

Aggregation happens in Python over the MetricsEvents that query_recent
returns -- not in per-backend SQL (pinned 2026-07-14, with PR 4's
revision). This keeps the MetricsStore contract at record + query so
custom backends stay trivial to implement; revisit only if event volume
ever makes Python-side aggregation a real bottleneck.

Note: no cost stat here -- cost is deferred/optional (see PR 6) and is not
part of the core aggregation this PR needs to produce.

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
- recent errors
- exploration bonus

Note: cost is deliberately not a default scoring factor (see Project goal
and PR 6). If manual cost tracking is ever built, it could be wired in as an
additional optional weight in ScoreWeights later, but it is not part of the
core scoring model.

Tests:
- higher success rate improves score
- lower latency improves score
- recent errors reduce score
- unknown provider gets exploration bonus

PR 9: Score-based routing policy
--------------------------------
Goal:
Use the persisted metrics history (DuckDB by default, or whichever
MetricsStore backend is configured -- see PR4) to rank providers.

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
- Completion contract: a stream succeeded iff iteration ended after a
  chunk carrying a finish_reason. A stream that ends cleanly without one
  was silently truncated by the provider -- there is no HTTP status and no
  SDK exception for this case, so the router synthesizes its own
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
  {"include_usage": true}, the final chunk carries usage; RouterStream
  captures it into the token columns (added by PR 24 -- whichever of
  PR 23 / PR 24 lands second wires both sources into those columns).

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
- for streams: latency_ms records TTFT and stream=1; total_duration_ms
  recorded on completion; the event is written at stream end; usage
  captured when include_usage was requested

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
