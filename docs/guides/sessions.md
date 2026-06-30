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
options = {"persist": "./my-sessions/"}
```

Or disable persistence entirely:

```python
options = {"persist": False}
```

## Session IDs

You can supply your own:

```python
options = {"session_id": "user-42/thread-abc"}
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

forked = fork_session(
    source=session,
    checkpoint="before-experiment",
    new_session_id="user-42/thread-abc/alt-1",
)
```

The fork shares history up to the checkpoint, then diverges. Each branch
journals to its own JSONL file. The original is untouched.

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
from mantis_agent import iter_transcripts

for path, transcript in iter_transcripts("~/.mantis-agent/sessions"):
    print(path, len(transcript.messages))
```

`JsonlTranscript` is the on-disk format; `read_transcript(path)` returns
one.

## Auto-compaction

When a session approaches the model's context window, the runtime emits
a compaction event: it summarises older turns into a single condensed
message and continues. The original transcript is preserved on disk; only
the in-memory message list is replaced.

Tune the threshold:

```python
options = {
    "compact_threshold": 0.85,  # at 85% of context window, compact
}
```

Set to `1.0` to disable.
