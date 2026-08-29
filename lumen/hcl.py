"""
Minimal HCL-like config parser/serializer.

Supports the small subset of HCL Lumen actually needs: comments
(# // and /* */), string/number/bool values, and one level (or more)
of nested `name { ... }` blocks. It is intentionally not a full HCL2
implementation -- just enough to keep config.hcl human-editable
without pulling in a heavier dependency.
"""


def _tokenize(text):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "#" or text[i:i + 2] == "//":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text[i:i + 2] == "/*":
            end = text.find("*/", i + 2)
            i = end + 2 if end != -1 else n
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            tokens.append(("STRING", "".join(buf)))
            i = j + 1
            continue
        if c in "{}=":
            tokens.append((c, c))
            i += 1
            continue
        if c == "-" or c.isdigit():
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            tokens.append(("NUMBER", text[i:j]))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_-"):
                j += 1
            word = text[i:j]
            if word in ("true", "false"):
                tokens.append(("BOOL", word))
            else:
                tokens.append(("IDENT", word))
            i = j
            continue
        # unrecognized character, skip it
        i += 1
    return tokens


def loads(text):
    """Parse HCL-ish text into a plain dict (nested dicts for blocks)."""
    tokens = _tokenize(text)
    pos = [0]

    def parse_block():
        result = {}
        while pos[0] < len(tokens):
            ttype, tval = tokens[pos[0]]
            if ttype == "}":
                pos[0] += 1
                return result
            if ttype != "IDENT":
                pos[0] += 1
                continue
            key = tval
            pos[0] += 1
            if pos[0] >= len(tokens):
                break
            ntype, _ = tokens[pos[0]]
            if ntype == "=":
                pos[0] += 1
                if pos[0] >= len(tokens):
                    break
                vtype, vval = tokens[pos[0]]
                pos[0] += 1
                if vtype == "STRING":
                    value = vval
                elif vtype == "NUMBER":
                    value = float(vval) if "." in vval else int(vval)
                elif vtype == "BOOL":
                    value = vval == "true"
                else:
                    value = None
                result[key] = value
            elif ntype == "{":
                pos[0] += 1
                result[key] = parse_block()
        return result

    return parse_block()


def _dump_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def dumps(data, indent=0):
    """Serialize a plain dict (nested dicts become blocks) to HCL-ish text."""
    lines = []
    pad = "  " * indent
    for k, v in data.items():
        if isinstance(v, dict):
            lines.append(f"{pad}{k} {{")
            lines.append(dumps(v, indent + 1))
            lines.append(f"{pad}}}")
        else:
            lines.append(f"{pad}{k} = {_dump_value(v)}")
    return "\n".join(lines)
