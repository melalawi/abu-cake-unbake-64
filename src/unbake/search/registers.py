"""Conservative source lifetime mutations ranked by allocator evidence."""

import math
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

from unbake.config import Held
from unbake.decomp.explain import Allocation, leverage
from unbake.search.core import Context, Mutation
from unbake.work.compare import Compared


@dataclass(frozen=True)
class Local:
    name: str
    type: str
    start: int
    end: int
    uses: tuple[int, ...]


_TOKEN = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|^\s*#[^\n]*', re.S | re.M)
_DECL = re.compile(
    r"(?m)^[ \t]*((?:(?:unsigned|signed|long|short)\s+)*"
    r"(?:int|char|float|double|[A-Za-z_]\w*)(?:[ \t]*\*)*)[ \t]+([A-Za-z_]\w*)[ \t]*;[ \t]*(?:\n|$)"
)


def _code(source: str) -> str:
    return _TOKEN.sub(lambda m: "".join("\n" if c == "\n" else " " for c in m[0]), source)


def _locals(code: str) -> list[Local]:
    depth = 0
    depths = []
    for char in code:
        depths.append(depth)
        depth += (char == "{") - (char == "}")
    rows = []
    for match in _DECL.finditer(code):
        type_name, name = match.groups()
        if type_name in ("return", "goto", "break", "continue") or depths[match.start()] != 1:
            continue
        occurrences = tuple(m.start() for m in re.finditer(r"\b" + re.escape(name) + r"\b", code))
        declaration_name = match.start(2)
        if any(pos < declaration_name for pos in occurrences):
            continue
        uses = tuple(pos for pos in occurrences if pos >= match.end())
        if not uses or re.search(r"&\s*\b" + re.escape(name) + r"\b", code):
            continue
        if any(code[max(0, pos - 2) : pos].rstrip().endswith((".", "->")) for pos in uses):
            continue
        if len(re.findall(r"\b" + re.escape(name) + r"\s*(?:;|=)", code[: match.end()])) != 1:
            continue
        rows.append(Local(name, type_name.strip(), match.start(), match.end(), uses))
    return rows


def _rename(source: str, code: str, start: int, end: int, old: str, new: str) -> str:
    matches = list(re.finditer(r"\b" + re.escape(old) + r"\b", code[start:end]))
    result = source
    for match in reversed(matches):
        a, b = start + match.start(), start + match.end()
        result = result[:a] + new + result[b:]
    return result


def _definitions(local: Local, code: str) -> list[int]:
    definitions = []
    for pos in local.uses:
        tail = code[pos:]
        match = re.match(re.escape(local.name) + r"\s*=(?!=)([^;]*);", tail)
        if match and not re.search(r"\b" + re.escape(local.name) + r"\b", match[1]):
            # Only a whole expression statement can begin a fresh lifetime.
            prefix = code[:pos].rstrip()
            if prefix.endswith((";", "{", "}")):
                definitions.append(pos)
    return definitions


def propose(source: str, trial: Compared, ctx: Context) -> Iterator[Mutation]:
    """Yield reorder, split, recycle, merge and scope mutations for one C body.

    ctx.allocation must describe this trial. Transformations that recycle or
    split storage are limited to straight-line bodies with no address escapes.
    """
    if not isinstance(source, str) or not source.strip():
        raise Held("search", "source: missing nonempty C text")
    if ctx is None or not hasattr(ctx, "allocation") or not isinstance(ctx.allocation, Allocation):
        raise Held("search", "context.allocation: missing Allocation")
    if trial is None:
        raise Held("search", "trial: missing value")
    deadline = getattr(ctx, "deadline", None)
    if type(deadline) not in (int, float) or not math.isfinite(cast(float, deadline)):
        raise Held("search", "context.deadline: finite monotonic time required")
    deadline = cast(float, deadline)
    if time.monotonic() >= deadline:
        return
    code = _code(source)
    if code.count("{") != 1 or code.count("}") != 1:
        return
    locals_ = _locals(code)
    order = {p.name: i for i, p in enumerate(leverage(ctx.allocation))}
    locals_.sort(key=lambda local: (order.get(local.name, len(order)), local.start))
    seen = {source}

    def mutation(kind: str, description: str, changed: str) -> Mutation | None:
        if changed not in seen:
            seen.add(changed)
            return Mutation(kind, description, changed)
        return None

    for left in locals_:
        if time.monotonic() >= deadline:
            return
        for right in locals_:
            if left.start >= right.start:
                continue
            gap = code[left.end : right.start]
            if not gap.strip():
                changed = (
                    source[: left.start]
                    + source[right.start : right.end]
                    + source[left.end : right.start]
                    + source[left.start : left.end]
                    + source[right.end :]
                )
                item = mutation("registers.reorder", f"reorder declarations {left.name}, {right.name}", changed)
                if item:
                    yield item
    if re.search(r"\b(?:if|else|for|while|do|switch|case|goto|setjmp|longjmp)\b|\?|&&|\|\||(?<!:):(?!:)", code):
        return
    body_end = code.rfind("}")
    for local in locals_:
        if time.monotonic() >= deadline:
            return
        definitions = _definitions(local, code)
        for position in definitions[1:]:
            new_name = local.name + "_tail"
            if re.search(r"\b" + new_name + r"\b", code):
                continue
            changed = _rename(source, code, position, body_end, local.name, new_name)
            indent_match = re.match(r"[ \t]*", source[local.start :])
            assert indent_match is not None
            indent = indent_match[0]
            changed = changed[: local.end] + indent + local.type + " " + new_name + ";\n" + changed[local.end :]
            item = mutation("registers.split", f"split reused local {local.name}", changed)
            if item:
                yield item
        if definitions and local.uses[0] == definitions[0]:
            first_statement = code.rfind(";", 0, definitions[0]) + 1
            last_statement = code.find(";", local.uses[-1]) + 1
            # A scope must not hide another local's declaration or return early.
            segment = code[first_statement:last_statement]
            if (
                first_statement >= local.end
                and not re.search(r"\breturn\b", segment)
                and not any(first_statement <= other.start < last_statement for other in locals_)
            ):
                declaration = source[local.start : local.end]
                changed = source[:last_statement] + "\n}" + source[last_statement:]
                changed = changed[:first_statement] + "\n{\n" + declaration + changed[first_statement:]
                changed = changed[: local.start] + changed[local.end :]
                item = mutation("registers.group", f"scope temporary {local.name}", changed)
                if item:
                    yield item
        for dead in locals_:
            if dead.name == local.name or dead.type != local.type or not definitions or definitions[0] != local.uses[0]:
                continue
            first = local.uses[0]
            copy = re.match(re.escape(local.name) + r"\s*=\s*" + re.escape(dead.name) + r"\s*;", code[first:])
            if dead.uses[-1] >= first and not (copy and dead.uses[-1] < first + copy.end()):
                continue
            # Keep both declarations in the same initial declaration group.
            first_use = min(p.uses[0] for p in locals_)
            if local.end > first_use or dead.end > first_use:
                continue
            changed = _rename(source, code, first, body_end, local.name, dead.name)
            if copy:
                new_copy = re.match(
                    re.escape(dead.name) + r"\s*=\s*" + re.escape(dead.name) + r"\s*;", _code(changed)[first:]
                )
                assert new_copy is not None
                changed = changed[:first] + changed[first + new_copy.end() :]
            changed = changed[: local.start] + changed[local.end :]
            item = mutation(
                "registers.merge" if copy else "registers.recycle",
                f"{'merge' if copy else 'recycle'} {local.name} into dead local {dead.name}",
                changed,
            )
            if item:
                yield item
