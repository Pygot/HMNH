# tests/import_rules.py
import ast

MAX_LINE = 100


def _module(node: ast.ImportFrom) -> str:
    return "." * node.level + (node.module or "")


def _name(alias: ast.alias) -> str:
    return alias.name if alias.asname is None else f"{alias.name} as {alias.asname}"


def _leading_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    found: list[ast.Import | ast.ImportFrom] = []
    for node in tree.body:
        if not isinstance(node, ast.Import | ast.ImportFrom):
            break
        found.append(node)
    return found


def _render(imports: list[ast.Import | ast.ImportFrom]) -> str:
    multi: list[tuple[int, str, str]] = []
    single: list[tuple[int, str, str]] = []
    plain: list[tuple[int, str, str]] = []
    for node in imports:
        if isinstance(node, ast.Import):
            for alias in node.names:
                line = f"import {_name(alias)}"
                plain.append((-len(line), alias.name, line))
            continue
        module = _module(node)
        names = sorted((_name(alias) for alias in node.names), key=str.casefold)
        if len(names) > 1:
            body = "".join(f"    {name},\n" for name in names)
            multi.append((-len(names), module, f"from {module} import (\n{body})"))
        else:
            line = f"from {module} import {names[0]}"
            single.append((-len(line), module, line))
    ordered_from = [item[2] for item in sorted(multi)] + [item[2] for item in sorted(single)]
    ordered_plain = [item[2] for item in sorted(plain)]
    blocks = ["\n".join(ordered_from), "\n".join(ordered_plain)]
    return "\n\n".join(block for block in blocks if block) + "\n"


def normalize_imports(source: str) -> str:
    tree = ast.parse(source)
    imports = _leading_imports(tree)
    if not imports:
        return source
    lines = source.splitlines(keepends=True)
    start = imports[0].lineno - 1
    end = imports[-1].end_lineno or imports[-1].lineno
    return "".join(lines[:start]) + _render(imports) + "".join(lines[end:])


def misplaced_globals(source: str) -> list[str]:
    tree = ast.parse(source)
    skip = len(_leading_imports(tree))
    problems = []
    seen_code = False
    for node in tree.body[skip:]:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            if seen_code:
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                problems.append(f"line {node.lineno}: {ast.unparse(target)}")
        else:
            seen_code = True
    return problems
