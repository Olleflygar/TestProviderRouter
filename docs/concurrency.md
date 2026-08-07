# Concurrency & lifecycle

This matrix is the whole promise. Anything not listed as supported is not
supported.

## Supported

- **One `ProviderRouter` shared by multiple threads in one process.** Health,
  policy ordering, adapter reuse, and metrics bookkeeping serialize behind one
  router lock.
- **Provider network calls running concurrently.** No lock is held across an
  adapter call or while you iterate a stream — only short bookkeeping at the
  edges.
- **DuckDB / SQLite metrics from multiple threads in one process.** Each local
  store has its own lock; router lock is always taken first.
- **Postgres metrics from multiple threads in one process.** The store uses a
  connection pool and holds no store lock of its own. A slow remote DB can
  still delay other threads' routing because the router holds its lock across
  storage calls — connection timeouts bound that.
- **Several processes sharing one SQLite metrics file.**
- **`close()` and `with ProviderRouter(...) as router:`.** Close is idempotent
  and terminal. The router closes only a store *it* created (the default
  DuckDB). Caller-provided `metrics_store` is yours to close.
- **Custom policies without their own locks**, as long as `order()` never calls
  back into the router (that deadlocks).

## Not supported

- **Multiple processes writing one DuckDB file** — use SQLite or Postgres.
- **Sharing health across processes** — health is in-memory per router.
- **One stream consumed by more than one thread** — single-consumer only.
- **`invoke()` after `close()`** — raises `RouterClosedError`.
  `health_report()` / `reset_health()` still work on in-memory state.
- **Buffered/batched writes and native async** — not shipped yet; writes stay
  eager and synchronous.

## Shutdown

Finish in-flight calls and fully drain or close streams **before** closing the
router. Close does not interrupt work, but metrics bookkeeping that arrives
after close is dropped (so a late write cannot reopen the DB file). A stream
that finishes after close loses its metrics row, not its content; health
updates still land in memory.
