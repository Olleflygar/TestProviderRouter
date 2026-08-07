# Streaming

Declare `CallType.STREAMING`, pass the provider's native `stream=True`, and
iterate. Chunks are the SDK's own objects, in order, unbuffered.

```python
stream = router.invoke(
    [
        CallVariant(
            protocol=ApiProtocol.OPENAI_CHAT,
            operation="chat.completions.create",
            call_type=CallType.STREAMING,
            arguments={
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )
    ]
)

for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

Responses streams yield typed events (`response.output_text.delta`, etc.) the
same way — see [Getting started](./getting-started.md).

`call_type` is router metadata. Keep it consistent with native `stream=True`;
the router does not insert or reconcile that flag for you.

## Mid-stream failure

If a normalized stream dies mid-generation (dropped connection, timeout, silent
truncation), the router can fall back to the next provider in the already
computed order instead of dumping a raw SDK exception into your loop.

Default policy is **restart** on the next provider. To fail immediately:

```python
from llm_provider_router import StreamFailurePolicy

router = ProviderRouter(
    ...,
    stream_failure_policy=StreamFailurePolicy.RAISE,
)
```

A stream that opens but yields zero chunks counts as a failed attempt.

Same-provider retry does **not** apply after a stream has opened — only
pre-open failures can retry the same provider. Mid-stream recovery walks the
ordered tail.

## Restarts regenerate from scratch

Two generations cannot be spliced. On restart, discard everything you already
buffered from the dead provider:

```python
from llm_provider_router import StreamRestart

def on_restart(restart: StreamRestart) -> None:
    print(
        f"discard {restart.chunks_yielded} chunk(s) from "
        f"{restart.failed_provider}; "
        f"{restart.next_provider} is regenerating"
    )
    buffer.clear()

router = ProviderRouter(..., on_restart=on_restart)
```

If you skip the callback and chunks had already been yielded, you get a
warning instead. Restart count is on the stream as `.restarts`.

Bad requests and bad `operation`/`arguments` still stop globally — including
across protocol variants.

## Stopping early

Close the stream when you break out early (or use a context manager):

```python
with router.invoke([...]) as stream:
    for chunk in stream:
        if done_enough(chunk):
            break
```

Close never triggers a restart. If the provider had already marked the response
finished, the attempt is recorded as success; otherwise nothing is recorded for
that attempt. A bare `break` without `close()` runs no router cleanup.

## Truncation detection

Chat success needs a `finish_reason`. Responses success needs
`response.completed` or the served terminal `response.incomplete`.
`response.failed` / `error` become provider failures.

If the chunk shape is unrecognized, the router will not invent a truncation
verdict — the stream counts as completed and you get one warning that detection
is unavailable for that shape.

## Custom adapters

Mid-stream fallback is opt-in. Returning a raw SDK stream passes through
untouched. To participate, return a `NormalizedStream` that yields SDK chunks
unchanged and reports `completed` / `usage`.
