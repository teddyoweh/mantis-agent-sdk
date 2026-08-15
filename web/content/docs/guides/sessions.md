# Sessions and resume

Every `query()` call (and every `ClaudeSDKClient` lifetime) is a
**session**. Sessions are journaled to disk as JSONL transcripts; you can
fork them, resume them from arbitrary checkpoints, and prune them.

## Where transcripts live

```
~/.mantis-agent/sessions/{session_id}.jsonl
```

Each line is one `Message`. The format is stable across versions — older
transcripts will keep deserialising as the SDK evolves.

Override the location:

```python
options = MantisAgentOptions(
    persist="./my-sessions/",
)
```

Or disable persistence entirely:

```python
options = MantisAgentOptions(
    persist=False,
)
```

## Session IDs

You can supply your own:

```python
options = MantisAgentOptions(
    session_id="user-42/thread-abc",
)
```

Otherwise the SDK generates a ULID-shaped id (sortable, no central
authority needed).

## The `Session` class

```python
from mantis_agent import Session, SqliteSessionStore

store = SqliteSessionStore("~/.mantis-agent/sessions.db")
session = Session.load(store, "user-42/thread-abc")

print(session.info.created_at)
print(len(session.messages))
print(session.checkpoints)
```

`Session.load` reads the JSONL (or SQLite row, depending on store) and
returns an in-memory copy. Pass it to `Agent(session=...)` or
`ClaudeSDKClient(session=...)` to continue the thread.

## Checkpoints

A **checkpoint** is a labelled position in the transcript. The runtime
auto-creates checkpoints at sensible points (end of each turn, after
compaction). You can also add them manually:

```python
session.add_checkpoint("before-experiment")
```

Inspect them:

```python
from mantis_agent import make_checkpoints, Checkpoint

cps: list[Checkpoint] = make_checkpoints(session.messages)
for cp in cps:
    print(cp.label, cp.position, cp.created_at)
```

## Fork

Branch off a session at any checkpoint:

```python
from mantis_agent import fork_session

# fork_session is async and takes the STORE plus the source id — positionally:
#   fork_session(store, src_id, new_id=None, *, checkpoint=None)
new_id = await fork_session(store, "user-42/thread-abc", "user-42/thread-abc/alt-1")

# A checkpoint is an index (or a Checkpoint handle), not a label: `2` keeps
# messages [0:2]. Omit it to copy the whole history.
truncated = await fork_session(store, "user-42/thread-abc", checkpoint=2)
```

The fork shares history up to the checkpoint, then diverges. Each branch
journals under its own id, and the metadata records `forked_from` (plus
`forked_at_index` for a truncated fork). The original is untouched.

Use forks to:

- A/B different system prompts on the same context.
- Speculatively explore a path and roll back.
- Hand a partial conversation to a sub-agent.

## Resume

Restart a session from a specific checkpoint:

```python
from mantis_agent import resume_session

resumed = resume_session(
    session_id="user-42/thread-abc",
    checkpoint="before-experiment",
    store=store,
)

# Continue the conversation
async for msg in resumed.query("now what?"):
    ...
```

Resuming discards everything after the checkpoint. Use it when a turn went
sideways and you want to retry from a known-good state.

## Stores

Two stores ship by default:

- `InMemorySessionStore` — non-persistent. Useful in tests.
- `SqliteSessionStore` — single-file SQLite at the path you give.

The `SessionStore` protocol is small (5 methods); implement your own if
you want Redis, S3, Postgres, etc.

```python
from mantis_agent import SessionStore

class MyStore(SessionStore):
    async def load(self, session_id): ...
    async def save(self, session_id, messages): ...
    async def list(self): ...
    async def delete(self, session_id): ...
    async def list_checkpoints(self, session_id): ...
```

## Iterate over all transcripts

```python
from mantis_agent import iter_transcripts, read_transcript

# Yields (session_id, path) for every persisted transcript. It takes no
# arguments — the location comes from $MANTIS_AGENT_HOME.
for session_id, path in iter_transcripts():
    # read_transcript takes the SESSION ID, not the path, and yields one
    # parsed dict per line (so it streams rather than loading the file).
    lines = list(read_transcript(session_id))
    print(session_id, len(lines))
```

`JsonlTranscript` is the on-disk format; `read_transcript(path)` reads one back.

## Auto-compaction

When a session approaches the model's context window, the runtime emits
a compaction event: it summarises older turns into a single condensed
message and continues. The original transcript is preserved on disk; only
the in-memory message list is replaced.

Tune the threshold:

There is no `compact_threshold` option. The threshold lives on the compactor,
which you hand to `Agent` directly:

```python
from mantis_agent import Agent
from mantis_agent.compact import SimpleCompactor

agent = Agent(
    model="qwen2.5:7b",
    backend="http://localhost:11434",
    # 0.85 of the context window is the default; keep_recent_turns controls how
    # much verbatim tail survives a compaction.
    compactor=SimpleCompactor(lambda *a, **k: "", threshold=0.9, keep_recent_turns=8),
)

no_compaction = Agent(
    model="qwen2.5:7b",
    backend="http://localhost:11434",
    auto_compact=False,
)
```

Passing `compactor=` builds the summarizer yourself; leaving `auto_compact=True`
(the default) wires a `SimpleCompactor` to the agent's own model. Compaction
tuning is an `Agent` concern — neither knob is reachable through `query()`
options.
