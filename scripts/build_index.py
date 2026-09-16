#!/usr/bin/env python3
"""Generate a map of this repository so an agent (or a human) does not have to read it all.

Two artefacts, both regenerated from the source with ``ast`` - no hand editing, no
drift:

* ``docs/CODE_MAP.md``  - module -> symbols -> one-line purpose, plus the CLI surface
* ``docs/index.json``  - the same data, machine readable

Usage::

    python scripts/build_index.py            # rewrite the artefacts
    python scripts/build_index.py --check    # exit 1 if they are stale (CI)

The point is cost: reading six thousand lines to find one function is waste. The map
fits in a few hundred lines and every entry carries a line number.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "hermes_update_check"
TESTS = ROOT / "tests"
MAP_MD = ROOT / "docs" / "CODE_MAP.md"
MAP_JSON = ROOT / "docs" / "index.json"

#: symbols that are noise in a map (dunder plumbing, tiny helpers)
SKIP_PREFIXES = ("_parse_docstring",)


@dataclass
class Symbol:
    kind: str  # class | function | constant
    name: str
    line: int
    summary: str = ""
    signature: str = ""
    members: list[str] = field(default_factory=list)


@dataclass
class Module:
    name: str  # relative module path, e.g. "notify.telegram"
    path: str  # repo-relative file path
    lines: int
    summary: str
    symbols: list[Symbol] = field(default_factory=list)


def first_line(text: str | None) -> str:
    """First sentence of a docstring, collapsed to one line."""
    if not text:
        return ""
    body = " ".join(part.strip() for part in text.strip().splitlines() if part.strip())
    if not body:
        return ""
    match = re.split(r"(?<=[.!?])\s+", body, maxsplit=1)
    line = match[0].rstrip(".")
    return (line[:150] + "…") if len(line) > 150 else line


def render_arg(node: ast.arg, default: ast.expr | None) -> str:
    name = node.arg
    if node.annotation is not None:
        name += f": {ast.unparse(node.annotation)}"
    if default is not None:
        name += " = …"
    return name


def render_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args
    parts = [render_arg(a, None) for a in args.posonlyargs]
    if args.posonlyargs:
        parts.append("/")
    defaults_offset = len(args.args) - len(args.defaults)
    for index, arg in enumerate(args.args):
        default = args.defaults[index - defaults_offset] if index >= defaults_offset else None
        parts.append(render_arg(arg, default))
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parts.append(render_arg(arg, default))
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"({', '.join(parts)}){returns}"


def scan_module(path: Path, root: Path) -> Module:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    rel = path.relative_to(root)
    parts = rel.parts
    module_name = (
        ".".join((*parts[1:-1], parts[-1][:-3]))
        if parts[0] == "hermes_update_check"
        else ".".join((*parts[:-1], parts[-1][:-3]))
    )
    if module_name.endswith(".__init__"):
        module_name = module_name[: -len(".__init__")]

    module = Module(
        name=module_name,
        path=str(rel).replace("\\", "/"),
        lines=len(source.splitlines()),
        summary=first_line(ast.get_docstring(tree)),
    )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_parse_docstring"):
            if node.name.startswith("_") and not node.name.startswith("__"):
                continue  # private helpers are noise in a map
            module.symbols.append(
                Symbol(
                    kind="function",
                    name=node.name,
                    line=node.lineno,
                    summary=first_line(ast.get_docstring(node)),
                    signature=render_signature(node),
                )
            )
        elif isinstance(node, ast.ClassDef):
            members: list[str] = []
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not child.name.startswith("_"):
                    members.append(child.name)
            module.symbols.append(
                Symbol(
                    kind="class",
                    name=node.name,
                    line=node.lineno,
                    summary=first_line(ast.get_docstring(node)),
                    members=members,
                )
            )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.isupper()
                    and isinstance(node.value, (ast.Constant, ast.Dict, ast.Set, ast.List))
                ):
                    value = ast.unparse(node.value)
                    module.symbols.append(
                        Symbol(
                            kind="constant",
                            name=target.id,
                            line=node.lineno,
                            summary=(value[:70] + "…") if len(value) > 70 else value,
                        )
                    )
    return module


def scan_tests() -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for path in sorted(TESTS.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        ]
        files.append(
            {
                "path": f"tests/{path.name}",
                "lines": len(source.splitlines()),
                "summary": first_line(ast.get_docstring(tree)),
                "tests": len(names),
                "names": names,
            }
        )
    return files


def scan_cli() -> list[dict[str, str]]:
    """Extract the command surface straight out of build_parser()."""
    tree = ast.parse((SRC / "cli.py").read_text(encoding="utf-8"))
    commands: list[dict[str, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"add_parser", "add_argument"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.func.attr == "add_parser"
        ):
            help_text = ""
            for keyword in node.keywords:
                if keyword.arg == "help" and isinstance(keyword.value, ast.Constant):
                    help_text = str(keyword.value.value)
            commands.append({"command": str(node.args[0].value), "help": help_text})
    return commands


def build() -> tuple[str, dict[str, object]]:
    modules = [scan_module(path, ROOT / "src") for path in sorted(SRC.rglob("*.py"))]
    tests = scan_tests()
    commands = scan_cli()

    total_lines = sum(m.lines for m in modules)
    test_total = sum(int(t["tests"]) for t in tests)

    lines: list[str] = [
        "# Code map (generated - do not edit by hand)",
        "",
        "Read this before opening files: it is regenerated from the source by",
        "`python scripts/build_index.py` and CI fails when it is stale.",
        "",
        "## Quick facts",
        "",
        f"- package: `hermes_update_check` - {len(modules)} modules, {total_lines} lines",
        f"- tests: {test_total} test functions in {len(tests)} files (offline, no network)",
        "- docs: `README.md` (user guide), `SECURITY.md` (privacy policy), `CHANGELOG.md`",
        "- invariants: never updates Hermes without an explicit `y`; unknown data is reported as",
        "  UNKNOWN, never as safe; exit codes are a public contract (see `errors.py`)",
        "",
        "## Entry points",
        "",
        "| entry | where |",
        "|---|---|",
        "| `hermes-update-check` console script | `cli.py:main` |",
        "| `python -m hermes_update_check` | `__main__.py` |",
        "| one-click installers | `install.sh`, `install.ps1` |",
        "| secret/privacy scan | `scripts/scan_secrets.py` |",
        "",
        "## CLI surface (parsed from `cli.py`)",
        "",
        "| command | help |",
        "|---|---|",
    ]
    for entry in commands:
        lines.append(f"| `{entry['command']}` | {entry['help']} |")

    lines += ["", "## Modules", "", "| module | lines | what it is for |", "|---|---|---|"]
    for module in sorted(modules, key=lambda m: (-m.lines, m.name)):
        lines.append(f"| [`{module.name}`](../{module.path}) | {module.lines} | {module.summary} |")

    for module in sorted(modules, key=lambda m: m.name):
        lines += [
            "",
            f"### `{module.name}` — {module.summary or '(no docstring)'}",
            "",
            f"`{module.path}` ({module.lines} lines)",
            "",
        ]
        if not module.symbols:
            lines.append("_no public symbols_")
            continue
        lines += ["| kind | symbol | line | purpose |", "|---|---|---|---|"]
        for symbol in sorted(module.symbols, key=lambda s: s.line):
            name = symbol.name
            if symbol.kind == "function":
                name = f"`{symbol.name}{symbol.signature}`"
            elif symbol.kind == "class":
                members = f" — {', '.join(symbol.members[:6])}" if symbol.members else ""
                name = f"**{symbol.name}**{members}"
            else:
                name = f"`{symbol.name}`"
            lines.append(f"| {symbol.kind} | {name} | {symbol.line} | {symbol.summary} |")

    lines += ["", "## Tests", "", "| file | tests | lines | focus |", "|---|---|---|---|"]
    for entry in tests:
        lines.append(
            f"| [`{entry['path']}`](../{entry['path']}) | {entry['tests']} | {entry['lines']} | {entry['summary']} |"
        )

    lines.append("")

    payload: dict[str, object] = {
        "generated_from": "scripts/build_index.py",
        "package": "hermes_update_check",
        "modules": [
            {
                "name": m.name,
                "path": m.path,
                "lines": m.lines,
                "summary": m.summary,
                "symbols": [
                    {
                        "kind": s.kind,
                        "name": s.name,
                        "line": s.line,
                        "summary": s.summary,
                        "signature": s.signature,
                        "members": s.members,
                    }
                    for s in m.symbols
                ],
            }
            for m in modules
        ],
        "cli": commands,
        "tests": tests,
        "totals": {"modules": len(modules), "lines": total_lines, "tests": test_total},
    }
    return "\n".join(lines), payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate docs/CODE_MAP.md and docs/index.json.")
    parser.add_argument("--check", action="store_true", help="verify the artefacts are up to date")
    args = parser.parse_args(argv)

    markdown, payload = build()
    json_text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"

    if args.check:
        stale: list[str] = []
        if not MAP_MD.exists() or MAP_MD.read_text(encoding="utf-8") != markdown:
            stale.append(str(MAP_MD.relative_to(ROOT)))
        if not MAP_JSON.exists() or MAP_JSON.read_text(encoding="utf-8") != json_text:
            stale.append(str(MAP_JSON.relative_to(ROOT)))
        if stale:
            print("index is stale: " + ", ".join(stale), file=sys.stderr)
            print("run: python scripts/build_index.py", file=sys.stderr)
            return 1
        print("index is up to date")
        return 0

    MAP_MD.parent.mkdir(parents=True, exist_ok=True)
    MAP_MD.write_text(markdown, encoding="utf-8")
    MAP_JSON.write_text(json_text, encoding="utf-8")
    print(f"wrote {MAP_MD.relative_to(ROOT)} ({len(markdown.splitlines())} lines)")
    print(f"wrote {MAP_JSON.relative_to(ROOT)} ({len(json_text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
