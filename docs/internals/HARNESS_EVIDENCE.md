# Evidence-aware harness

The harness now separates UI previews from worker results, archives compacted evidence,
and maintains optional structured task state independently of conversation summaries.

## Terminal behavior

Full-model terminal sessions enable:

- Project-scoped artifacts under `projects/<project-hash>/artifacts/` in the Mantis home.
- Transcript-scoped task state under `projects/<project-hash>/task-state/<session-id>.json`.
- `read_artifact` for bounded retrieval of archived evidence by content-addressed ID.
- `task_state` for objectives, acceptance checks, hypotheses, and evidence references.

Small-model mode does not add these tools. Normal permissions still apply. A new or
branched session gets independent task state; resume reloads its existing state.
`/clear` starts a fresh transcript. Corrupt task state is reported rather than reset.
The artifact root stays project-scoped so references remain available after resume.

## SDK opt-in

```python
from pathlib import Path
from mantis_agent import Agent
from mantis_agent.artifacts import ArtifactStore
from mantis_agent.task_state import TaskState

agent = Agent(
    model="your-model",
    tools=your_tools,
    artifact_store=ArtifactStore(Path(".local-agent/artifacts")),
    task_state=TaskState(Path(".local-agent/task.json")),
)
```

These options are off by default for SDK callers. The default compactor uses the
supplied artifact store. A custom compactor must be configured separately.
`read_artifact` and `task_state` are reserved names when their stores are supplied.
Registering these bound tools does not add them to the caller's registry.

## Context and handoffs

Workflow `result` preserves the complete latest assistant text, independently of the
200-character activity preview. Intermediate commentary is not concatenated into the
final answer. Serialization and replay preserve that full result.

With an artifact store, microcompaction/emergency clearing archives heavy payloads
before replacing them. Full compaction archives the summarized messages and places a
retrieval reference alongside the summary. Archive-write failures retain the original
context. Structured thinking blocks are excluded from retrievable JSON archives.

Artifact IDs are SHA-256 hashes; retrieval accepts no filesystem paths. Character-based
paging caps output at 16,000 characters, and literal search scans at most 1,000,000
characters per request. Follow `next_offset` until `eof`. Files are local and private;
tool outputs can contain sensitive data, so treat the store like session transcripts.
There is no automatic garbage collection: deleting artifacts breaks old references.

Memory recall now searches body content as well as weighted metadata, including old
notes outside the former newest-200 candidate limit. Injection is bounded and uses
query-relevant excerpts. Evicted memories can be recalled again. Current task state
and the selected recall block are reassembled at provider boundaries, including
compaction retry paths.

## Task evidence contract

Use `task_state` for complex work rather than adding overhead to every small task:

1. Set the objective and add acceptance checks.
2. Run tools or experiments.
3. On a later model turn, use observed call IDs to record check results or close hypotheses.
4. Inspect unresolved checks before claiming completion.

Only the host records execution evidence. Model-supplied IDs must refer to retained
observations. Error results cannot support a passed check. A tool returning normally
is **not proof of semantic success**: passed/supported statuses remain model assertions
backed by execution evidence. Evaluate the output against the acceptance criterion.
State progress counts unique evidenced criteria/hypotheses instead of repeatedly
rewarding rewrites. Existing hard turn/cost limits still apply.

Task state uses bounded, versioned, atomically replaced JSON. It assumes a single writer
per path. It does not automatically replay commands or reconcile interrupted external
side effects. Persistence errors are surfaced, not silently ignored.

## Validation and remaining work

Regression coverage includes long worker reports and trailing verdicts, serialization
and replay, repeated compaction, archive failure/path confinement, older/body-only
memory retrieval, bounded excerpts, task-state persistence, actual execution provenance,
registry isolation, and terminal session rebinding.

This is the evidence/context foundation, not the entire proposed architecture. Still
future work: a unified code/document/artifact retrieval index, revision-based evidence
invalidation, global compute reservation across workers, automatic experiment selection,
isolated competing implementations, external-side-effect reconciliation, and a scored
end-to-end hard-task benchmark suite. Unit/regression success does not establish a
measured multiplier in real-world task-solving capability.
