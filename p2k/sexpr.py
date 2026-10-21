"""Minimal KiCad s-expression reader/writer (stdlib only).

A node is a Python list: [head, arg, ...]. Atoms are `Sym` (bare words and
numbers, written unquoted) or `str` (written quoted).
"""
import re


class Sym(str):
    """Unquoted atom (keyword or number)."""


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)


def parse(text):
    stack, cur = [], None
    pos = 0
    while True:
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            break
        pos = m.end()
        op, cl, qs, atom = m.groups()
        if op:
            node = []
            if cur is not None:
                cur.append(node)
                stack.append(cur)
            cur = node
        elif cl:
            if not stack:
                return cur
            cur = stack.pop()
        elif qs is not None:
            cur.append(re.sub(r'\\(.)', lambda g: {"n": "\n", "t": "\t"}.get(g.group(1), g.group(1)), qs))
        else:
            cur.append(Sym(atom))
    return cur


def load(path):
    with open(path, encoding="utf-8") as f:
        return parse(f.read())


def _atom(a):
    if isinstance(a, Sym):
        return str(a)
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, (int, float)):
        return fmt_num(a)
    s = str(a).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def fmt_num(v):
    if isinstance(v, int):
        return str(v)
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# Nodes whose children stay on one line.
_INLINE = {"at", "xy", "size", "pts", "start", "end", "mid", "center", "radius", "length",
           "width", "type", "color", "offset", "hide", "justify", "uuid", "version",
           "generator", "generator_version", "paper", "lib_id", "unit", "reference",
           "page", "number", "name", "exclude_from_sim", "in_bom", "on_board", "dnp",
           "in_pos_files", "duplicate_pin_numbers_are_jumpers", "embedded_fonts",
           "show_name", "do_not_autoplace", "extends", "power", "bold", "italic",
           "thickness", "fields_autoplaced", "body_style", "convert", "diameter"}


def dump(node, indent=0):
    tab = "\t" * indent
    if not isinstance(node, list):
        return tab + _atom(node)
    head = node[0]
    simple = all(not isinstance(c, list) for c in node[1:])
    if simple and (head in _INLINE or len(node) <= 4):
        return tab + "(" + " ".join(_atom(c) for c in node) + ")"
    if head == "pts":
        return tab + "(pts\n" + tab + "\t" + " ".join(dump(c).strip() for c in node[1:]) + "\n" + tab + ")"
    out = [tab + "(" + _atom(head)]
    lead = []
    rest = node[1:]
    while rest and not isinstance(rest[0], list):
        lead.append(_atom(rest[0]))
        rest = rest[1:]
    if lead:
        out[0] += " " + " ".join(lead)
    for c in rest:
        out.append(dump(c, indent + 1) if isinstance(c, list) else tab + "\t" + _atom(c))
    out.append(tab + ")")
    return "\n".join(out)


def save(path, node):
    with open(path, "w", encoding="utf-8") as f:
        f.write(dump(node) + "\n")


def find(node, head):
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == head:
            return c
    return None


def find_all(node, head):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == head]


def walk(node, head):
    """All descendants (and node itself) with the given head."""
    if isinstance(node, list) and node:
        if node[0] == head:
            yield node
        for c in node[1:]:
            yield from walk(c, head)
