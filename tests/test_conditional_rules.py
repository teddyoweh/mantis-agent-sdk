"""Path-scoped conditional rules — .mantis/rules/*.md with globs inject only when
a matching file is active."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio

from mantis_agent.agent import Agent
from mantis_agent.capabilities import HOSTED_PROFILES
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.rules import (
    active_files_from_messages,
    discover_conditional_rules,
    parse_rule_frontmatter,
    rule_file_has_globs,
    select_matching_rules,
)
from mantis_agent.types import (
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
    Usage,
)


# --- pure functions --------------------------------------------------------

def test_parse_frontmatter_forms() -> None:
    assert parse_rule_frontmatter('---\nglobs: ["**/*.sql","db/**"]\n---\nBody')[0] == ["**/*.sql", "db/**"]
    assert parse_rule_frontmatter("---\nglobs: **/*.sql\n---\nx")[0] == ["**/*.sql"]
    assert parse_rule_frontmatter('---\nglobs:\n  - "*.sql"\n  - "*.psql"\n---\nx')[0] == ["*.sql", "*.psql"]
    assert parse_rule_frontmatter("no frontmatter")[0] == []
    assert parse_rule_frontmatter("---\nglobs: **/*.sql\n---\nBody text")[1] == "Body text"


def test_active_files_from_messages() -> None:
    msgs = [
        UserMessage(content="please fix @db/schema.sql"),
        AssistantMessage(content=[ToolUseBlock(id="c", name="read_file", input={"path": "src/app.py"})]),
    ]
    assert active_files_from_messages(msgs) == {"db/schema.sql", "src/app.py"}


def test_select_matching_rules() -> None:
    rules = [
        (Path("sql.md"), ["**/*.sql"], "SQL body"),
        (Path("go.md"), ["**/*.go"], "Go body"),
    ]
    sel = select_matching_rules(rules, {"db/schema.sql"})
    assert [b for _p, b in sel] == ["SQL body"]


def _mkrules(tmp: Path) -> None:
    d = tmp / ".mantis" / "rules"
    d.mkdir(parents=True)
    (d / "sql.md").write_text('---\nglobs: ["**/*.sql"]\n---\nUse snake_case in SQL.')
    (d / "always.md").write_text("Always be concise.")   # no globs → unconditional


def test_discover_only_globbed(tmp_path: Path) -> None:
    _mkrules(tmp_path)
    found = discover_conditional_rules(tmp_path)
    assert len(found) == 1                       # only the globbed one
    assert found[0][1] == ["**/*.sql"]
    assert rule_file_has_globs(tmp_path / ".mantis/rules/sql.md")
    assert not rule_file_has_globs(tmp_path / ".mantis/rules/always.md")


# --- end-to-end injection --------------------------------------------------

class _Text:
    name = "mock"

    def __init__(self) -> None:
        self.backend_capability = HOSTED_PROFILES["mock"]

    async def stream(self, *, model: str, messages: Any, **_kw: Any):
        yield MessageStart(message_id="m", model="mock")
        yield ContentBlockStart(index=0, block=TextBlock(text=""))
        yield ContentBlockDelta(index=0, delta=TextDelta(text="ok"))
        yield ContentBlockStop(index=0)
        yield MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))
        yield MessageStop()


def _run(user_text: str, tmp_path: Path, monkeypatch) -> list:
    _mkrules(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MANTIS_AGENT_NO_CONTEXT", raising=False)

    async def go():
        agent = Agent(model="mock", provider=_Text(), auto_compact=False,
                      include_recall=False, include_env=False, include_memory=False)
        msgs: list = [UserMessage(content=user_text)]
        async for _ in agent.run_iter(msgs):
            pass
        return msgs
    return anyio.run(go)


def _has_rule(msgs: list) -> bool:
    return any(getattr(m, "isMeta", False) and "snake_case" in str(m.content) for m in msgs)


def test_rule_injected_when_file_active(tmp_path: Path, monkeypatch) -> None:
    msgs = _run("update @db/schema.sql to add an index", tmp_path, monkeypatch)
    assert _has_rule(msgs)               # SQL rule rode the SQL mention


def test_rule_not_injected_otherwise(tmp_path: Path, monkeypatch) -> None:
    msgs = _run("refactor the python module app.py", tmp_path, monkeypatch)
    assert not _has_rule(msgs)           # no SQL file active → no SQL rule
