# Sessions API

## `Session`

```python
@dataclass
class Session:
    info: SessionInfo
    messages: list[Message]
    checkpoints: list[Checkpoint]

    @classmethod
    def load(cls, store: SessionStore, session_id: str) -> "Session": ...
    def save(self, store: SessionStore) -> None: ...
    def add_checkpoint(self, label: str) -> Checkpoint: ...
    def truncate_to(self, checkpoint: str | Checkpoint) -> None: ...
```

A `Session` holds an in-memory copy of a transcript plus its checkpoints.
Use it when you want to inspect a session outside of an active agent
loop.

## `SessionInfo`

```python
@dataclass
class SessionInfo:
    session_id: str
    created_at: datetime
    updated_at: datetime
    model: str | None
    title: str | None
    tags: list[str]
    metadata: dict
```

## `Checkpoint`

```python
@dataclass
class Checkpoint:
    label: str            # human-readable
    position: int         # index into messages
    created_at: datetime
    auto: bool            # True if runtime-generated
```

`make_checkpoints(messages)` builds the default checkpoint list from a
message array. The runtime adds one after each `assistant`/`result`
turn, plus one after compaction.

## `fork_session`

```python
def fork_session(
    source: Session,
    checkpoint: str | Checkpoint,
    new_session_id: str,
) -> Session: ...
```

Branch off from a checkpoint. The new session shares history up to
`checkpoint` and starts a fresh JSONL file.

## `resume_session`

```python
def resume_session(
    session_id: str,
    checkpoint: str | Checkpoint,
    store: SessionStore,
) -> Session: ...
```

Loads a session, truncates to the checkpoint, and returns it. The
discarded suffix is *not* permanently lost — it's preserved in a sister
file `{session_id}.discarded.jsonl` for forensic inspection.

## Stores

### `SessionStore` protocol

```python
class SessionStore(Protocol):
    async def load(self, session_id: str) -> Session: ...
    async def save(self, session: Session) -> None: ...
    async def list(self) -> list[SessionInfo]: ...
    async def delete(self, session_id: str) -> None: ...
    async def list_checkpoints(self, session_id: str) -> list[Checkpoint]: ...
```

### `InMemorySessionStore`

Non-persistent. Useful for tests.

```python
from mantis_agent import InMemorySessionStore
store = InMemorySessionStore()
```

### `SqliteSessionStore`

Single-file SQLite.

```python
from mantis_agent import SqliteSessionStore
store = SqliteSessionStore("~/.mantis-agent/sessions.db")
```

Schema:

- `sessions(session_id, info_json, updated_at)`
- `messages(session_id, ord, message_json)`
- `checkpoints(session_id, label, position, created_at, auto)`

`SessionInfo.metadata` is stored as JSON inside `info_json` — query via
`json_extract()` in SQLite.

## `SessionNotFoundError`

```python
from mantis_agent import SessionNotFoundError

try:
    s = await store.load("does-not-exist")
except SessionNotFoundError as e:
    print(e.session_id)
```

## `JsonlTranscript`

The on-disk format used by the file-backed default store.

```python
from mantis_agent import (
    JsonlTranscript,
    read_transcript,
    iter_transcripts,
)

# Single file
transcript = read_transcript("~/.mantis-agent/sessions/abc.jsonl")
print(len(transcript.messages))

# Walk all transcripts — no argument; the root comes from $MANTIS_AGENT_HOME.
for session_id, path in iter_transcripts():
    print(session_id, path)
```

Each line of a `.jsonl` file is one `Message` serialised via `msgspec`.
The format is stable across minor versions.
