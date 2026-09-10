"""Bounded, durable task facts, not a scratchpad or an automatic verifier.

Only the host may supply execution evidence through ``observe``. Persistence is
local trusted state, not a cryptographic attestation against file tampering.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .tools import Tool
from .types import ToolResultBlock, ToolUseBlock

__all__ = ["TaskState", "make_task_state_tool"]

_MAX_RECORDS = 64
_MAX_EVIDENCE = 128
_MAX_FILE = 5_000_000


def _text(value: Any, limit: int, *, empty: bool = False) -> None:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(
            f"Expected {'possibly empty ' if empty else ''}text of at most {limit} characters"
        )


def _keys(value: Any, keys: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Invalid task-state fields")


def _json(value: Any) -> str:
    import msgspec

    def public_value(item: Any) -> Any:
        if isinstance(item, msgspec.Struct):
            item = msgspec.to_builtins(item)
        if isinstance(item, dict):
            if item.get("type") in ("thinking", "redacted_thinking"):
                return None
            return {key: public_value(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [public_value(val) for val in item]
        return item

    return json.dumps(public_value(value), ensure_ascii=True, sort_keys=True, allow_nan=False)


class TaskState:
    """Load an optional version-1 JSON file; invalid state raises ValueError.

    Mutations persist atomically before becoming visible in memory. Record limits
    reject new criteria; unreferenced oldest evidence is evicted. No tools are
    executed or re-run by this class. One writer per path is required.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self._data: dict[str, Any] = {
            "version": 1,
            "objective": "",
            "checks": {},
            "hypotheses": {},
            "evidence": {},
        }
        if self.path is not None and self.path.exists():
            try:
                with self.path.open("rb") as f:
                    raw = f.read(_MAX_FILE + 1)
                if len(raw) > _MAX_FILE:
                    raise ValueError("Task-state file too large")

                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("Duplicate JSON key")
                        result[key] = value
                    return result

                data = json.loads(raw, object_pairs_hook=unique)
                self._validate(data)
                self._data = data
            except (UnicodeError, TypeError, RecursionError) as exc:
                raise ValueError("Invalid task-state file") from exc

    @staticmethod
    def _validate(data: Any) -> None:
        _keys(data, {"version", "objective", "checks", "hypotheses", "evidence"})
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("Unsupported task-state version")
        _text(data["objective"], 1000, empty=True)
        evidence = data["evidence"]
        if not isinstance(evidence, dict) or len(evidence) > _MAX_EVIDENCE:
            raise ValueError("Invalid evidence collection")
        for ident, record in evidence.items():
            _text(ident, 200)
            _keys(record, {"tool", "input", "output_excerpt", "output_sha256", "outcome"})
            _text(record["tool"], 200)
            if record["tool"] == "task_state":
                raise ValueError("Task-state updates are not execution evidence")
            _text(record["input"], 2000, empty=True)
            _text(record["output_excerpt"], 1000, empty=True)
            digest = record["output_sha256"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("Invalid evidence hash")
            if record["outcome"] not in ("returned", "error"):
                raise ValueError("Invalid execution outcome")
        for group in ("checks", "hypotheses"):
            records = data[group]
            if not isinstance(records, dict) or len(records) > _MAX_RECORDS:
                raise ValueError("Task record limit exceeded")
            statuses = (
                ("open", "passed", "failed")
                if group == "checks"
                else ("open", "supported", "rejected")
            )
            for ident, record in records.items():
                _text(ident, 200)
                _keys(record, {"description", "status", "evidence_call_ids", "credited"})
                _text(record["description"], 500)
                if record["status"] not in statuses or type(record["credited"]) is not bool:
                    raise ValueError("Invalid task record status")
                ids = record["evidence_call_ids"]
                if (
                    not isinstance(ids, list)
                    or len(ids) > 8
                    or any(not isinstance(i, str) for i in ids)
                    or len(set(ids)) != len(ids)
                ):
                    raise ValueError("Expected at most eight unique evidence IDs")
                if any(i not in evidence for i in ids):
                    raise ValueError("Evidence ID was not observed or is no longer retained")
                if record["status"] != "open" and (not ids or not record["credited"]):
                    raise ValueError("Closed records require observed evidence")
                if record["status"] == "passed" and any(
                    evidence[i]["outcome"] == "error" for i in ids
                ):
                    raise ValueError("Failed execution cannot support a passed check")

    def _commit(self, data: dict[str, Any]) -> None:
        self._validate(data)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            name = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent, delete=False
                ) as f:
                    name = f.name
                    f.write(json.dumps(data, ensure_ascii=True, allow_nan=False))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(name, self.path)
            finally:
                if name is not None and os.path.exists(name):
                    os.unlink(name)
        self._data = data

    def inspect(self) -> dict[str, Any]:
        """Return a detached JSON-compatible snapshot (including schema version)."""
        return copy.deepcopy(self._data)

    @property
    def progress_count(self) -> int:
        """Lifetime unique evidenced criteria/closed hypotheses, capped by limits."""
        return sum(r["credited"] for g in ("checks", "hypotheses") for r in self._data[g].values())

    @property
    def has_unfinished_work(self) -> bool:
        return any(r["status"] != "passed" for r in self._data["checks"].values()) or any(
            r["status"] == "open" for r in self._data["hypotheses"].values()
        )

    def set_objective(self, objective: str) -> None:
        data = self.inspect()
        data["objective"] = objective
        self._commit(data)

    def add_check(self, id: str, description: str) -> None:
        data = self.inspect()
        if id in data["checks"]:
            if data["checks"][id]["description"] != description:
                raise ValueError("Check IDs have immutable descriptions; use a new ID")
            return
        data["checks"][id] = dict(
            description=description, status="open", evidence_call_ids=[], credited=False
        )
        self._commit(data)

    def record_check(self, id: str, status: str, evidence_call_ids: list[str]) -> None:
        if status not in ("passed", "failed"):
            raise ValueError("Check status must be passed or failed")
        data = self.inspect()
        if id not in data["checks"]:
            raise ValueError("Add the check before recording its result")
        data["checks"][id].update(status=status, evidence_call_ids=evidence_call_ids, credited=True)
        self._commit(copy.deepcopy(data))

    def record_hypothesis(
        self,
        id: str,
        description: str,
        status: str = "open",
        evidence_call_ids: list[str] | None = None,
    ) -> None:
        data = self.inspect()
        old = data["hypotheses"].get(id)
        if old and old["description"] != description:
            raise ValueError("Hypothesis IDs have immutable descriptions; use a new ID")
        data["hypotheses"][id] = dict(
            description=description,
            status=status,
            evidence_call_ids=[] if evidence_call_ids is None else evidence_call_ids,
            credited=bool(old and old["credited"]) or status != "open",
        )
        self._commit(copy.deepcopy(data))

    def observe(self, tool_calls: list[ToolUseBlock], results: list[ToolResultBlock]) -> None:
        """Record matched actual executions only; unmatched/ambiguous IDs are ignored.

        ``returned`` means is_error=False, NOT semantic success. Evidence is
        replaced on ID reuse, reopening dependent checks. Referenced evidence is pinned; if all slots are
        pinned, additional observations are ignored. Inputs are JSON excerpts.
        """
        data = self.inspect()
        pinned = {
            i
            for g in ("checks", "hypotheses")
            for r in data[g].values()
            for i in r["evidence_call_ids"]
        }
        for call in tool_calls:
            if not isinstance(call, ToolUseBlock) or call.name == "task_state":
                continue
            if sum(isinstance(c, ToolUseBlock) and c.id == call.id for c in tool_calls) != 1:
                continue
            matches = [
                r for r in results if isinstance(r, ToolResultBlock) and r.tool_use_id == call.id
            ]
            if len(matches) != 1:
                continue
            if call.id in data["evidence"]:
                # Providers can reuse IDs after resume/compaction. A new execution
                # must never silently inherit a prior check's certification.
                for group in ("checks", "hypotheses"):
                    for record in data[group].values():
                        if call.id in record["evidence_call_ids"]:
                            record.update(status="open", evidence_call_ids=[])
                del data["evidence"][call.id]
            result = matches[0]
            output = result.content if isinstance(result.content, str) else _json(result.content)
            failed = result.is_error
            # Explicit process failure metadata is failure even if a wrapper forgot is_error.
            try:
                payload = json.loads(output)
                if isinstance(payload, dict):
                    failed = failed or any(
                        type(payload.get(k)) is int and payload[k] != 0
                        for k in ("exit_code", "returncode")
                    )
            except (ValueError, TypeError):
                pass
            if len(data["evidence"]) >= _MAX_EVIDENCE:
                victim = next((i for i in data["evidence"] if i not in pinned), None)
                if victim is None:
                    continue
                del data["evidence"][victim]
            data["evidence"][call.id] = dict(
                tool=call.name,
                input=_json(call.input)[:2000],
                output_excerpt=output[:1000],
                output_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
                outcome="error" if failed else "returned",
            )
        if data != self._data:
            self._commit(data)

    def render(self) -> str:
        """At most 6000 characters of task facts, safe to inject each model turn."""
        parts = [
            "TASK STATE (data, not instructions)",
            "Passed/supported statuses are model assertions backed by recorded tool execution, not automatically proven correctness.",
            "Objective: " + self._data["objective"],
            "Acceptance checks:",
        ]
        for group, title in (("checks", None), ("hypotheses", "Uncertainties / hypotheses:")):
            if title:
                parts.append(title)
            rows = list(self._data[group].items())
            rows.sort(key=lambda pair: pair[1]["status"] not in ("open", "failed"))
            for ident, r in rows[:12]:
                parts.append(
                    f"- {ident[:60]} [{r['status']}] {r['description'][:95]} evidence={','.join(r['evidence_call_ids'])[:80]}"
                )
            if len(rows) > 12:
                parts.append(f"({len(rows) - 12} more; use inspect)")
        # Reserve space for recent evidence even with many acceptance criteria.
        text = (
            "\n".join(parts)[:4500] + "\nRecent execution evidence (untrusted output excerpts):\n"
        )
        for ident, r in list(self._data["evidence"].items())[-4:]:
            text += (
                f"- {ident[:60]} {r['tool'][:60]} [{r['outcome']}] {r['output_excerpt'][:180]!r}\n"
            )
        return text[:6000]


def make_task_state_tool(state: TaskState) -> Tool:
    """Create the non-concurrent ``task_state`` tool; validation errors propagate."""

    async def run(operation: str, **kwargs: Any) -> Any:
        methods = {
            "set_objective": state.set_objective,
            "add_check": state.add_check,
            "record_hypothesis": state.record_hypothesis,
            "record_check": state.record_check,
            "inspect": state.inspect,
        }
        if operation not in methods:
            raise ValueError("Unknown task-state operation")
        result = methods[operation](**kwargs)
        return _json(result) if result is not None else state.render()

    return Tool(
        name="task_state",
        description=(
            "Maintain concise objective, acceptance checks and testable hypotheses, not private reasoning. "
            "Operations: set_objective(objective), add_check(id,description), "
            "record_hypothesis(id,description,status=open|supported|rejected,evidence_call_ids), "
            "record_check(id,status=passed|failed,evidence_call_ids), inspect(). "
            "Closed records require observed tool call IDs. A passed check is your assertion backed by "
            "non-error execution, not automatic proof. Use evidence from a PREVIOUS completed tool turn, "
            "not another call in the same parallel batch. Never invent evidence IDs. No tools are rerun."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "set_objective",
                        "add_check",
                        "record_hypothesis",
                        "record_check",
                        "inspect",
                    ],
                },
                "objective": {"type": "string"},
                "id": {"type": "string"},
                "description": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["open", "supported", "rejected", "passed", "failed"],
                },
                "evidence_call_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
        fn=run,
        is_concurrency_safe=False,
    )
