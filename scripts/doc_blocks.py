import re


def extract_text(elements):
    if not elements:
        return ""
    parts = []
    for el in elements:
        if isinstance(el, dict):
            tr = el.get("text_run") or {}
            parts.append(tr.get("content", ""))
            mr = el.get("mention_user") or el.get("mention_doc") or {}
            if mr:
                parts.append(mr.get("content", ""))
    return "".join(parts)


def extract_block_text(block):
    for key in block:
        if isinstance(block[key], dict) and "elements" in block[key]:
            return extract_text(block[key].get("elements", []))
    return ""


def get_block_text_by_id(block_id, block_map, visited=None):
    if visited is None:
        visited = set()
    if not block_id or block_id in visited:
        return ""
    visited.add(block_id)

    block = block_map.get(block_id, {})
    if not block:
        return ""

    text = extract_block_text(block).strip()
    if text:
        return text

    child_texts = []
    for child_id in block.get("children", []) or []:
        child_text = get_block_text_by_id(child_id, block_map, visited)
        if child_text:
            child_texts.append(child_text)
    return "\n".join(child_texts)


def collect_descendant_ids(block_id, block_map, visited=None):
    if visited is None:
        visited = set()
    if not block_id or block_id in visited:
        return set()
    visited.add(block_id)

    block = block_map.get(block_id, {})
    descendants = set()
    for child_id in block.get("children", []) or []:
        descendants.add(child_id)
        descendants.update(collect_descendant_ids(child_id, block_map, visited))
    return descendants


def table_block_to_md(block, block_map):
    table = block.get("table", {})
    prop = table.get("property", {}) if isinstance(table, dict) else {}

    row_size = int(prop.get("row_size", 0) or 0)
    col_size = int(prop.get("column_size", 0) or 0)
    cell_ids = table.get("cells", []) if isinstance(table, dict) else []

    if row_size <= 0 or col_size <= 0 or not cell_ids:
        return "[表格]"

    row_count = min(row_size, max(1, len(cell_ids) // col_size))
    matrix = [["" for _ in range(col_size)] for _ in range(row_count)]

    total_cells = min(len(cell_ids), row_count * col_size)
    for idx in range(total_cells):
        r = idx // col_size
        c = idx % col_size
        text = get_block_text_by_id(cell_ids[idx], block_map).strip()
        text = text.replace("\n", "<br>").replace("|", "\\|")
        matrix[r][c] = text

    if not matrix:
        return "[表格]"

    header = matrix[0]
    separator = ["---"] * col_size
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for row in matrix[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def callout_block_to_md(block, block_map):
    texts = []

    children = block.get("children", []) or []
    if children:
        for child_id in children:
            child_text = get_block_text_by_id(child_id, block_map).strip()
            if child_text:
                texts.append(child_text)
    else:
        callout = block.get("callout", {})
        if isinstance(callout, dict) and callout.get("elements"):
            direct = extract_text(callout.get("elements", [])).strip()
            if direct:
                texts.append(direct)

    if not texts:
        return None

    merged = "\n".join(texts)
    return "\n".join([f"> {ln}" if ln else ">" for ln in merged.split("\n")])


def block_to_md(block, block_map=None):
    btype = block.get("block_type", 0)
    if btype == 1:
        page = block.get("page", {})
        return "# " + extract_text(page.get("elements", []))
    elif btype == 2:
        return extract_text(block.get("text", {}).get("elements", []))
    elif btype in range(3, 12):
        level = btype - 2
        key = f"heading{level}"
        return "#" * level + " " + extract_text(block.get(key, {}).get("elements", []))
    elif btype == 12:
        return "- " + extract_text(block.get("bullet", {}).get("elements", []))
    elif btype == 13:
        return "1. " + extract_text(block.get("ordered", {}).get("elements", []))
    elif btype == 14:
        code = block.get("code", {})
        lang_map = {
            0: "PlainText", 1: "ABAP", 2: "Ada", 3: "Apache", 4: "Apex", 5: "Assembly",
            6: "Bash", 7: "CSharp", 8: "CPP", 9: "C", 10: "COBOL", 11: "CSS", 12: "CoffeeScript",
            13: "D", 14: "Dart", 15: "Delphi", 16: "Django", 17: "Dockerfile", 18: "Erlang",
            19: "Fortran", 20: "FoxPro", 21: "Go", 22: "Groovy", 23: "HTML", 24: "HTMLBars",
            25: "HTTP", 26: "Haskell", 27: "JSON", 28: "Java", 29: "JavaScript", 30: "Julia",
            31: "Kotlin", 32: "LateX", 33: "Lisp", 34: "Logo", 35: "Lua", 36: "MATLAB",
            37: "Makefile", 38: "Markdown", 39: "Nginx", 40: "Objective-C", 41: "OpenEdgeABL",
            42: "PHP", 43: "Perl", 44: "PostScript", 45: "Power Shell", 46: "Prolog",
            47: "ProtoBuf", 48: "Python", 49: "R", 50: "RPG", 51: "Ruby", 52: "Rust", 53: "SAS",
            54: "SCSS", 55: "SQL", 56: "Scala", 57: "Scheme", 58: "Scratch", 59: "Shell",
            60: "Swift", 61: "Thrift", 62: "TypeScript", 63: "VBScript", 64: "Visual Basic",
            65: "XML", 66: "YAML",
        }
        lang = lang_map.get(code.get("style", {}).get("language", 0), "")
        return f"```{lang}\n{extract_text(code.get('elements', []))}\n```"
    elif btype == 15:
        return "> " + extract_text(block.get("quote_container", block.get("quote", {})).get("elements", []))
    elif btype == 17:
        todo = block.get("todo", {})
        done = todo.get("style", {}).get("done", False)
        return f"- [{'x' if done else ' '}] " + extract_text(todo.get("elements", []))
    elif btype == 23:
        return "---"
    elif btype == 22:
        if "divider" in block:
            return "---"
        return table_block_to_md(block, block_map or {})
    elif btype == 27:
        return "[图片]"
    elif btype == 31 and isinstance(block.get("table"), dict):
        return table_block_to_md(block, block_map or {})
    elif btype == 18:
        return "[多维表格]"
    elif btype == 31:
        return "[分栏]"
    elif btype == 19:
        return callout_block_to_md(block, block_map or {})
    return extract_block_text(block)


def parse_inline_styles(text):
    if not text:
        return [{"text_run": {"content": " "}}]
    elements = []
    pattern = re.compile(
        r'(\*\*(.+?)\*\*)'
        r'|(`([^`]+)`)'
        r'|(~~(.+?)~~)'
        r'|(\[([^\]]+)\]\(([^)]+)\))'
    )
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            elements.append({"text_run": {"content": text[pos:m.start()]}})
        if m.group(2):
            elements.append({"text_run": {"content": m.group(2), "text_element_style": {"bold": True}}})
        elif m.group(4):
            elements.append({"text_run": {"content": m.group(4), "text_element_style": {"inline_code": True}}})
        elif m.group(6):
            elements.append({"text_run": {"content": m.group(6), "text_element_style": {"strikethrough": True}}})
        elif m.group(8):
            link_url = m.group(9)
            if link_url.startswith("http://") or link_url.startswith("https://"):
                elements.append({"text_run": {"content": m.group(8), "text_element_style": {"link": {"url": link_url}}}})
            else:
                elements.append({"text_run": {"content": f"[{m.group(8)}]({link_url})"}})
        pos = m.end()
    if pos < len(text):
        elements.append({"text_run": {"content": text[pos:]}})
    return elements if elements else [{"text_run": {"content": " "}}]


def make_text_elements(text):
    return parse_inline_styles(text)


def make_plain_elements(text):
    return [{"text_run": {"content": text}}] if text else [{"text_run": {"content": " "}}]


def make_text_block(text):
    return {"block_type": 2, "text": {"elements": make_text_elements(text)}}


def make_heading_block(level, text):
    level = max(1, min(level, 9))
    block_type = level + 2
    key = f"heading{level}"
    elements = [{"text_run": {"content": text, "text_element_style": {"bold": True}}}]
    return {"block_type": block_type, key: {"elements": elements}}


def make_bullet_block(text):
    return {"block_type": 12, "bullet": {"elements": make_text_elements(text)}}


def make_ordered_block(text):
    return {"block_type": 13, "ordered": {"elements": make_text_elements(text)}}


def make_code_block(code_text, lang=""):
    lang_map = {
        "sql": 56,
        "java": 29,
        "javascript": 30,
        "typescript": 63,
        "python": 49,
        "go": 22,
        "bash": 7,
        "shell": 60,
        "json": 28,
        "yaml": 67,
        "xml": 66,
        "html": 24,
        "css": 11,
        "groovy": 23,
        "lua": 36,
        "markdown": 39,
        "nginx": 40,
        "php": 43,
        "c": 10,
        "cpp": 9,
        "c++": 9,
        "csharp": 8,
        "c#": 8,
        "scala": 57,
        "ruby": 52,
        "rust": 53,
        "r": 50,
        "scss": 55,
        "mermaid": 21,
        "plaintext": 21,
        "": 21,
    }
    lang_code = lang_map.get(lang.lower(), 21)
    return {
        "block_type": 14,
        "code": {
            "elements": make_plain_elements(code_text),
            "style": {"language": lang_code},
        },
    }


def make_quote_block(text):
    return {"_callout": True, "_callout_text": text}


def make_divider_block():
    return {"block_type": 22, "divider": {}}


def make_todo_block(text, done=False):
    return {
        "block_type": 17,
        "todo": {
            "elements": make_text_elements(text),
            "style": {"done": done},
        },
    }
