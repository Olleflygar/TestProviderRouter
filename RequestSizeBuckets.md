# Request-size routing buckets — scrapped design

**Status: SCRAPPED. PR11 is not part of the active roadmap and this design must
not be implemented.**

The idea was rejected because the router cannot estimate request size reliably
without inspecting and interpreting opaque `CallVariant.arguments`. Doing so
would add latency, CPU and memory work, provider-specific semantic dependencies,
and behavior based on ambiguous guesses. That conflicts with the router's main
goals: native pass-through, transparency, and a lightweight core.

The intended benefit is already available more explicitly through
`metrics_scope`. Callers can separate short chats, document analysis, vision,
or other materially different workloads without the router examining their
content. The existing nullable `request_size_bucket` field therefore remains
unused and router-produced events leave it `NULL`.

The remainder of this note summarizes what PR11 was intended to implement and
the design problems that caused it to be scrapped. It is historical rationale,
not an implementation plan.

## What was proposed

Provider latency can vary with request size. PR11 proposed assigning every call
a broad size bucket, storing that bucket on each provider attempt, and comparing
providers only against history from similarly sized calls.

The bucket would have been calculated once before provider selection and reused
across fallbacks. Calls containing several protocol-specific `CallVariant`
values would also have needed one shared bucket so the same logical request did
not enter different provider histories.

## Approaches considered

| Approach | Strength | Why it was rejected |
| --- | --- | --- |
| Exact tokenization | Accurate for known text and models | CPU- and memory-heavy; requires tokenizer and model dependencies; cannot reliably measure referenced or multimodal content |
| Character counting | Simple and dependency-free | Character length is not semantic size and varies greatly across languages, code, JSON, and provider formats |
| Schema-aware estimation | Can recognize known message and media fields | Couples the router to provider schemas and requires ongoing provider-specific interpretation |
| User-supplied estimator | Applications can define their own semantics | Adds another classification API when `metrics_scope` already provides an explicit partition |

## The central ambiguity

Base64 data illustrates why a generic estimate is misleading. A one-megabyte
image becomes roughly 1.4 million characters, and several attachments can
produce several million characters. Counting those characters makes a modest
image request look like an enormous text prompt.

Avoiding that error requires guessing that the string is encoded data and then
guessing whether it represents an image, PDF, audio file, archive, or something
else. Replacing every detected payload with one flat estimate is still wrong:
one image and a hundred-page PDF do not have comparable provider cost or
latency. Detection can also misclassify long hashes, source data, or valid text.

Other unresolved cases include:

- A short URL or `file_id` may reference a very large document that the router
  cannot inspect without fetching provider-owned content.
- Tool schemas, structured output, source code, JSON, and different natural
  languages have different token densities for the same character count.
- Images, documents, audio, and video are processed differently across models
  and providers.
- Prompt caching can make a large repeated request faster than a smaller
  uncached request.
- Equivalent protocol variants may have different shapes and lengths, forcing
  another arbitrary rule for their shared bucket.

These are semantic dependencies rather than threshold-tuning problems. Adding
exceptions for them would make the router increasingly provider-aware and make
routing decisions harder to explain.

## Final decision

- PR11 and automatic request-size inference are scrapped.
- `request_size_bucket` remains nullable and unused for schema compatibility.
- No bucket estimation, filtering, aggregation, or bucket-aware scoring should
  be added under the current roadmap.
- Applications should use separate `metrics_scope` values to partition routing
  history by workload size or type.
- The idea should be reconsidered only if a future roadmap provides an explicit,
  caller-supplied size signal that requires no inspection of native arguments.
