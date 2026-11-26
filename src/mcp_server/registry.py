"""MCP tool definitions derived from the Meeting Desk action registry.

This module deliberately has no MCP SDK dependency so the desktop CLI and the
offline test suite continue to work without the optional SDK.
"""
from __future__ import annotations

from typing import Any, Iterable

from src.desktop.service import ACTION_REGISTRY

ALLOWED_ACTIONS = (
    "help", "environment", "list", "list_jobs", "preview",
    "handoff", "import_cleaned", "import_job", "import_notes",
)

_WRITE_TEXT = {
    "handoff": "會寫入 LLM inbox 交接工作與會議狀態。",
    "import_cleaned": "會寫入或更新會議清洗稿與狀態。",
    "import_job": "會匯入 outbox 清洗結果、更新清洗稿並封存工作。",
    "import_notes": "會寫入會議記錄 coverage／草稿檔並封存工作。",
}

# These values describe the actual service behaviour, not merely whether the
# action is read-only.  A destructive action may replace or archive artifacts.
_ANNOTATIONS = {
    "help": (True, False, True),
    "environment": (True, False, True),
    "list": (True, False, True),
    "list_jobs": (True, False, True),
    "preview": (True, False, True),
    "handoff": (False, False, True),
    "import_cleaned": (False, True, False),
    "import_job": (False, True, False),
    "import_notes": (False, False, False),
}


def _json_type(type_name: str) -> dict[str, Any]:
    if type_name == "string_or_array":
        return {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}
    if type_name not in {"string", "boolean", "object", "array"}:
        raise ValueError(f"不支援的 action schema 型別：{type_name}")
    schema: dict[str, Any] = {"type": type_name}
    if type_name == "array":
        schema["items"] = {}
    return schema


def schema_for_action(action: dict[str, Any]) -> dict[str, Any]:
    """Convert one service registry entry to a JSON Schema object."""
    properties: dict[str, Any] = {}
    for name, spec in action["required"].items():
        properties[name] = _json_type(spec)
    for name, spec in action["optional"].items():
        schema = _json_type(spec["type"])
        schema["default"] = spec.get("default")
        properties[name] = schema
    return {
        "type": "object",
        "properties": properties,
        "required": list(action["required"]),
        "additionalProperties": False,
    }


def _is_json_type(value: Any, type_name: str) -> bool:
    """Check JSON Schema's basic types without treating bool as an integer."""
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    return False


def validate_json_schema(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    """Return validation errors for the small JSON Schema subset we publish.

    This intentionally stays SDK-independent.  The registry currently needs
    object properties, required fields, additionalProperties, basic types,
    arrays and anyOf; supporting exactly that subset keeps the server and
    offline tests on the same validation path.
    """
    if "anyOf" in schema:
        if any(not validate_json_schema(option, value, path) for option in schema["anyOf"]):
            return []
        return [f"{path} 不符合 anyOf 型別。"]

    type_name = schema.get("type")
    if type_name and not _is_json_type(value, type_name):
        return [f"{path} 必須是 {type_name}。"]

    errors: list[str] = []
    if type_name == "object":
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}.{name} 為必要欄位。")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{path}.{name} 是未知欄位。")
        for name, child_schema in properties.items():
            if name in value:
                errors.extend(validate_json_schema(child_schema, value[name], f"{path}.{name}"))
    elif type_name == "array" and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(validate_json_schema(schema["items"], item, f"{path}[{index}]"))
    return errors


def validate_arguments(schema: dict[str, Any], arguments: Any) -> None:
    """Raise ValueError unless arguments satisfy a published input schema."""
    if not isinstance(arguments, dict):
        raise ValueError("工具 arguments 必須是 object。")
    errors = validate_json_schema(schema, arguments)
    if errors:
        raise ValueError("；".join(errors))


def action_tool_specs(registry: Iterable[dict[str, Any]] = ACTION_REGISTRY) -> list[dict[str, Any]]:
    """Return SDK-neutral MCP tool specifications for the explicit allowlist."""
    by_name = {action["name"]: action for action in registry}
    specs = []
    for name in ALLOWED_ACTIONS:
        action = by_name.get(name)
        if action is None:
            continue
        read_only, destructive, idempotent = _ANNOTATIONS[action["name"]]
        if read_only != action["read_only"]:
            raise ValueError(f"MCP annotation 與 registry read_only 不一致：{action['name']}")
        text = action["summary"]
        if read_only:
            text += "（唯讀；不會寫入會議資料。）"
        else:
            text += f"（{_WRITE_TEXT[action['name']]}）"
        specs.append({
            "name": action["name"],
            "description": text,
            "inputSchema": schema_for_action(action),
            "annotations": {
                "readOnlyHint": read_only,
                "destructiveHint": destructive,
                "idempotentHint": idempotent,
            },
            "action": action,
        })
    return specs
