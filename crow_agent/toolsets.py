"""Tool registry: decorator-based registration, JSON schema compilation, execution wrappers."""

from __future__ import annotations

import inspect
import logging
import json
import types
from dataclasses import dataclass, field
from typing import Any, Callable, get_type_hints

logger = logging.getLogger("crow_agent.toolsets")


@dataclass
class Tool:
    """A registered tool with its schema and callable."""
    name: str
    description: str
    parameters_schema: dict[str, Any]  # JSON Schema object
    fn: Callable[..., Any]
    check_fn: Callable[[], bool] | None = None  # ponytail: env-check

    def to_openai_tool(self) -> dict[str, Any]:
        """Format for OpenAI-compatible tool_calls API."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


@dataclass
class LazyExtension:
    """A group of tools registered on demand via trigger matching."""
    name: str
    trigger_pattern: str  # regex to match against user message
    register_fn: Callable[["ToolRegistry"], None]  # registers all tools in this extension
    _active: bool = False


class ToolRegistry:
    """Central registry. Decorator-based registration, dict-based lookup."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._lazy: list[LazyExtension] = []

    def register(
        self,
        name: str | None = None,
        description: str = "",
        check_fn: Callable[[], bool] | None = None,
    ) -> Callable:
        """Decorator: register a function as a tool.

        Usage:
            @registry.register(description="Run a shell command")
            def run_cmd(command: str, timeout: int = 30) -> str: ...
        """
        def decorator(fn: Callable) -> Callable:
            tool_name = name or fn.__name__
            if check_fn is not None:
                try:
                    if not check_fn():
                        logger.warning("Tool '%s' skipped: check_fn returned falsy", tool_name)
                        return fn
                except Exception as exc:
                    logger.warning("Tool '%s' skipped: check_fn raised %s", tool_name, exc)
                    return fn
            schema = _compile_schema(fn)
            self._tools[tool_name] = Tool(
                name=tool_name,
                description=description or fn.__doc__ or "",
                parameters_schema=schema,
                fn=fn,
                check_fn=check_fn,
            )
            return fn
        return decorator

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def remove(self, name: str) -> bool:
        """Remove a tool by name. Returns True if it existed."""
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def register_lazy(self, name: str, trigger_pattern: str, register_fn: Callable[["ToolRegistry"], None]) -> None:
        """Register an extension for lazy loading. Tools only appear when trigger matches."""
        ext = LazyExtension(name=name, trigger_pattern=trigger_pattern, register_fn=register_fn)
        self._lazy.append(ext)
        logger.info("Extension registered (lazy): %s (trigger: %s)", name, trigger_pattern)

    def activate_extensions(self, user_message: str) -> int:
        """Activate lazy extensions whose trigger matches user_message. Returns count activated."""
        import re
        activated = 0
        for ext in self._lazy:
            if ext._active:
                continue
            try:
                if re.search(ext.trigger_pattern, user_message, re.IGNORECASE):
                    ext.register_fn(self)
                    ext._active = True
                    activated += 1
                    logger.info("Extension activated: %s", ext.name)
            except Exception:
                logger.warning("Extension activation failed: %s", ext.name, exc_info=True)
        return activated

    def all_schemas(self) -> list[dict[str, Any]]:
        """Return all tools formatted for OpenAI-compatible API."""
        return [t.to_openai_tool() for t in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool by name with given arguments. Returns string result."""
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        result = tool.fn(**arguments)
        if not isinstance(result, str):
            return json.dumps(result) if isinstance(result, (dict, list)) else str(result)
        return result


# --- Schema compilation ---

_PY_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _resolve_type(py_type: type) -> dict[str, Any]:
    """Resolve a Python type to a JSON Schema property dict.

    Handles generics (list[str] → array with items), Optional/Union
    (str | None → string, not nullable), and dict generics.
    """
    # UnionType: str | None, int | float, etc. (PEP 604)
    if isinstance(py_type, types.UnionType):
        args = py_type.__args__
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            # Optional[X] — strip None, resolve X
            return _resolve_type(non_none[0])
        if len(non_none) > 1:
            # Union[X, Y] — use first (better than raw "string")
            return _resolve_type(non_none[0])

    # GenericAlias: list[str], dict[str, Any]
    if isinstance(py_type, types.GenericAlias):
        origin = py_type.__origin__
        if origin is list:
            items = py_type.__args__[0] if py_type.__args__ else str
            return {"type": "array", "items": _resolve_type(items)}
        if origin is dict:
            val = py_type.__args__[1] if len(py_type.__args__) > 1 else str
            return {"type": "object", "additionalProperties": _resolve_type(val)}

    # Simple type
    type_str = _PY_TYPE_MAP.get(py_type, "string")
    return {"type": type_str}


def _compile_schema(fn: Callable) -> dict[str, Any]:
    """Build a JSON Schema 'object' from a function's signature and type hints."""
    hints = get_type_hints(fn)
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        py_type = hints.get(param_name, str)
        prop = _resolve_type(py_type)
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
        else:
            prop["default"] = param.default
        properties[param_name] = prop

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema
