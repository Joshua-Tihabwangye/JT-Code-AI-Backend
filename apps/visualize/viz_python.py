"""User Python visuals: validate, run and sanitize (engine side; ``validate`` also runs in the API).

A Python visual is a short script that receives the visual's filtered (and, when large, evenly
sampled) rows as a pandas ``DataFrame`` named ``dataset`` — with ``pd`` and ``np`` ready — and sets
``result`` to one of:

* an ECharts option (``dict``) → drawn as a chart;
* a ``DataFrame`` or ``Series`` → drawn as a table;
* a number → drawn as a card.

Defence in depth (the script is untrusted):

1. **Static checks** (``validate``) on the parsed source, before anything runs: imports only from an
   allow-list of analysis libraries; no names or attributes starting with ``_`` (no dunder escapes
   such as ``().__class__.__subclasses__()``); no dangerous builtins (``exec``, ``eval``, ``open``,
   ``getattr``, ``type``, ``globals``…); no escape-prone attributes (``os``, ``sys``, ``system``,
   ``eval``, ``query``, ``read_*``, ``load``, ``format``…); no ``global``/``nonlocal``, classes or
   async code; a size limit.
2. **Runtime checks**: every attribute read is rewritten to go through ``_jt_attr``, which refuses to
   hand out modules outside the allow-list (``pd.io.common.os``) or functions of system modules
   (``os``, ``subprocess``, ``io``, ``pickle``…); ``import`` goes through an allow-listing importer;
   builtins are a small safe subset; ``print`` is captured.
3. **The engine sandbox**: the script runs inside the guarded engine process (no network, no
   programs, no files outside its temporary directory — ``viz_guard.py``), with memory, CPU and
   wall-clock limits, in its own process per visual.
4. **Output sanitizing** (``sanitize_option``): options are size-capped and stripped of anything the
   browser could turn into HTML, links or remote content (``formatter``, ``link``, ``image``,
   ``image://`` symbols, ``$fn`` markers).
"""

from __future__ import annotations

import ast
import io
import math
import types
from typing import Any

MAX_CODE = 20_000
MAX_OUTPUT_BYTES = 1_000_000
MAX_PRINT = 4_000
ALLOWED_MODULES = frozenset(
    {
        "pandas",
        "numpy",
        "scipy",
        "statsmodels",
        "sklearn",
        "math",
        "statistics",
        "datetime",
        "collections",
        "itertools",
        "functools",
        "json",
        "re",
        "random",
    }
)
BLOCKED_NAMES = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "open",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "globals",
        "locals",
        "vars",
        "dir",
        "type",
        "super",
        "object",
        "input",
        "breakpoint",
        "help",
        "memoryview",
        "classmethod",
        "staticmethod",
        "property",
        "exit",
        "quit",
        "copyright",
        "credits",
        "license",
        "id",
    }
)
BLOCKED_ATTRIBUTES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "system",
        "popen",
        "spawn",
        "kill",
        "remove",
        "unlink",
        "rmdir",
        "rename",
        "environ",
        "builtins",
        "modules",
        "importlib",
        "loader",
        "ctypes",
        "cffi",
        "eval",
        "exec",
        "query",
        "format",
        "format_map",
        "mro",
        "load",
        "fromfile",
        "tofile",
        "save",
        "savez",
        "savez_compressed",
        "memmap",
        "ctypeslib",
        "to_pickle",
        "open",
        "stdout",
        "stderr",
        "stdin",
        "f_globals",
        "f_locals",
        "f_back",
        "f_builtins",
        "gi_frame",
        "gi_code",
        "cr_frame",
        "ag_frame",
        "tb_frame",
        "tb_next",
        "co_code",
        "func_globals",
        "getattr",
        "setattr",
        "globals",
        "shell",
        "dlopen",
        "cdll",
    }
)
BLOCKED_PREFIXES = ("read_", "_")
# Functions defined in these modules are never handed to a script (top-level module names).
SYSTEM_MODULES = frozenset(
    {
        "os",
        "posix",
        "nt",
        "sys",
        "subprocess",
        "shutil",
        "socket",
        "ctypes",
        "importlib",
        "builtins",
        "io",
        "_io",
        "pathlib",
        "pickle",
        "_pickle",
        "marshal",
        "shelve",
        "tempfile",
        "signal",
        "threading",
        "multiprocessing",
        "_posixsubprocess",
        "code",
        "codeop",
        "runpy",
        "inspect",
        "gc",
        "glob",
        "fcntl",
        "resource",
        "mmap",
    }
)
FORBIDDEN_NODES = (
    ast.Global,
    ast.Nonlocal,
    ast.ClassDef,
    ast.AsyncFunctionDef,
    ast.AsyncFor,
    ast.AsyncWith,
    ast.Await,
)
SAFE_BUILTINS = (
    "abs",
    "all",
    "any",
    "bool",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "frozenset",
    "int",
    "isinstance",
    "len",
    "list",
    "map",
    "max",
    "min",
    "pow",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "ZeroDivisionError",
    "ArithmeticError",
    "True",
    "False",
    "None",
)
DROP_KEYS = frozenset({"formatter", "link", "sublink", "image", "$fn", "renderItem", "target", "subtarget"})


class PythonVisualError(ValueError):
    """The script is not allowed, failed, or returned something that cannot be drawn."""


def _blocked_attribute(name: str) -> bool:
    return name in BLOCKED_ATTRIBUTES or name.startswith(BLOCKED_PREFIXES)


def validate(code: Any) -> list[str]:
    """Problems that stop the script from running (empty when it may run)."""
    if not isinstance(code, str) or not code.strip():
        return ["Write a script that sets `result`."]
    if len(code) > MAX_CODE:
        return [f"Scripts are limited to {MAX_CODE:,} characters."]
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return [f"Line {exc.lineno}: {exc.msg}."]
    problems: list[str] = []

    def report(node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", "?")
        problems.append(f"Line {line}: {message}")

    for node in ast.walk(tree):
        if isinstance(node, FORBIDDEN_NODES):
            report(node, f"{type(node).__name__.replace('Def', '')} statements are not allowed.")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_MODULES:
                    report(node, f"importing {alias.name!r} is not allowed.")
                if alias.asname and alias.asname.startswith("_"):
                    report(node, "names may not start with '_'.")
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module or node.module.split(".")[0] not in ALLOWED_MODULES:
                report(node, f"importing from {node.module or '.'!r} is not allowed.")
            for alias in node.names:
                if alias.name == "*":
                    report(node, "'import *' is not allowed.")
                elif _blocked_attribute(alias.name) or (alias.asname or "").startswith("_"):
                    report(node, f"importing {alias.name!r} is not allowed.")
        elif isinstance(node, ast.Name):
            if node.id.startswith("_"):
                report(node, "names may not start with '_'.")
            elif node.id in BLOCKED_NAMES:
                report(node, f"{node.id!r} is not available in Python visuals.")
        elif isinstance(node, ast.Attribute):
            if _blocked_attribute(node.attr):
                report(node, f"the attribute {node.attr!r} is not allowed.")
        elif isinstance(node, (ast.FunctionDef, ast.Lambda)):
            arguments = node.args
            names = [a.arg for a in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
            names += [a.arg for a in (arguments.vararg, arguments.kwarg) if a is not None]
            if isinstance(node, ast.FunctionDef):
                names.append(node.name)
            if any(name.startswith("_") for name in names):
                report(node, "names may not start with '_'.")
        elif (isinstance(node, ast.keyword) and (node.arg or "").startswith("_")) or (
            isinstance(node, ast.ExceptHandler) and (node.name or "").startswith("_")
        ):
            report(node, "names may not start with '_'.")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("__"):
            # Strings like "__class__" only matter as attribute names, which are checked above,
            # but subscripting by them (e.g. ``vars(x)["__dict__"]``) is never needed in a visual.
            report(node, "strings starting with '__' are not allowed.")
        if len(problems) >= 10:
            break
    return problems


class _Rewrite(ast.NodeTransformer):
    """Every attribute *read* goes through the runtime check ``_jt_attr``."""

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load):
            call = ast.Call(
                func=ast.Name(id="_jt_attr", ctx=ast.Load()),
                args=[node.value, ast.Constant(node.attr)],
                keywords=[],
            )
            return ast.copy_location(call, node)
        return node


def _top(name: Any) -> str:
    return str(name or "").split(".")[0]


def _check_value(value: Any, name: str) -> Any:
    if isinstance(value, types.ModuleType):
        if _top(value.__name__) not in ALLOWED_MODULES:
            raise PythonVisualError(f"Access to the module {value.__name__!r} is not allowed.")
        return value
    if callable(value) and _top(getattr(value, "__module__", None)) in SYSTEM_MODULES:
        raise PythonVisualError(f"{name!r} is not available in Python visuals.")
    return value


def _jt_attr(target: Any, name: str) -> Any:
    if _blocked_attribute(name):
        raise PythonVisualError(f"The attribute {name!r} is not allowed.")
    return _check_value(getattr(target, name), name)


def _importer(
    name: str, globals_: Any = None, locals_: Any = None, fromlist: Any = (), level: int = 0
) -> Any:
    if level or _top(name) not in ALLOWED_MODULES:
        raise PythonVisualError(f"Importing {name!r} is not allowed.")
    import importlib

    module = importlib.import_module(name)
    if fromlist:
        return module
    return importlib.import_module(_top(name))


def run(code: str, dataset: Any) -> tuple[Any, str]:
    """Run a validated script; returns its ``result`` and what it printed."""
    problems = validate(code)
    if problems:
        raise PythonVisualError(problems[0])
    import builtins

    import numpy as np
    import pandas as pd

    tree = ast.fix_missing_locations(_Rewrite().visit(ast.parse(code, mode="exec")))
    printed = io.StringIO()

    def capture(*values: Any, sep: str = " ", end: str = "\n", **_: Any) -> None:
        if printed.tell() < MAX_PRINT:
            printed.write(sep.join(str(v) for v in values) + end)

    safe = {name: getattr(builtins, name) for name in SAFE_BUILTINS if hasattr(builtins, name)}
    safe.update({"print": capture, "__import__": _importer})
    scope: dict[str, Any] = {
        "__builtins__": safe,
        "_jt_attr": _jt_attr,
        "pd": pd,
        "np": np,
        "math": math,
        "dataset": dataset,
    }
    try:
        exec(compile(tree, "<python visual>", "exec"), scope)  # noqa: S102 - validated, rewritten, sandboxed
    except PythonVisualError:
        raise
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 - the script's own errors are shown to its author
        line = getattr(exc, "lineno", None)
        raise PythonVisualError(
            f"{type(exc).__name__}: {str(exc)[:240]}" + (f" (line {line})" if line else "")
        ) from None
    if "result" not in scope:
        raise PythonVisualError("Set `result` to an ECharts option (dict), a DataFrame or a number.")
    return scope["result"], printed.getvalue()[:MAX_PRINT]


def _marker(value: Any) -> bool:
    """A ``{"$fn": name, ...}`` formatter marker: a name the browser maps to a built-in function."""
    return isinstance(value, dict) and isinstance(value.get("$fn"), str) and len(value) <= 6


def _plain(value: Any, depth: int, budget: list[int], markers: bool = False) -> Any:
    """JSON-safe copy of an option without HTML/link/remote-content keys; raises when too large.

    With ``markers`` (charts the engine built), formatter *markers* are kept; formatter strings,
    which ECharts may render as HTML, are always dropped."""
    budget[0] -= 1
    if budget[0] < 0 or depth > 30:
        raise PythonVisualError("The chart option is too large or nested too deeply.")
    if isinstance(value, dict):
        if markers and _marker(value):
            return {
                str(k)[:40]: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:40])
                for k, v in value.items()
            }
        return {
            str(key)[:100]: _plain(item, depth + 1, budget, markers)
            for key, item in value.items()
            if str(key) not in DROP_KEYS
            or (markers and key in {"formatter", "valueFormatter"} and _marker(item))
        }
    if isinstance(value, (list, tuple)):
        if len(value) > 50_000:
            raise PythonVisualError("The chart has too many points (50,000 at most).")
        return [_plain(item, depth + 1, budget, markers) for item in value]
    if isinstance(value, str):
        if value.lower().startswith(("image://", "http://", "https://", "javascript:", "data:")):
            return ""
        return value[:2000]
    if isinstance(value, bool) or value is None:
        return value
    if hasattr(value, "item") and callable(value.item):  # numpy scalars
        try:
            value = value.item()
        except TypeError, ValueError:
            return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "tolist"):
        return _plain(value.tolist(), depth + 1, budget)
    return str(value)[:2000]


def sanitize_option(option: dict[str, Any], *, markers: bool = False) -> dict[str, Any]:
    import json

    clean = _plain(option, 0, [400_000], markers)
    if len(json.dumps(clean, default=str)) > MAX_OUTPUT_BYTES:
        raise PythonVisualError("The chart option is larger than 1 MB.")
    return clean
