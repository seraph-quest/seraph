"""Hermes-style session todo runtime tool."""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

from smolagents import Tool

from src.approval.runtime import get_current_session_id
from src.agent.session import session_manager


def _parse_items(items: str) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for raw_line in items.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        completed = False
        if line.startswith("- "):
            line = line[2:].strip()
        if line.startswith("* "):
            line = line[2:].strip()
        if line.startswith("[x]") or line.startswith("[X]"):
            completed = True
            line = line[3:].strip()
        elif line.startswith("[ ]"):
            line = line[3:].strip()
        if line:
            parsed.append({"content": line, "completed": completed})
    return parsed


def _render_todos(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Todo list is empty."
    lines = []
    open_count = 0
    completed_count = 0
    for index, item in enumerate(items, start=1):
        mark = "x" if item.get("completed") else " "
        if item.get("completed"):
            completed_count += 1
        else:
            open_count += 1
        lines.append(f"{index}. [{mark}] {item.get('content', '').strip()}")
    lines.append("")
    lines.append(f"Open: {open_count} · Completed: {completed_count}")
    return "\n".join(lines)


class TodoTool(Tool):
    skip_forward_signature_validation = True

    def __init__(self) -> None:
        super().__init__()
        self.name = "todo"
        self.description = (
            "Manage the current session's task list with explicit persistent state. "
            "Use this to track multi-step plans instead of keeping them only in prompt text."
        )
        self.inputs = {
            "action": {
                "type": "string",
                "description": "One of: list, set, add, complete, reopen, remove, clear.",
            },
            "items": {
                "type": "string",
                "description": "Newline-separated todo items for set/add actions. Prefix completed items with [x].",
                "nullable": True,
            },
            "item_id": {
                "type": "string",
                "description": "Todo id or 1-based list position for complete, reopen, or remove.",
                "nullable": True,
            },
        }
        self.output_type = "string"
        self._last_audit_payload: tuple[str, dict[str, Any]] | None = None
        self.is_initialized = True

    def forward(self, action: str, items: str = "", item_id: str = "") -> str:
        return self.__call__(action=action, items=items, item_id=item_id)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        if len(args) == 1 and not kwargs and isinstance(args[0], dict):
            payload = args[0]
            action = "" if payload.get("action") is None else str(payload.get("action", ""))
            items = "" if payload.get("items") is None else str(payload.get("items", ""))
            item_id = "" if payload.get("item_id") is None else str(payload.get("item_id", ""))
        else:
            raw_action = kwargs.get("action", args[0] if args else "")
            raw_items = kwargs.get("items", args[1] if len(args) > 1 else "")
            raw_item_id = kwargs.get("item_id", args[2] if len(args) > 2 else "")
            action = "" if raw_action is None else str(raw_action)
            items = "" if raw_items is None else str(raw_items)
            item_id = "" if raw_item_id is None else str(raw_item_id)

        session_id = get_current_session_id()
        if not session_id:
            self._last_audit_payload = None
            return "Error: Todo list is only available inside a conversation session."

        normalized = action.strip().lower()
        result_items: list[dict[str, Any]]
        if normalized == "list":
            result_items = _run(session_manager.get_todos(session_id))
        elif normalized == "set":
            parsed = _parse_items(items)
            result_items = _run(session_manager.replace_todos(session_id, parsed))
        elif normalized == "add":
            parsed = _parse_items(items)
            result_items = _run(session_manager.append_todos(session_id, parsed))
        elif normalized == "complete":
            result_items = _run(session_manager.update_todo_completion(session_id, item_id, completed=True))
            if result_items is None:
                self._last_audit_payload = None
                return f"Error: Todo '{item_id}' was not found."
        elif normalized == "reopen":
            result_items = _run(session_manager.update_todo_completion(session_id, item_id, completed=False))
            if result_items is None:
                self._last_audit_payload = None
                return f"Error: Todo '{item_id}' was not found."
        elif normalized == "remove":
            result_items = _run(session_manager.remove_todo(session_id, item_id))
            if result_items is None:
                self._last_audit_payload = None
                return f"Error: Todo '{item_id}' was not found."
        elif normalized == "clear":
            _run(session_manager.clear_todos(session_id))
            result_items = []
        else:
            self._last_audit_payload = None
            return "Error: Unsupported todo action. Use list, set, add, complete, reopen, remove, or clear."

        open_count = sum(1 for item in result_items if not item.get("completed"))
        completed_count = sum(1 for item in result_items if item.get("completed"))
        self._last_audit_payload = (
            f"todo {normalized} — {open_count} open, {completed_count} completed",
            {
                "action": normalized,
                "open_count": open_count,
                "completed_count": completed_count,
                "total_count": len(result_items),
                "items": [
                    {
                        "id": item.get("id"),
                        "content": item.get("content"),
                        "completed": bool(item.get("completed")),
                    }
                    for item in result_items
                ],
            },
        )
        return _render_todos(result_items)

    def get_audit_result_payload(
        self,
        _arguments: dict[str, Any],
        _result: Any,
    ) -> tuple[str, dict[str, Any]] | None:
        return self._last_audit_payload


def _run(coro):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


todo = TodoTool()
