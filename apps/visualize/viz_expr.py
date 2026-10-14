"""Calculated measures: a small, safe expression language (no ``eval``, no Python AST).

Syntax (Power BI-flavoured)::

    DIVIDE(SUM([profit]), SUM([revenue]))            -- profit margin
    SUM([revenue]) * (1 + [Price change])            -- uses a what-if parameter
    IF(SUM([units]) > 1000, SUM([revenue]), 0)
    [Profit margin] * 100                            -- another calculation, by name

* numbers, ``+ - * /``, comparisons (``= <> < <= > >=``) and parentheses;
* aggregates of a dataset field: ``SUM AVG AVERAGE COUNT DISTINCTCOUNT MIN MAX MEDIAN`` and
  ``COUNTROWS()``;
* scalar functions: ``DIVIDE(a, b[, alternate])``, ``IF(condition, then, else)``, ``AND``, ``OR``,
  ``NOT``, ``ABS``, ``ROUND(x[, digits])``, ``SQRT``, and ``MIN(a, b)`` / ``MAX(a, b)``;
* a bare ``[Name]`` is a what-if parameter or another calculation.

``parse`` turns text into a JSON tree of a few whitelisted node types; ``resolve`` replaces
parameters by their values and inlines other calculations (bounded depth, no cycles);
``aggregates`` lists the aggregate leaves, which the query planner turns into ordinary measures
(computed by Postgres or pandas); ``evaluate`` computes the tree over the aggregated rows with
numpy. Nothing here imports Django, so the sandboxed engine loads this file too.
"""

from __future__ import annotations

import re
from typing import Any

MAX_TEXT = 1000
MAX_NODES = 200
MAX_DEPTH = 30
MAX_AGGREGATES = 8
MAX_NESTING = 5
AGGREGATES = {
    "SUM": "sum",
    "AVG": "avg",
    "AVERAGE": "avg",
    "COUNT": "count",
    "DISTINCTCOUNT": "distinct",
    "MIN": "min",
    "MAX": "max",
    "MEDIAN": "median",
}
# name → (min args, max args)
SCALARS = {
    "DIVIDE": (2, 3),
    "IF": (3, 3),
    "AND": (2, 2),
    "OR": (2, 2),
    "NOT": (1, 1),
    "ABS": (1, 1),
    "ROUND": (1, 2),
    "SQRT": (1, 1),
    "MIN": (2, 2),
    "MAX": (2, 2),
}
COMPARISONS = ("<=", ">=", "<>", "=", "<", ">")
TOKEN = re.compile(
    r"\s*(?:(?P<number>\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    r"|\[(?P<ref>[^\[\]]{1,200})\]"
    r"|(?P<name>[A-Za-z][A-Za-z0-9_]{0,30})"
    r"|(?P<op><=|>=|<>|[-+*/(),=<>]))"
)


class ExprError(ValueError):
    """The expression is not valid; the message says why (and where)."""


def _tokens(text: str) -> list[tuple[str, str, int]]:
    if len(text) > MAX_TEXT:
        raise ExprError(f"Expressions are limited to {MAX_TEXT} characters.")
    out: list[tuple[str, str, int]] = []
    position = 0
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = TOKEN.match(text, position)
        if not match or match.end() == position:
            raise ExprError(
                f"Unexpected character {text[position:].lstrip()[:1]!r} at position {position + 1}."
            )
        kind = match.lastgroup or "op"
        value = match.group(0).strip() if kind == "number" else match.group(kind)
        out.append((kind, value, match.start(kind) if kind != "number" else match.start()))
        position = match.end()
    return out


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = _tokens(text)
        self.index = 0
        self.nodes = 0

    def peek(self) -> tuple[str, str, int] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self, value: str | None = None) -> tuple[str, str, int]:
        token = self.peek()
        if token is None:
            raise ExprError("The expression ends too early." if value is None else f"Expected {value!r}.")
        if value is not None and token[1] != value:
            raise ExprError(f"Expected {value!r} at position {token[2] + 1}, found {token[1]!r}.")
        self.index += 1
        return token

    def node(self, *parts: Any) -> list[Any]:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ExprError("The expression is too long.")
        return list(parts)

    def parse(self) -> list[Any]:
        if not self.tokens:
            raise ExprError("Write an expression, e.g. SUM([revenue]).")
        tree = self.comparison(0)
        token = self.peek()
        if token is not None:
            raise ExprError(f"Unexpected {token[1]!r} at position {token[2] + 1}.")
        return tree

    def comparison(self, depth: int) -> list[Any]:
        left = self.additive(depth)
        token = self.peek()
        if token and token[0] == "op" and token[1] in COMPARISONS:
            self.take()
            return self.node("cmp", token[1], left, self.additive(depth))
        return left

    def additive(self, depth: int) -> list[Any]:
        tree = self.term(depth)
        while (token := self.peek()) and token[0] == "op" and token[1] in "+-":
            self.take()
            tree = self.node("op", token[1], tree, self.term(depth))
        return tree

    def term(self, depth: int) -> list[Any]:
        tree = self.unary(depth)
        while (token := self.peek()) and token[0] == "op" and token[1] in "*/":
            self.take()
            tree = self.node("op", token[1], tree, self.unary(depth))
        return tree

    def unary(self, depth: int) -> list[Any]:
        if depth > MAX_DEPTH:
            raise ExprError("The expression is nested too deeply.")
        token = self.peek()
        if token and token[0] == "op" and token[1] in "+-":
            self.take()
            inner = self.unary(depth + 1)
            return self.node("neg", inner) if token[1] == "-" else inner
        return self.primary(depth)

    def primary(self, depth: int) -> list[Any]:
        kind, value, position = self.take()
        if kind == "number":
            return self.node("num", float(value))
        if kind == "ref":
            return self.node("ref", value.strip())
        if kind == "op" and value == "(":
            inner = self.comparison(depth + 1)
            self.take(")")
            return inner
        if kind == "name":
            upper = value.upper()
            self.take("(")
            args: list[list[Any]] = []
            raw_refs: list[str | None] = []
            if self.peek() and self.peek()[1] != ")":  # type: ignore[index]
                while True:
                    start = self.peek()
                    raw_refs.append(start[1] if start and start[0] == "ref" else None)
                    args.append(self.comparison(depth + 1))
                    if self.peek() and self.peek()[1] == ",":  # type: ignore[index]
                        self.take(",")
                        continue
                    break
            self.take(")")
            if upper == "COUNTROWS":
                if args:
                    raise ExprError("COUNTROWS() takes no arguments.")
                return self.node("agg", "count", "*")
            if upper in AGGREGATES and len(args) == 1:
                if args[0][0] != "ref" or raw_refs[0] is None:
                    raise ExprError(f"{upper} needs a field, e.g. {upper}([revenue]).")
                return self.node("agg", AGGREGATES[upper], args[0][1])
            if upper in SCALARS:
                low, high = SCALARS[upper]
                if not low <= len(args) <= high:
                    expected = str(low) if low == high else f"{low}–{high}"
                    raise ExprError(f"{upper} takes {expected} arguments.")
                return self.node("fn", upper, args)
            raise ExprError(f"Unknown function {value!r} at position {position + 1}.")
        raise ExprError(f"Unexpected {value!r} at position {position + 1}.")


def parse(text: Any) -> list[Any]:
    if not isinstance(text, str):
        raise ExprError("An expression must be text.")
    return _Parser(text).parse()


def _walk(tree: list[Any]) -> list[list[Any]]:
    out = [tree]
    kind = tree[0]
    if kind in {"op", "cmp"}:
        out += _walk(tree[2]) + _walk(tree[3])
    elif kind == "neg":
        out += _walk(tree[1])
    elif kind == "fn":
        for arg in tree[2]:
            out += _walk(arg)
    return out


def references(tree: list[Any]) -> list[str]:
    """Bare ``[Name]`` references (parameters or other calculations)."""
    return [node[1] for node in _walk(tree) if node[0] == "ref"]


def resolve(
    tree: list[Any],
    parameters: dict[str, Any] | None = None,
    calculations: dict[str, str] | None = None,
    _seen: tuple[str, ...] = (),
) -> list[Any]:
    """Replace ``[Name]`` by a parameter's value or the named calculation's (resolved) tree."""
    parameters = {str(k).lower(): v for k, v in (parameters or {}).items()}
    calcs = {str(k).lower(): v for k, v in (calculations or {}).items()}

    def visit(node: list[Any]) -> list[Any]:
        kind = node[0]
        if kind == "ref":
            key = node[1].lower()
            if key in parameters:
                try:
                    return ["num", float(parameters[key])]
                except (TypeError, ValueError) as exc:
                    raise ExprError(f"The parameter {node[1]!r} has no numeric value.") from exc
            if key in calcs:
                if key in _seen:
                    raise ExprError(f"The calculation {node[1]!r} refers to itself.")
                if len(_seen) >= MAX_NESTING:
                    raise ExprError("Calculations are nested too deeply.")
                return resolve(parse(calcs[key]), parameters, calculations, (*_seen, key))
            raise ExprError(
                f"Unknown name [{node[1]}]. Wrap a field in an aggregate, e.g. SUM([{node[1]}]), "
                "or add a parameter or calculation with that name."
            )
        if kind in {"op", "cmp"}:
            return [kind, node[1], visit(node[2]), visit(node[3])]
        if kind == "neg":
            return ["neg", visit(node[1])]
        if kind == "fn":
            return ["fn", node[1], [visit(arg) for arg in node[2]]]
        return node

    resolved = visit(tree)
    if len(_walk(resolved)) > MAX_NODES * 2:
        raise ExprError("The expression is too long once its calculations are included.")
    return resolved


def aggregates(tree: list[Any]) -> list[tuple[str, str]]:
    """The distinct ``(agg, field)`` leaves, in order of appearance."""
    seen: list[tuple[str, str]] = []
    for node in _walk(tree):
        if node[0] == "agg" and (node[1], node[2]) not in seen:
            seen.append((node[1], node[2]))
    if len(seen) > MAX_AGGREGATES:
        raise ExprError(f"An expression may use at most {MAX_AGGREGATES} different aggregates.")
    return seen


NODE_KINDS = {"num", "agg", "op", "cmp", "neg", "fn"}


def evaluate(tree: list[Any], leaves: dict[tuple[str, str], Any], size: int) -> Any:
    """The expression over aggregated rows: ``leaves[(agg, field)]`` are arrays of length ``size``."""
    import numpy as np

    def value(node: Any) -> Any:
        if not isinstance(node, list) or not node or node[0] not in NODE_KINDS:
            raise ExprError("Malformed expression.")
        kind = node[0]
        if kind == "num":
            return np.full(size, float(node[1]))
        if kind == "agg":
            return np.asarray(leaves[(node[1], node[2])], dtype=float)
        if kind == "neg":
            return -value(node[1])
        if kind == "op":
            left, right = value(node[2]), value(node[3])
            if node[1] == "+":
                return left + right
            if node[1] == "-":
                return left - right
            if node[1] == "*":
                return left * right
            if node[1] == "/":
                return np.divide(left, right, out=np.full(size, np.nan), where=right != 0)
            raise ExprError("Malformed expression.")
        if kind == "cmp":
            left, right = value(node[2]), value(node[3])
            result = {
                "=": left == right,
                "<>": left != right,
                "<": left < right,
                "<=": left <= right,
                ">": left > right,
                ">=": left >= right,
            }[node[1]]
            return np.where(np.isnan(left) | np.isnan(right), np.nan, result.astype(float))
        name, args = node[1], [value(arg) for arg in node[2]]
        if name == "DIVIDE":
            alternate = args[2] if len(args) > 2 else np.full(size, np.nan)
            valid = (args[1] != 0) & ~np.isnan(args[1])
            return np.where(valid, np.divide(args[0], np.where(valid, args[1], 1.0)), alternate)
        if name == "IF":
            return np.where(np.nan_to_num(args[0]) != 0, args[1], args[2])
        if name == "AND":
            return ((np.nan_to_num(args[0]) != 0) & (np.nan_to_num(args[1]) != 0)).astype(float)
        if name == "OR":
            return ((np.nan_to_num(args[0]) != 0) | (np.nan_to_num(args[1]) != 0)).astype(float)
        if name == "NOT":
            return (np.nan_to_num(args[0]) == 0).astype(float)
        if name == "ABS":
            return np.abs(args[0])
        if name == "ROUND":
            digits = int(np.nan_to_num(args[1][0])) if len(args) > 1 and size else 0
            return np.round(args[0], max(-6, min(digits, 10)))
        if name == "SQRT":
            return np.sqrt(np.where(args[0] >= 0, args[0], np.nan))
        if name == "MIN":
            return np.fmin(args[0], args[1])
        if name == "MAX":
            return np.fmax(args[0], args[1])
        raise ExprError("Malformed expression.")

    with np.errstate(all="ignore"):
        result = value(tree)
    return np.where(np.isfinite(result), result, np.nan)
