"""Restricted Python metric runner and atomic artifact writer.

This is defense-in-depth, not an OS-grade sandbox for hostile multi-tenant execution.
The main workflow does not automatically execute model-generated Python.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from src.config import Settings

_ALLOWED_MODULES = {"json", "csv", "statistics", "math", "io"}
_ALLOWED_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "len",
    "list", "max", "min", "round", "set", "sorted", "str", "sum", "tuple", "zip", "range",
}
_SAFE_METHODS = {
    "append", "extend", "get", "items", "keys", "values", "split", "strip",
    "lower", "upper", "replace", "startswith", "endswith", "count", "join", "sort", "copy",
}
_MODULE_ATTRS = {
    "json": {"loads", "dumps"},
    "csv": {"reader", "DictReader", "Sniffer"},
    "statistics": {"mean", "median", "median_low", "median_high", "pstdev", "pvariance", "stdev", "variance"},
    "math": {"ceil", "floor", "sqrt", "log", "log10", "fabs", "isfinite"},
    "io": {"StringIO"},
}
_BLOCKED_NODES = (
    ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Raise, ast.Lambda,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Global, ast.Nonlocal,
    ast.Delete, ast.Yield, ast.YieldFrom, ast.Await,
)


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if isinstance(node, (ast.Attribute, ast.Subscript)) else node.func
    return node.id if isinstance(node, ast.Name) else None


def validate_python_source(code: str) -> ast.Module:
    if not code.strip():
        raise ValueError("Python source cannot be empty.")
    if len(code) > 12000:
        raise ValueError("Python source is limited to 12,000 characters.")
    tree = ast.parse(code, mode="exec")
    for node in ast.walk(tree):
        if isinstance(node, _BLOCKED_NODES):
            raise ValueError("This sandbox blocks dynamic control, function/class definitions, and exception/file contexts.")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError("Dunder names are not permitted in sandboxed code.")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise ValueError("Dunder attribute access is not permitted.")
            root = _root_name(node.value)
            allowed = node.attr in _SAFE_METHODS or (root in _MODULE_ATTRS and node.attr in _MODULE_ATTRS[root])
            if not allowed:
                raise ValueError(f"Attribute access is not allowed: {node.attr}")
        if isinstance(node, ast.Import):
            if any(alias.name not in _ALLOWED_MODULES for alias in node.names):
                raise ValueError("Only json, csv, statistics, math, and io imports are permitted.")
        if isinstance(node, ast.ImportFrom):
            raise ValueError("Use explicit module imports such as 'import json, csv, statistics'.")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in _ALLOWED_BUILTINS:
                    raise ValueError(f"Function calls are restricted: {node.func.id}")
            elif isinstance(node.func, ast.Attribute):
                root = _root_name(node.func.value)
                if not (node.func.attr in _SAFE_METHODS or (root in _MODULE_ATTRS and node.func.attr in _MODULE_ATTRS[root])):
                    raise ValueError(f"Function call is restricted: {node.func.attr}")
            else:
                raise ValueError("Indirect function calls are not permitted.")
    return tree


_CHILD_RUNNER = r'''
import ast, csv, io, json, math, statistics, sys
payload = json.loads(sys.argv[1])
code = payload["code"]
data = payload.get("input_data")
def limited_range(*args):
    value = range(*args)
    if len(value) > 5000:
        raise ValueError("range is limited to 5,000 iterations")
    return value
def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    allowed = {"json", "csv", "statistics", "math", "io"}
    if level or name not in allowed:
        raise ImportError("module is not permitted by the Verity-1 sandbox")
    return __import__(name, globals, locals, fromlist, level)
safe_builtins = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "float": float, "int": int, "len": len,
    "list": list, "max": max, "min": min, "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "range": limited_range, "__import__": safe_import,
}
tree = ast.parse(code, mode="exec")
namespace = {
    "__builtins__": safe_builtins, "data": data, "input_data": data,
    "json": json, "csv": csv, "statistics": statistics, "math": math, "io": io,
}
exec(compile(tree, "<verity-sandbox>", "exec"), namespace, namespace)
if "result" in namespace:
    result = namespace["result"]
elif "output" in namespace:
    result = namespace["output"]
else:
    raise ValueError("Assign the final value to result or output.")
print(json.dumps({"result": result}, ensure_ascii=False, allow_nan=False))
'''


def write_artifact(task_id: str, filename: str, content: str, output_dir: Path | str | None = None,
                   media_type: str = "text/markdown") -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
        raise ValueError("task_id contains unsupported characters.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", filename) or filename in {".", ".."}:
        raise ValueError("filename must be a plain filename without directory separators.")
    root = (Path(output_dir) if output_dir is not None else Settings.from_env().output_dir).expanduser().resolve()
    task_dir = (root / task_id).resolve()
    if task_dir.parent != root:
        raise ValueError("The task output path escaped the configured output directory.")
    task_dir.mkdir(parents=True, exist_ok=True)
    target = task_dir / filename
    payload = content.encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".verity-", dir=task_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {
        "path": f"{task_id}/{filename}", "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload), "media_type": media_type,
    }


def execute_python(code: str, input_data: Any = None, *, task_id: str = "sandbox",
                   output_name: str | None = None, output_dir: Path | str | None = None,
                   timeout_seconds: int = 3) -> dict[str, Any]:
    validate_python_source(code)
    encoded_input = json.dumps(input_data, ensure_ascii=False, allow_nan=False)
    if len(encoded_input) > 1_000_000:
        raise ValueError("Sandbox input is limited to 1 MB of JSON.")
    payload = json.dumps({"code": code, "input_data": input_data}, ensure_ascii=False, allow_nan=False)
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", _CHILD_RUNNER, payload],
            check=False, capture_output=True, text=True, timeout=max(1, min(10, timeout_seconds)),
            cwd=tempfile.gettempdir(), env={"PATH": os.environ.get("PATH", "")},
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Sandbox execution exceeded its time limit.") from exc
    if completed.returncode != 0:
        message = completed.stderr.strip().splitlines()
        raise RuntimeError(message[-1] if message else "Sandbox execution failed.")
    try:
        result = json.loads(completed.stdout)["result"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError("Sandbox returned an invalid JSON result.") from exc
    artifact = None
    if output_name:
        content = json.dumps({"task_id": task_id, "result": result}, ensure_ascii=False, indent=2, allow_nan=False)
        artifact = write_artifact(task_id, output_name, content, output_dir, "application/json")
    return {"result": result, "artifact": artifact}
