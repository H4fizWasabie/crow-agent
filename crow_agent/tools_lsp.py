"""LSP-like code intelligence tools for Crow.

Uses jedi (pure Python static analysis) for definition/references/hover.
Uses py_compile + ruff (optional) for diagnostics.
No language server process needed.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("crow_agent.lsp")


def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register LSP tools."""

    @registry.register(
        description="Jump to the definition of a symbol (function, class, variable). Returns file path and line number."
    )
    def lsp_definition(symbol: str, file_path: str, line: int = 1) -> str:
        path = Path(file_path)
        if not path.exists():
            return f"File not found: {file_path}"

        try:
            import jedi
        except ImportError:
            return "jedi not installed. Run: pip install jedi"

        try:
            script = jedi.Script(path=path)
            names = script.goto(line=line, column=len(symbol), follow_imports=True)
            if not names:
                return f"Symbol '{symbol}' not found at {file_path}:{line}"

            results = []
            for n in names[:10]:
                loc = f"{n.module_path}:{n.line}:{n.column}" if n.module_path else "built-in"
                results.append(f"{n.type} {n.name} → {loc}")
                if n.description:
                    results[-1] += f"  ({n.description[:120]})"
            return "\n".join(results)
        except Exception as e:
            return f"lsp_definition error: {e}"

    @registry.register(
        description="Find all references to a symbol across the project. Returns file:line locations."
    )
    def lsp_references(symbol: str, file_path: str, line: int = 1) -> str:
        path = Path(file_path)
        if not path.exists():
            return f"File not found: {file_path}"

        try:
            import jedi
        except ImportError:
            return "jedi not installed. Run: pip install jedi"

        try:
            script = jedi.Script(path=path)
            refs = script.get_references(line=line, column=len(symbol))
            if not refs:
                return f"No references found for '{symbol}'"

            results = []
            for r in refs[:30]:
                loc = f"{r.module_path}:{r.line}" if r.module_path else "built-in"
                # Get context line
                try:
                    if r.module_path and Path(r.module_path).exists():
                        ctx = Path(r.module_path).read_text().split("\n")[r.line - 1].strip()[:100]
                    else:
                        ctx = ""
                except Exception:
                    ctx = ""
                results.append(f"{r.type} → {loc}  {ctx}")
            return "\n".join(results)
        except Exception as e:
            return f"lsp_references error: {e}"

    @registry.register(
        description="Get type signature, docstring, and parameter info for a symbol."
    )
    def lsp_hover(symbol: str, file_path: str, line: int = 1) -> str:
        path = Path(file_path)
        if not path.exists():
            return f"File not found: {file_path}"

        try:
            import jedi
        except ImportError:
            return "jedi not installed. Run: pip install jedi"

        try:
            script = jedi.Script(path=path)
            names = script.infer(line=line, column=len(symbol))
            if not names:
                return f"No type info found for '{symbol}'"

            results = []
            for n in names[:5]:
                sig = f"{n.name}"
                if hasattr(n, "type") and n.type:
                    sig = f"{n.type} {sig}"
                if hasattr(n, "params") and n.params:
                    params_str = ", ".join(f"{p.name}: {p.description}" for p in n.params)
                    sig = f"{sig}({params_str})"
                results.append(sig)
                if hasattr(n, "docstring") and n.docstring:
                    doc = n.docstring() if callable(n.docstring) else str(n.docstring)
                    results.append(f"  {doc[:300]}")
            return "\n".join(results) if results else f"No hover info for '{symbol}'"
        except Exception as e:
            return f"lsp_hover error: {e}"

    @registry.register(
        description="Check a Python file for syntax errors and lint issues. Uses py_compile + ruff if available."
    )
    def lsp_diagnostics(file_path: str) -> str:
        path = Path(file_path)
        if not path.exists():
            return f"File not found: {file_path}"

        results = []

        # 1. Syntax check (always available, zero deps)
        try:
            import py_compile
            py_compile.compile(str(path), doraise=True)
            results.append("✅ Syntax: OK")
        except py_compile.PyCompileError as e:
            results.append(f"❌ Syntax error: {e}")

        # 2. Ruff lint (optional, fast)
        try:
            r = subprocess.run(
                ["ruff", "check", str(path), "--output-format", "text"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                results.append("✅ Lint: clean")
            else:
                lines = r.stdout.strip().split("\n")[:10]
                results.append(f"⚠️ Lint issues ({len(lines)}):\n" + "\n".join(lines))
        except FileNotFoundError:
            results.append("💡 ruff not installed. Run: pip install ruff")
        except Exception as e:
            results.append(f"⚠️ Lint check failed: {e}")

        return "\n\n".join(results)
