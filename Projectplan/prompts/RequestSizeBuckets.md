# Request-size estimation and routing buckets — design note

## Status: scrapped

**PR11 and automatic request-size routing buckets were scrapped and are not
part of the active roadmap. The design below must not be implemented as
currently written.**

The proposal depends on estimating request size by inspecting opaque
`CallVariant.arguments`. Even a lightweight estimator adds work before every
call and introduces semantic dependencies: the router must decide which values
are text, tools, documents, images, or other data and then guess how those
values relate to provider cost and latency. Exact tokenization increases CPU
and memory pressure and adds model-specific dependencies, while character-based
estimation is cheaper but substantially more ambiguous.

Base64 payloads make the problem especially clear. Counting an encoded image or
PDF can mean reading millions of characters and treating them as prompt text.
Avoiding that error requires guessing the payload type and assigning an
arbitrary substitute size. URLs and provider file IDs create the opposite
problem: a short argument can reference a very large document that the router
cannot measure without fetching and interpreting provider-owned content.

This conflicts with the router's native pass-through, transparency, and
lightweight-core principles. The motivating use case is already handled more
explicitly by `metrics_scope`: callers can partition short chats, document
analysis, vision, or other materially different workloads without the router
interpreting their call arguments. The formerly reserved `request_size_bucket`
field and column were removed from the event record and metrics schema on
2026-08-05.

The remainder of this document is the original proposed PR11 design. It is
preserved as historical rationale, including the alternatives and tradeoffs
that were considered, but it is not an active implementation plan.

## Original proposed design (historical)


Status: design settled 2026-07-31, not yet implemented. Based on a survey of seven
router/gateway repos (Bifrost, CARROT, LiteLLM, RouteLLM, WeaveRouter, AvengersPro,
OmniRoute). Relates to PR11 in `Projectplan/` and the `request_size_bucket` column
already reserved in `storage/base.py`.

PR29 correction (shipped 2026-08-03): the nullable event field and exact-schema
database column are now present, and router-produced events leave them NULL.
PR11 must populate and use the existing field; it must not add the column or
revive the removed `_ADDED_COLUMNS`/implicit `ALTER TABLE` migration pattern.

## Why size buckets

Provider latency varies with prompt size: a provider that is fast on a 2k-token
prompt may be slow on a 60k-token one. Today every observation for a provider goes
into one pooled average, so score-based routing compares providers on evidence
gathered at wildly different request sizes. Bucketing splits that evidence so
providers are compared against requests of roughly the same size — the same
argument `stats.py` already makes for keeping regular and streaming measurements
apart.

Survey finding worth recording: **none of the seven surveyed projects segments
latency by request size.** LiteLLM computes input tokens in its latency strategy
and uses them only for a rate-limit gate. OmniRoute stores token counts in the same
table as latency and never joins them. WeaveRouter's speed score uses a constant
instead of the request's actual size. This feature has no prior art to copy; the
estimator below does.

## Design principle: a stable partitioning function, not a token counter

The estimator does not try to predict billing. It needs exactly two properties:

1. **Deterministic** — the same request always gets the same label.
2. **Monotone** — a bigger request never gets a smaller label than a smaller one.

The estimate is computed **once per call, before any provider is chosen**.
Whichever provider serves the request — or several, across fallbacks — every
attempt carries the same bucket label. Estimator error therefore **cannot favor
one provider over another**; it can only blur what a bucket means (file some
image-carrying requests among the mediums). Within any bucket, provider A and
provider B are always compared on the identical population of requests.

This is why token accuracy is deliberately not a goal. Errors are accepted
wherever they stay within roughly a factor of ten and apply identically to every
request of the same shape.

## The estimator

Requirements: zero dependencies (the core package depends on `pydantic` alone, and
`tests/test_lightweight_import.py` enforces it), no knowledge of any provider's
message schema (`CallVariant.arguments` is opaque by design), and per-request cost
that is negligible against a network call.

A real tokenizer is deliberately not used. LiteLLM ships bundled tiktoken vocabs
and a HuggingFace tokenizer registry, yet its own size-based complexity router uses
`len(text) // 4` — and it carries a kill switch for its real token counter because
BPE counting on the hot path is a measured CPU problem in production. Exact
counting buys precision on an axis the buckets deliberately make coarse: adjacent
boundaries are 8× and 4× apart, so an estimate off by ±25% almost never changes
the bucket.

```python
from collections.abc import Mapping

_CHARS_PER_TOKEN = 4           # ~1 token per 4 characters of ordinary text
_OPAQUE_MIN_CHARS = 5000       # bare-base64 trigger: only strings longer than this
_OPAQUE_SAMPLE_CHARS = 64      # how much of a string the base64 glance reads
_OPAQUE_PAYLOAD_CHARS = 10_000 # flat stand-in for any caught payload: ~2500 tokens
_B64 = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


def _is_opaque_payload(value: str) -> bool:
    # Two spellings of inline binary exist, and they fail each other's test, so
    # both triggers are needed:
    #   1. Data-URL form (OpenAI-style): "data:image/png;base64,<blob>". Keyed on
    #      the ";base64," marker -- a wire-format artifact no prose, YAML, or log
    #      line plausibly contains -- not on the word "data:", which collides
    #      with ordinary text.
    #   2. Bare form (Anthropic-style): the blob alone in a field. A long string
    #      whose opening is pure base64 language; real text betrays itself within
    #      a few characters (spaces, punctuation, non-ASCII).
    if value.startswith("data:") and ";base64," in value[:128]:
        return True
    return len(value) > _OPAQUE_MIN_CHARS and _B64.issuperset(
        value[:_OPAQUE_SAMPLE_CHARS]
    )


def _text_length(value: object) -> int:
    if isinstance(value, str):
        if _is_opaque_payload(value):
            # The string is a file spelled as text. Its length says nothing
            # about its token cost: a 1 MB photo is ~1.4M characters but bills
            # like a few hundred tokens. One flat stand-in for every payload --
            # image, PDF, or encoded data -- keeps the rule uniform.
            return _OPAQUE_PAYLOAD_CHARS
        return len(value)
    if isinstance(value, (bytes, bytearray)):
        # Raw bytes in arguments are by definition a binary payload.
        return _OPAQUE_PAYLOAD_CHARS
    if isinstance(value, Mapping):
        return sum(_text_length(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_text_length(item) for item in value)
    return 0  # numbers, booleans, None: no text, and never an exception


def estimate_tokens(arguments: Mapping[str, object]) -> int:
    return _text_length(arguments) // _CHARS_PER_TOKEN
```

Bucket mapping (from the original PR11 sketch, to be recalibrated against real
traffic): small < 2k estimated tokens, medium 2k–16k, large 16k–64k, xlarge > 64k.

### How it works, in plain words

Walk the arguments and find every string. Count each string's length — except
strings that are a file spelled as text (base64), which count as a flat ~2,500
tokens regardless of their real length. Add up, divide by four, pick the bucket.

The payload rule is the only clever part, and it exists for one reason: JSON
cannot carry raw bytes, so an attached file travels as its bytes re-spelled in
base64. A 1 MB photo becomes ~1.4 million characters that bill as ~1,300 tokens.
Unhandled, one attached photo makes a small request look 300× larger than it is
and lands every image-carrying request in `xlarge`, poisoning that bucket's
statistics. WeaveRouter shipped exactly this bug — its comments record base64
payloads causing "false context-window evictions" — and fixed it by permanently
distorting its divisor for all text instead of detecting the payloads.

The flat stand-in (~2,500 tokens) is deliberately a mid-range guess: above a
typical image (~1,300), absorbing a small document, below a huge one. It is not a
billing prediction; it is a uniform label that keeps every payload-carrying
request of the same shape in the same bucket.

Base64 detection is sound because it is anchored on a structural fact, not a
convention: **in JSON APIs, "inline binary" and "base64" are the same thing.**
JSON has no binary type, so any inline file must be text-encoded, and base64 is
the standard the OpenAI, Anthropic (and every other JSON) protocol uses. The
alternatives to inline are references (URLs, file ids — short strings) or
client-side text extraction (which is honest text and is counted as such). There
is no common second encoding to slip past the check.

### Cost

Python stores string lengths, so `len()` is O(1); the marker check reads ≤128
characters and the alphabet sample reads 64. **Nothing ever reads a whole string**
— measuring a 10 MB blob costs the same as measuring "hi". Total cost scales with
the number of values in the arguments, not characters: low microseconds for a
large conversation, against a provider call taking hundreds of milliseconds.
Measured on this machine: prefix/marker checks tens of nanoseconds, alphabet
sample ~126–453 ns, versus ~500,000 ns for the whole-string scans this design
rejects.

## Tradeoffs

### Strengths

- **Zero dependencies.** The lightweight-import test stays green; nothing is
  downloaded, bundled, or lazily imported.
- **No schema knowledge.** The walk never reads a key name. It measures what
  strings *say about themselves* (a `;base64,` wire marker, the base64 alphabet),
  which relies on stable web standards, not on any provider's message shape.
  Adding a protocol requires no estimator change. The
  "`arguments` is never interpreted" principle stays almost intact: the router
  measures arguments, it still does not read their meaning.
- **Cannot bias provider comparison.** One estimate per call, stamped on every
  attempt. See the design principle above — this is the load-bearing property.
- **Fails soft.** The walk is total (unknown value types count as 0, never
  raise). Residual errors are bounded under-counts on rare shapes, not the
  unbounded over-count the design eliminates.
- **Explainable.** Every rule is categorical and states in one sentence. "This
  string carries the `;base64,` marker" is a fact, not a statistical guess.

### Weaknesses

| Case | What happens | Error size |
|---|---|---|
| Large inline PDF | flat stand-in ~2.5k, bills ~1.5–3k tokens *per page* | **the one breach of the factor-of-ten tolerance** (~100× under for a long document). Mitigable later: base64 preserves the file's magic bytes (`JVBERi` = PDF, `iVBORw` = PNG, `/9j/` = JPEG), so PDFs could be counted by length with a larger divisor — still value-level. Out of scope until document traffic exists |
| Large *referenced* file (`file_id`, URL) | ~30 chars counted, can pull 100k+ tokens into context | unbounded under-count — but **uncatchable by any pre-dispatch estimator**, including schema-aware ones (LiteLLM cannot price a file_id without asking the provider). This is the floor for the whole problem class, not a weakness of this design |
| Many referenced images | 40 URL-form images under-count by ~50k tokens | same category: invisible without fetching |
| False positive at the gate (e.g. a 5,001-char hex dump) | counted 2.5k instead of ~1.25k | ~2× over at the boundary; the flat stand-in's price for simplicity |
| Long hex dumps / DNA sequences | genuinely text, but capped by the alphabet trigger | under-count; rare, workload-dependent; the estimator seam (below) is the escape hatch |
| CJK-heavy text | ÷4 under-counts (CJK runs ~1 token/char) | ~4× under; deliberate omission, revisit only if such traffic appears |
| base64url payloads (`-_` alphabet variant) | would evade the alphabet trigger and count raw | **the one hypothetical over-count.** No supported protocol uses base64url for media today; check this when adding protocols |

Constants must be pinned by tests, not comments: two surveyed projects (WeaveRouter,
OmniRoute) have divisors that silently drifted from their documentation. A test
asserting a known string's bucket makes drift a test failure instead of a quiet
reshaping of the buckets.

## Is this a viable PR for this router?

The estimator and the bucketed routing have different answers.

**The estimator fits this project's principles exactly.** It adds no dependency,
no schema knowledge, no configuration burden, and its behavior can be explained in
four sentences. It should be injectable via a constructor parameter (the testing
rules require real seams), defaulting to the heuristic — anyone with unusual
traffic swaps in their own callable, including a tokenizer-backed one, without the
core importing anything.

**Bucket-scoped routing carries a real cost: evidence dilution.** Scoring already
softens thin evidence with an optimistic prior. Splitting one evidence pool into
four means each bucket fills 4× slower; at modest traffic, most buckets sit at the
prior most of the time, every provider scores alike, and ranking quietly degrades
to round-robin. Bucketing can make routing *worse* before it makes it better.

### Worth it when

- Traffic mixes genuinely different request sizes (chat turns alongside
  document-stuffed or agentic calls), so buckets separate something real.
- Volume is high enough that at least the busy buckets accumulate evidence within
  the scoring lookback window.
- Observed provider behavior actually differs by size — which is currently an
  assumption, not a measurement.

### Not worth it when

- Request sizes are uniform: everything lands in one bucket, and the feature adds
  a column and a code path while changing no routing decision.
- Traffic is too low to fill even pooled statistics well: splitting evidence that
  is already thin buys noise.

### Recommended path: record first, route later

Phase 1 — producer only. Compute the estimate per call and store it in PR29's
existing nullable event field/column, changing no routing behavior. Cost is
near zero, risk is zero, and it is trivially reversible.

Phase 2 — decided by the data phase 1 produces. After real traffic, check whether
per-provider latency actually varies by bucket and where natural boundaries fall.
If the effect is real, scope `aggregate_stats` and the score policy by bucket
(with a pooled fallback when a bucket has no evidence — never let an empty bucket
collapse scoring to priors). If the effect is absent, stop at phase 1 and keep
the column as diagnostics. Consider starting with two buckets rather than four:
the observation motivating the feature is "big behaves differently from small,"
not "there are four regimes," and two buckets fill twice as fast.

Phase 1 is the cheap half of the work that all seven surveyed projects skipped —
OmniRoute had the answer sitting on disk and never ran the query. Phase 2 is only
worth building once phase 1 shows there is something to route on.

## Important note: multiple CallVariants and future protocols

A call to the router may supply **several CallVariants** — the same logical
request spelled once per protocol (e.g. an `openai_chat` variant and an
`openai_responses` variant), with the router dispatching whichever matches the
chosen provider. This interacts with the design principle above and must be
handled deliberately:

- **The bucket is a property of the call, not the attempt.** Estimate every
  supplied variant's arguments and take the **maximum**; stamp that single bucket
  on every attempt the call produces, including fallbacks to other providers and
  mid-stream restarts. Estimating per-attempt instead would let two providers
  record the same call in different buckets whenever the variants' estimates
  straddle a boundary — quietly breaking the guarantee that estimator error can
  never bias a provider comparison.
- Variants of one call carry the same content in different shapes, so their
  estimates are normally near-identical. The maximum is chosen because it is
  deterministic and order-independent (the first-variant rule would silently
  depend on the caller's list ordering), not because large differences are
  expected.
- **Future protocols need no estimator work.** The walk never reads key names,
  and in JSON APIs base64 is the only spelling for inline binary, so the same two
  triggers apply unchanged to any new variant's arguments. The single check to
  perform when adding a protocol: confirm its SDK does not ship payloads in
  base64url (`-_` alphabet), which would evade the bare-base64 trigger and count
  raw — the one failure mode that recreates the over-count this design exists to
  prevent.
