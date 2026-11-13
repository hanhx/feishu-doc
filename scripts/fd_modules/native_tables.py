"""Parse Markdown tables and write each as a single native Docx table."""
import re
import time
from urllib.parse import urlencode

from . import doc_blocks


INITIAL_ROW_LIMIT = 9


class NativeTableError(ValueError):
    pass


def split_cells(line):
    """Keep empty cells, escaped pipes and pipes inside code spans."""
    line = line.strip().removeprefix("|")
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells, buffer, code_ticks, index = [], [], 0, 0
    while index < len(line):
        char = line[index]
        if char == "\\" and line[index + 1:index + 2] == "|":
            buffer.append("|")
            index += 2
            continue
        if char == "`":
            end = index + 1
            while end < len(line) and line[end] == "`":
                end += 1
            length = end - index
            if not code_ticks or code_ticks == length:
                code_ticks = 0 if code_ticks else length
            buffer.append(line[index:end])
            index = end
            continue
        if char == "|" and not code_ticks:
            cells.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
        index += 1
    cells.append("".join(buffer).strip())
    return cells


def parse_table(lines, start):
    if start + 1 >= len(lines) or not lines[start].lstrip().startswith("|"):
        return None
    if not re.fullmatch(r"\s*\|[\s\-:|]+\|?\s*", lines[start + 1]):
        return None
    end = start + 2
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    rows = [split_cells(lines[start])] + [split_cells(line) for line in lines[start + 2:end]]
    return {"_native_table": rows}, end


def write_table(doc_token, access_token, parent_block_id, insert_index, rows, api_call):
    columns = len(rows[0])
    rows = [(row + [""] * columns)[:columns] for row in rows]
    max_lengths = [max(len(text) for text in column) for column in zip(*rows)]
    width_total = max(sum(max_lengths), 1)
    widths = [max(100, 700 * length // width_total) for length in max_lengths]
    base = f"/docx/v1/documents/{doc_token}"
    revision = None
    table_id = None

    def call(method, suffix="", body=None, **params):
        nonlocal revision
        if revision is not None:
            params["document_revision_id"] = revision
        path = base + suffix
        if params:
            path += "?" + urlencode(params)
        response = api_call(method, path, access_token, body, retries=3 if method == "GET" else 1)
        if method != "GET" and response.get("code") in (429, 99991400):
            # Retry only an explicit rejection, keeping the same document revision.
            time.sleep(1)
            response = api_call(method, path, access_token, body, retries=1)
        if response.get("code") != 0:
            target = f"（表格块 {table_id}）" if table_id else ""
            raise NativeTableError(f"写入表格失败{target}，{method} 返回 {response.get('code')}。请回读文档核对已写入内容。")
        data = response.get("data", {})
        if method != "GET":
            revision = data.get("document_revision_id")
            if type(revision) is not int:
                raise NativeTableError("写入后未收到文档版本，请回读文档核对已写入内容。")
        return data

    revision = call("GET").get("document", {}).get("revision_id")
    if type(revision) is not int:
        raise NativeTableError("未取得文档版本，表格未写入。")

    initial_rows = min(INITIAL_ROW_LIMIT, len(rows))
    result = call("POST", f"/blocks/{parent_block_id}/children", {
        "index": insert_index,
        "children": [{"block_type": 31, "table": {"property": {
            "row_size": initial_rows, "column_size": columns, "column_width": widths, "header_row": True,
        }}}],
    })
    created = result.get("children", [])
    if len(created) != 1 or not created[0].get("block_id"):
        raise NativeTableError("未取得新表格块 ID，请回读文档核对已写入内容。")
    table_id = created[0]["block_id"]
    table = created[0].get("table", {})
    cell_ids = table.get("cells", [])
    if len(cell_ids) != initial_rows * columns:
        raise NativeTableError(f"表格块 {table_id} 初始单元格数量异常，请核对。")
    for row in range(initial_rows + 1, len(rows) + 1):
        result = call("PATCH", f"/blocks/{table_id}", {"insert_table_row": {"row_index": -1}})
        updated_cells = result.get("block", {}).get("table", {}).get("cells", [])
        if len(updated_cells) != row * columns or updated_cells[:len(cell_ids)] != cell_ids:
            raise NativeTableError(f"表格块 {table_id} 追加行后的单元格顺序异常，请核对。")
        cell_ids = updated_cells
    if len(set(cell_ids)) != len(cell_ids):
        raise NativeTableError(f"表格块 {table_id} 的单元格数量与源表不一致，请核对。")

    cells = {}
    seen_tokens = set()
    page = call("GET", f"/blocks/{table_id}/children", page_size=500)
    while True:
        cells.update((cell["block_id"], cell) for cell in page.get("items", []))
        if not page.get("has_more"):
            break
        page_token = page.get("page_token")
        if not page_token or page_token in seen_tokens:
            raise NativeTableError(f"表格块 {table_id} 的单元格分页不完整，请核对。")
        seen_tokens.add(page_token)
        page = call("GET", f"/blocks/{table_id}/children", page_size=500, page_token=page_token)
    if set(cells) != set(cell_ids) or any(len(cell.get("children", [])) > 1 for cell in cells.values()):
        raise NativeTableError(f"表格块 {table_id} 的单元格结构异常，请核对。")

    updates = []
    for index, (cell_id, text) in enumerate(zip(cell_ids, (text for row in rows for text in row))):
        make_elements = doc_blocks.make_plain_elements if index < columns else doc_blocks.make_text_elements
        elements = make_elements(text)
        children = cells[cell_id].get("children", [])
        if children:
            updates.append({"block_id": children[0], "update_text_elements": {"elements": elements}})
        else:
            call("POST", f"/blocks/{cell_id}/children", {
                "index": 0, "children": [{"block_type": 2, "text": {"elements": elements}}],
            })
    for start in range(0, len(updates), 50):
        call("PATCH", "/blocks/batch_update", {"requests": updates[start:start + 50]})
