"""
AutoAPIs v1.1.0 - Compile .api files into Python source files.
Usage: python autoapis.py [--cache yes|no] <file.api>
"""

VERSION = "1.1.0"

import sys
import os
import re
import hashlib
import json


# ---------------------------------------------------------------------------
# Argument fixup  (name: *args -> *args, outside strings)
# ---------------------------------------------------------------------------

_STRING_RE = re.compile(
    r'(""".*?"""|\'\'\'.*?\'\'\'|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')',
    re.DOTALL,
)
_BAD_ARG_RE = re.compile(r'\b(\w+)\s*:\s*(\*\*?\w+)')


def fix_arguments(args_str: str, funcname: str) -> str:
    parts = _STRING_RE.split(args_str)
    result = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            result.append(part)
        else:
            def replacer(m, fn=funcname):
                print(f"  ⚠  Auto-converted argument: {m.group(0)!r}  ->  {m.group(2)!r}  (in function '{fn}')")
                return m.group(2)
            result.append(_BAD_ARG_RE.sub(replacer, part))
    return "".join(result)


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

def tokenize(source: str):
    for lineno, raw in enumerate(source.splitlines(), 1):
        stripped = raw.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(stripped)
        parts = stripped.split(None, 1)
        keyword = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        yield lineno, indent, keyword, rest


# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------

class Node:
    pass


class ClassNode(Node):
    def __init__(self, name, bases, description, children):
        self.name = name
        self.bases = bases            # list of str, e.g. ["Exception"] or []
        self.description = description
        self.children = children


class FunctionNode(Node):
    def __init__(self, funcname, description, decorator, arguments, body_lines):
        self.funcname = funcname
        self.description = description
        self.decorator = decorator    # single string or None
        self.arguments = arguments
        self.body_lines = body_lines  # list of (rel_indent, text)


class VariableNode(Node):
    def __init__(self, name, value_lines):
        self.name = name
        self.value_lines = value_lines  # list of (rel_indent, text)


class EnumNode(Node):
    def __init__(self, name, bases, description, fields):
        self.name = name
        self.bases = bases            # e.g. ["Enum"] or ["IntEnum"]
        self.description = description
        self.fields = fields          # list of (field_name, value_str)


class DataclassNode(Node):
    def __init__(self, name, bases, description, options, fields, post_init):
        self.name = name
        self.bases = bases
        self.description = description
        self.options = options        # dict: frozen/eq/order -> bool
        self.fields = fields          # list of (field_name, type_str, default_str|None)
        self.post_init = post_init    # list of (rel_indent, text) or []


# ---------------------------------------------------------------------------
# Valid decorator types
# ---------------------------------------------------------------------------

VALID_TYPES = {
    "staticmethod", "classmethod",
    "abstractmethod",
    "property", "property.setter", "property.deleter",
    "override",
}

# Types that need an import statement added automatically
TYPE_IMPORTS = {
    "abstractmethod": "from abc import abstractmethod",
    "override":       "from typing import override",
}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class ParseError(Exception):
    def __init__(self, lineno, msg):
        super().__init__(f"Line {lineno}: {msg}")


class Parser:
    def __init__(self, tokens):
        self._tokens = list(tokens)
        self._pos = 0
        self.required_imports = set()   # populated during parse

    def _peek(self):
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _advance(self):
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect_keyword(self, keyword):
        tok = self._peek()
        if tok is None:
            raise ParseError("EOF", f"Expected '{keyword}' but reached end of file")
        lineno, indent, kw, rest = tok
        if kw != keyword:
            raise ParseError(lineno, f"Expected '{keyword}', got '{kw}'")
        self._advance()
        return lineno, rest

    # ---- top-level ---------------------------------------------------------

    def parse(self):
        nodes = []
        while self._peek() is not None:
            lineno, indent, kw, rest = self._peek()
            if kw == "item":
                nodes.append(self._parse_item())
            else:
                raise ParseError(lineno, f"Unexpected '{kw}' at top level - expected 'item'")
        return nodes

    # ---- item dispatcher ---------------------------------------------------

    def _parse_item(self):
        lineno, indent, kw, rest = self._advance()
        if not rest:
            raise ParseError(lineno, "'item' must be followed by a type")
        t = rest.strip()
        dispatch = {
            "class":     self._parse_class,
            "function":  self._parse_function,
            "variable":  self._parse_variable,
            "enum":      self._parse_enum,
            "dataclass": self._parse_dataclass,
        }
        if t not in dispatch:
            raise ParseError(lineno, f"Unknown item type '{t}'. Expected: {', '.join(dispatch)}")
        return dispatch[t](lineno)

    # ---- helpers for nested blocks -----------------------------------------

    def _parse_nested(self, parent_name, open_lineno):
        """Parse children of a class/dataclass, supporting both 'item X' and 'ParentName X'."""
        children = []
        nested_types = {"function", "class", "variable", "enum", "dataclass"}
        while True:
            tok = self._peek()
            if tok is None:
                raise ParseError(open_lineno, f"Unclosed '{parent_name}': missing 'close'")
            lineno, indent, kw, rest = tok

            if kw == "close":
                self._advance()
                break
            elif kw == "item":
                children.append(self._parse_item())
            elif kw == parent_name and rest in nested_types:
                self._advance()
                dispatch = {
                    "class":     self._parse_class,
                    "function":  self._parse_function,
                    "variable":  self._parse_variable,
                    "enum":      self._parse_enum,
                    "dataclass": self._parse_dataclass,
                }
                children.append(dispatch[rest](lineno))
            else:
                raise ParseError(lineno,
                    f"Unexpected '{kw}' inside '{parent_name}'. "
                    f"Expected '{parent_name} function/class/variable/enum/dataclass', 'item', or 'close'")
        return children

    # ---- class -------------------------------------------------------------

    def _parse_class(self, open_lineno):
        _, name = self._expect_keyword("name")
        if not name:
            raise ParseError(open_lineno, "'name' requires a value for class")

        bases = []
        description = None

        # optional: inherit and description in any order
        header = {"inherit", "description"}
        while True:
            tok = self._peek()
            if tok is None or tok[2] not in header:
                break
            _, _, kw, rest = self._advance()
            if kw == "inherit":
                bases = [b.strip() for b in rest.split(",") if b.strip()]
            elif kw == "description":
                description = rest

        children = self._parse_nested(name, open_lineno)
        return ClassNode(name=name, bases=bases, description=description, children=children)

    # ---- function ----------------------------------------------------------

    def _parse_function(self, open_lineno):
        funcname = None
        description = None
        decorator = None
        arguments = "()"

        header_kws = {"funcname", "description", "type", "arguments"}
        while True:
            tok = self._peek()
            if tok is None:
                raise ParseError(open_lineno, "Unclosed function: missing 'close'")
            lineno, indent, kw, rest = tok
            if kw not in header_kws:
                break
            self._advance()
            if kw == "funcname":
                if not rest:
                    raise ParseError(lineno, "'funcname' requires a value")
                funcname = rest
            elif kw == "description":
                description = rest
            elif kw == "type":
                if rest not in VALID_TYPES:
                    raise ParseError(lineno,
                        f"Invalid type '{rest}'. Valid types: {', '.join(sorted(VALID_TYPES))}")
                decorator = rest
                if rest in TYPE_IMPORTS:
                    self.required_imports.add(TYPE_IMPORTS[rest])
            elif kw == "arguments":
                arguments = rest if rest else "()"
                arguments = fix_arguments(arguments, funcname or "<unknown>")

        if funcname is None:
            raise ParseError(open_lineno, "Function is missing a 'funcname'")

        base_indent = None
        body_lines = []
        while True:
            tok = self._peek()
            if tok is None:
                raise ParseError(open_lineno, f"Unclosed function '{funcname}': missing 'close'")
            lineno, indent, kw, rest = tok
            if kw == "close":
                self._advance()
                break
            elif kw == "item":
                raise ParseError(lineno, "Nested 'item' blocks are not allowed inside a function body")
            else:
                if base_indent is None:
                    base_indent = indent
                rel = max(0, indent - base_indent)
                raw = (kw + " " + rest).rstrip() if rest else kw
                body_lines.append((rel, raw))
                self._advance()

        return FunctionNode(funcname=funcname, description=description,
                            decorator=decorator, arguments=arguments, body_lines=body_lines)

    # ---- variable ----------------------------------------------------------

    def _parse_variable(self, open_lineno):
        _, name = self._expect_keyword("name")
        if not name:
            raise ParseError(open_lineno, "'name' requires a value for variable")

        tok = self._peek()
        if tok is None or tok[2] != "value":
            raise ParseError(open_lineno, f"Variable '{name}' requires a 'value' keyword")
        _, base_indent, _, first_rest = self._advance()

        value_lines = [(0, first_rest)] if first_rest else []
        while True:
            tok = self._peek()
            if tok is None:
                raise ParseError(open_lineno, f"Unclosed variable '{name}': missing 'close'")
            lineno, indent, kw, rest = tok
            if kw == "close":
                self._advance()
                break
            rel = max(0, indent - base_indent)
            raw = (kw + " " + rest).rstrip() if rest else kw
            value_lines.append((rel, raw))
            self._advance()

        return VariableNode(name=name, value_lines=value_lines)

    # ---- enum --------------------------------------------------------------

    def _parse_enum(self, open_lineno):
        _, name = self._expect_keyword("name")
        if not name:
            raise ParseError(open_lineno, "'name' requires a value for enum")

        # optional: inherit (default Enum), description
        bases = ["Enum"]
        description = None
        header = {"inherit", "description"}
        while True:
            tok = self._peek()
            if tok is None or tok[2] not in header:
                break
            _, _, kw, rest = self._advance()
            if kw == "inherit":
                bases = [b.strip() for b in rest.split(",") if b.strip()]
            elif kw == "description":
                description = rest

        self.required_imports.add("from enum import Enum")

        fields = []
        while True:
            tok = self._peek()
            if tok is None:
                raise ParseError(open_lineno, f"Unclosed enum '{name}': missing 'close'")
            lineno, indent, kw, rest = tok
            if kw == "close":
                self._advance()
                break
            elif kw == "field":
                if not rest:
                    raise ParseError(lineno, "'field' requires NAME = value")
                # rest is like  "RED = 1"  or  "GREEN = 'green'"
                if "=" not in rest:
                    raise ParseError(lineno, f"enum field must be 'field NAME = value', got: {rest!r}")
                fname, _, fval = rest.partition("=")
                fields.append((fname.strip(), fval.strip()))
                self._advance()
            else:
                raise ParseError(lineno, f"Expected 'field' or 'close' inside enum '{name}', got '{kw}'")

        return EnumNode(name=name, bases=bases, description=description, fields=fields)

    # ---- dataclass ---------------------------------------------------------

    def _parse_dataclass(self, open_lineno):
        _, name = self._expect_keyword("name")
        if not name:
            raise ParseError(open_lineno, "'name' requires a value for dataclass")

        bases = []
        description = None
        options = {"frozen": False, "eq": True, "order": False}
        fields = []
        post_init_lines = []

        header = {"inherit", "description", "frozen", "eq", "order"}
        while True:
            tok = self._peek()
            if tok is None or tok[2] not in header:
                break
            _, _, kw, rest = self._advance()
            if kw == "inherit":
                bases = [b.strip() for b in rest.split(",") if b.strip()]
            elif kw == "description":
                description = rest
            elif kw in ("frozen", "eq", "order"):
                val = rest.strip().lower()
                if val not in ("yes", "no", "true", "false"):
                    raise ParseError(open_lineno, f"'{kw}' must be yes/no, got '{rest}'")
                options[kw] = val in ("yes", "true")

        self.required_imports.add("from dataclasses import dataclass, field")

        # fields and optional post_init block
        while True:
            tok = self._peek()
            if tok is None:
                raise ParseError(open_lineno, f"Unclosed dataclass '{name}': missing 'close'")
            lineno, indent, kw, rest = tok

            if kw == "close":
                self._advance()
                break
            elif kw == "field":
                # formats:
                #   field x: int
                #   field x: int = 0
                #   field x: int = field(default_factory=list)
                if not rest:
                    raise ParseError(lineno, "'field' requires at least NAME: TYPE")
                self._advance()
                # parse  name: type  or  name: type = default
                m = re.match(r'(\w+)\s*:\s*(.+)', rest)
                if not m:
                    raise ParseError(lineno, f"Invalid field syntax: {rest!r}. Expected 'NAME: TYPE' or 'NAME: TYPE = default'")
                fname = m.group(1)
                type_and_default = m.group(2)
                if "=" in type_and_default:
                    tidx = type_and_default.index("=")
                    ftype = type_and_default[:tidx].strip()
                    fdefault = type_and_default[tidx+1:].strip()
                else:
                    ftype = type_and_default.strip()
                    fdefault = None
                fields.append((fname, ftype, fdefault))
            elif kw == "post_init":
                # collect body until 'end_post_init'
                self._advance()
                base_indent = None
                while True:
                    tok = self._peek()
                    if tok is None:
                        raise ParseError(lineno, "Unclosed 'post_init': missing 'end_post_init'")
                    li, ind, k, r = tok
                    if k == "end_post_init":
                        self._advance()
                        break
                    if base_indent is None:
                        base_indent = ind
                    rel = max(0, ind - base_indent)
                    raw = (k + " " + r).rstrip() if r else k
                    post_init_lines.append((rel, raw))
                    self._advance()
            else:
                raise ParseError(lineno,
                    f"Expected 'field', 'post_init', or 'close' inside dataclass '{name}', got '{kw}'")

        return DataclassNode(name=name, bases=bases, description=description,
                             options=options, fields=fields, post_init=post_init_lines)


# ---------------------------------------------------------------------------
# Code generator
# ---------------------------------------------------------------------------

INDENT = "    "

def _ind(level):
    return INDENT * level

def _docstring(text, level):
    return f'{_ind(level)}"""{text}"""'

def generate(nodes, indent_level=0):
    parts = []
    for node in nodes:
        if   isinstance(node, ClassNode):     parts.append(_gen_class(node, indent_level))
        elif isinstance(node, FunctionNode):  parts.append(_gen_function(node, indent_level))
        elif isinstance(node, VariableNode):  parts.append(_gen_variable(node, indent_level))
        elif isinstance(node, EnumNode):      parts.append(_gen_enum(node, indent_level))
        elif isinstance(node, DataclassNode): parts.append(_gen_dataclass(node, indent_level))
    return "\n\n".join(parts)


def _bases_str(bases):
    return f"({', '.join(bases)})" if bases else ""


def _gen_class(node: ClassNode, level: int):
    ind = _ind(level)
    lines = [f"{ind}class {node.name}{_bases_str(node.bases)}:"]
    if node.description:
        lines.append(_docstring(node.description, level + 1))
    if node.children:
        child_parts = []
        for child in node.children:
            if   isinstance(child, ClassNode):     child_parts.append(_gen_class(child, level + 1))
            elif isinstance(child, FunctionNode):  child_parts.append(_gen_function(child, level + 1))
            elif isinstance(child, VariableNode):  child_parts.append(_gen_variable(child, level + 1))
            elif isinstance(child, EnumNode):      child_parts.append(_gen_enum(child, level + 1))
            elif isinstance(child, DataclassNode): child_parts.append(_gen_dataclass(child, level + 1))
        lines.append("\n\n".join(child_parts))
    else:
        lines.append(f"{_ind(level + 1)}pass")
    return "\n".join(lines)


def _gen_function(node: FunctionNode, level: int):
    ind = _ind(level)
    lines = []
    if node.decorator:
        lines.append(f"{ind}@{node.decorator}")
    lines.append(f"{ind}def {node.funcname}{node.arguments}:")
    if node.description:
        lines.append(_docstring(node.description, level + 1))
    if node.body_lines:
        for rel, raw in node.body_lines:
            lines.append(f"{_ind(level + 1)}{' ' * rel}{raw}")
    else:
        lines.append(f"{_ind(level + 1)}pass")
    return "\n".join(lines)


def _gen_variable(node: VariableNode, level: int):
    ind = _ind(level)
    if not node.value_lines:
        return f"{ind}{node.name} = None"
    if len(node.value_lines) == 1:
        return f"{ind}{node.name} = {node.value_lines[0][1]}"
    first = node.value_lines[0][1]
    rest = "\n".join(f"{ind}{' ' * rel}{raw}" for rel, raw in node.value_lines[1:])
    return f"{ind}{node.name} = {first}\n{rest}"


def _gen_enum(node: EnumNode, level: int):
    ind = _ind(level)
    lines = [f"{ind}class {node.name}{_bases_str(node.bases)}:"]
    if node.description:
        lines.append(_docstring(node.description, level + 1))
    if node.fields:
        for fname, fval in node.fields:
            lines.append(f"{_ind(level + 1)}{fname} = {fval}")
    else:
        lines.append(f"{_ind(level + 1)}pass")
    return "\n".join(lines)


def _gen_dataclass(node: DataclassNode, level: int):
    ind = _ind(level)
    opts = []
    if node.options.get("frozen"):  opts.append("frozen=True")
    if not node.options.get("eq", True): opts.append("eq=False")
    if node.options.get("order"):   opts.append("order=True")
    decorator = f"@dataclass({', '.join(opts)})" if opts else "@dataclass"

    lines = [f"{ind}{decorator}"]
    base_str = _bases_str(node.bases)
    lines.append(f"{ind}class {node.name}{base_str}:")
    if node.description:
        lines.append(_docstring(node.description, level + 1))
    if node.fields:
        for fname, ftype, fdefault in node.fields:
            if fdefault is not None:
                lines.append(f"{_ind(level + 1)}{fname}: {ftype} = {fdefault}")
            else:
                lines.append(f"{_ind(level + 1)}{fname}: {ftype}")
    if node.post_init:
        lines.append("")
        lines.append(f"{_ind(level + 1)}def __post_init__(self):")
        for rel, raw in node.post_init:
            lines.append(f"{_ind(level + 2)}{' ' * rel}{raw}")
    if not node.fields and not node.post_init:
        lines.append(f"{_ind(level + 1)}pass")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

CACHE_DIR = "autoapi_cache"


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _cache_meta_path(input_path: str) -> str:
    base = os.path.splitext(os.path.basename(input_path))[0]
    return os.path.join(CACHE_DIR, f"{base}.meta.json")


def _cache_py_path(input_path: str) -> str:
    base = os.path.splitext(os.path.basename(input_path))[0]
    return os.path.join(CACHE_DIR, f"{base}.py")


def _is_cached(input_path: str) -> bool:
    meta_path = _cache_meta_path(input_path)
    if not os.path.exists(meta_path):
        return False
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        return meta.get("hash") == _file_hash(input_path)
    except Exception:
        return False


def _write_cache(input_path: str, output_code: str):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_cache_py_path(input_path), "w", encoding="utf-8") as f:
        f.write(output_code)
    with open(_cache_meta_path(input_path), "w") as f:
        json.dump({"hash": _file_hash(input_path)}, f)


def _read_cache(input_path: str) -> str:
    with open(_cache_py_path(input_path), "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def compile_api(input_path: str, use_cache: bool = False):
    if not os.path.isfile(input_path):
        print(f"Error: File '{input_path}' not found.")
        sys.exit(1)

    if not input_path.endswith(".api"):
        print(f"Warning: '{input_path}' does not have a .api extension.")

    # Cache hit?
    if use_cache and _is_cached(input_path):
        output_path = os.path.splitext(input_path)[0] + ".py"
        cached_code = _read_cache(input_path)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(cached_code)
        print(f"✓ Cache hit - '{input_path}' unchanged, restored '{output_path}'")
        return

    with open(input_path, "r", encoding="utf-8") as f:
        source = f.read()

    tokens = list(tokenize(source))

    try:
        parser = Parser(tokens)
        ast = parser.parse()
    except ParseError as e:
        print(f"Parse error: {e}")
        sys.exit(1)

    output_code_body = generate(ast)

    # Build final file content
    lines = [f"# Generated by AutoAPI v{VERSION}"]
    if parser.required_imports:
        lines.append("")
        for imp in sorted(parser.required_imports):
            lines.append(imp)
    lines.append("")
    lines.append(output_code_body)
    lines.append("")
    full_output = "\n".join(lines)

    output_path = os.path.splitext(input_path)[0] + ".py"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_output)

    if use_cache:
        _write_cache(input_path, full_output)

    print(f"✓ Compiled '{input_path}' -> '{output_path}'")


if __name__ == "__main__":
    args = sys.argv[1:]
    use_cache = False

    # Parse --cache yes/no
    if "--cache" in args:
        idx = args.index("--cache")
        if idx + 1 >= len(args):
            print("Error: --cache requires a value (yes or no)")
            sys.exit(1)
        val = args[idx + 1].lower()
        if val not in ("yes", "no"):
            print("Error: --cache must be 'yes' or 'no'")
            sys.exit(1)
        use_cache = val == "yes"
        args = args[:idx] + args[idx + 2:]

    if len(args) != 1:
        print(f"AutoAPIs v{VERSION}")
        print("Usage: python autoapis.py [--cache yes|no] <file.api>")
        sys.exit(1)

    compile_api(args[0], use_cache=use_cache)