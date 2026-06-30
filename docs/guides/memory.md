# Memory

Memory is durable, file-based context that survives across sessions. The
agent reads a memory index at session start and loads individual entries
on demand.

## Layout

```
~/.mantis-agent/memory/
├── INDEX.md           one-line entry per memory file (always loaded)
├── user_role.md       — example: "user is a fintech BE engineer"
├── feedback_no_pii.md — example: "never log card numbers"
└── stripe/
    ├── INDEX.md       — sub-index for accumulated topic
    └── retries.md
```

Override the root with `MANTIS_AGENT_HOME=/path`.

## Entry format

Each memory file is markdown with frontmatter:

```markdown
---
name: User role
description: User is a fintech BE engineer who owns payments infrastructure
type: user
---

The user is a backend engineer at a fintech company...
```

Types:

- `user` — who the user is, role, preferences.
- `feedback` — corrections and validations from the user.
- `project` — ongoing work, goals, decisions.
- `reference` — pointers to raw materials elsewhere.

## Writing memory

```python
from mantis_agent import MemoryEntry, save_memory_entry, update_memory_index

entry = MemoryEntry(
    slug="user_role",
    name="User role",
    description="User is a fintech BE engineer who owns payments infra",
    type="user",
    body="The user is a backend engineer at a fintech company...",
)

save_memory_entry(entry)
update_memory_index([entry, ...])   # rewrites INDEX.md
```

## Reading memory

```python
from mantis_agent import (
    list_memory_entries,
    load_memory_entry,
    load_memory_index,
)

# Headers only (cheap)
for slug, name, desc in load_memory_index():
    print(slug, name, desc)

# Full entries
for entry in list_memory_entries():
    print(entry.name, entry.body[:80])

# Single entry by slug
entry = load_memory_entry("user_role")
```

## Loading into the agent

By default, the agent runtime loads `INDEX.md` at session start and
makes it available to the model as an `isMeta` system message. The
model can then load specific entries on demand via the built-in
`load_memory` tool.

To preload specific entries instead:

```python
options = {
    "memory_entries": ["user_role", "feedback_no_pii"],
}
```

To disable memory loading entirely:

```python
options = {"memory_entries": False}
```

## When to use memory vs. system prompt

- **System prompt** — every-session truths. The agent's role, voice,
  hard rules.
- **Memory** — user/project-specific context that accumulates over
  time. Loaded on demand so it doesn't pay for itself every session.

The INDEX.md you maintain at the root is the contents page; it always
loads. Individual entries cost zero context until the model decides to
read them.

## Promotion: flat → tree

When a topic accumulates 3+ entries at the root, promote it to a
subdirectory with its own `INDEX.md`. The `update_memory_index` helper
handles the bookkeeping if you pass a topic prefix:

```python
update_memory_index(
    entries=[stripe_entry_1, stripe_entry_2, stripe_entry_3],
    topic="stripe",
)
```

This creates `~/.mantis-agent/memory/stripe/INDEX.md` and moves the
entries. Root `INDEX.md` gets a single pointer line to the subdir.

## Best practices

- Lead each entry with the rule or fact in one sentence.
- Add **Why:** so future-you can judge edge cases.
- Add **How to apply:** so future-you knows when it kicks in.
- Verify before recommending: a memory naming a function is a claim that
  the function exists. Before acting on it, `grep` to confirm.
