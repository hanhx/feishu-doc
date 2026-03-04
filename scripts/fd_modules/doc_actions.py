import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor


def handle_read(doc_url, doc_type, doc_token, access_token, api_call, check_resp, block_to_md, collect_descendant_ids):
    resp = api_call("GET", f"/docx/v1/documents/{doc_token}/raw_content", access_token)
    data = check_resp(resp, "获取文档内容", auto_retry_login=True)
    content = data.get("content", "")

    items = []
    page_token = ""
    while True:
        url = f"/docx/v1/documents/{doc_token}/blocks?page_size=500"
        if page_token:
            url += f"&page_token={page_token}"
        resp2 = api_call("GET", url, access_token)
        blocks_data = resp2.get("data", {}) if resp2.get("code", -1) == 0 else {}
        items.extend(blocks_data.get("items", []))
        if not blocks_data.get("has_more", False):
            break
        page_token = blocks_data.get("page_token", "")
        if not page_token:
            break

    block_map = {it.get("block_id"): it for it in items if it.get("block_id")}
    skip_block_ids = set()
    for it in items:
        btype = it.get("block_type", 0)
        is_table = btype == 22 or (btype == 31 and isinstance(it.get("table"), dict))
        if is_table:
            table = it.get("table", {})
            for cell_id in table.get("cells", []) or []:
                skip_block_ids.add(cell_id)
                skip_block_ids.update(collect_descendant_ids(cell_id, block_map))
        if btype == 19:
            for child_id in it.get("children", []) or []:
                skip_block_ids.add(child_id)
                skip_block_ids.update(collect_descendant_ids(child_id, block_map))

    md_lines = []
    for item in items:
        block_id = item.get("block_id", "")
        if block_id and block_id in skip_block_ids:
            continue
        line = block_to_md(item, block_map)
        if line is not None:
            md_lines.append(line)

    markdown = "\n".join(md_lines)
    title = ""

    return {
        "docUrl": doc_url,
        "title": title if doc_type == "wiki" else "",
        "blockCount": len(items),
        "markdown": markdown,
        "rawContent": content,
    }


def handle_clear(doc_url, doc_token, access_token, api_call, check_resp):
    page_block_id = doc_token
    clear_resp = api_call("GET", f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}", access_token)
    clear_data = check_resp(clear_resp, "获取文档块", auto_retry_login=True)
    clear_children = clear_data.get("block", {}).get("children", [])

    api_call(
        "PATCH",
        f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}",
        access_token,
        {"update_text_elements": {"elements": [{"text_run": {"content": " "}}]}},
    )

    if not clear_children:
        return {"docUrl": doc_url, "action": "clear", "blocksDeleted": 0, "status": "success"}

    del_count = len(clear_children)
    del_resp = api_call(
        "DELETE",
        f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children/batch_delete",
        access_token,
        {"start_index": 0, "end_index": del_count},
    )
    if del_resp.get("code") != 0:
        remaining = del_count
        while remaining > 0:
            batch = min(50, remaining)
            api_call(
                "DELETE",
                f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children/batch_delete",
                access_token,
                {"start_index": 0, "end_index": batch},
            )
            remaining -= batch
            time.sleep(0.3)

    return {"docUrl": doc_url, "action": "clear", "blocksDeleted": del_count, "status": "success"}


def handle_write_append(action, doc_url, doc_token, access_token, content_file, helpers):
    if not content_file:
        print(f"❌ {action} 模式需要指定内容文件路径", file=sys.stderr)
        sys.exit(1)

    api_call = helpers["api_call"]
    check_resp = helpers["check_resp"]
    make_heading_block = helpers["make_heading_block"]
    make_code_block = helpers["make_code_block"]
    make_divider_block = helpers["make_divider_block"]
    make_todo_block = helpers["make_todo_block"]
    make_bullet_block = helpers["make_bullet_block"]
    make_ordered_block = helpers["make_ordered_block"]
    make_quote_block = helpers["make_quote_block"]
    make_text_block = helpers["make_text_block"]
    make_plain_elements = helpers["make_plain_elements"]
    make_text_elements = helpers["make_text_elements"]

    with open(content_file, "r", encoding="utf-8") as f:
        content = f.read()

    page_block_id = doc_token
    batch_size = 50
    counter = [0]

    # write 语义：覆盖更新（先清空正文，再写入新内容）
    if action == "write":
        handle_clear(doc_url, doc_token, access_token, api_call, check_resp)

    def flush_blocks(block_list):
        pending_buf = []
        for blk in block_list:
            if blk.get("_callout"):
                while pending_buf:
                    batch = pending_buf[:batch_size]
                    pending_buf = pending_buf[batch_size:]
                    resp = api_call(
                        "POST",
                        f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children",
                        access_token,
                        {"children": batch, "index": -1},
                    )
                    check_resp(resp, "写入文档", auto_retry_login=True)
                    counter[0] += len(batch)
                    time.sleep(0.5)

                cb = {"block_type": 19, "callout": {"background_color": 15}}
                cr = api_call(
                    "POST",
                    f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children",
                    access_token,
                    {"children": [cb], "index": -1},
                )
                cd = check_resp(cr, "创建引用块", auto_retry_login=True)
                counter[0] += 1

                callout_id = cd.get("children", [{}])[0].get("block_id", "")
                if callout_id:
                    get_resp = api_call("GET", f"/docx/v1/documents/{doc_token}/blocks/{callout_id}", access_token)
                    auto_children = get_resp.get("data", {}).get("block", {}).get("children", [])

                    if auto_children:
                        first_child_id = auto_children[0]
                        api_call(
                            "PATCH",
                            f"/docx/v1/documents/{doc_token}/blocks/{first_child_id}",
                            access_token,
                            {"update_text_elements": {"elements": make_text_elements(blk["_callout_text"])}} ,
                        )
                        for child_id in auto_children[1:]:
                            api_call("DELETE", f"/docx/v1/documents/{doc_token}/blocks/{child_id}", access_token)
                    else:
                        cc = {"block_type": 2, "text": {"elements": make_text_elements(blk["_callout_text"])}}
                        api_call(
                            "POST",
                            f"/docx/v1/documents/{doc_token}/blocks/{callout_id}/children",
                            access_token,
                            {"children": [cc], "index": -1},
                        )
                time.sleep(0.3)
            else:
                pending_buf.append(blk)

        while pending_buf:
            batch = pending_buf[:batch_size]
            pending_buf = pending_buf[batch_size:]
            resp = api_call(
                "POST",
                f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children",
                access_token,
                {"children": batch, "index": -1},
            )
            check_resp(resp, "写入文档", auto_retry_login=True)
            counter[0] += len(batch)
            time.sleep(0.5)

    lines = content.split("\n")
    children = []
    doc_title_set = False
    i = 0

    while i < len(lines):
        line = lines[i]

        if not doc_title_set and re.match(r"^#\s+(.+)", line) and not re.match(r"^##", line):
            title_text = re.match(r"^#\s+(.+)", line).group(1)
            if action == "write":
                api_call(
                    "PATCH",
                    f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}",
                    access_token,
                    {"update_text_elements": {"elements": [{"text_run": {"content": title_text}}]}},
                )
            else:
                children.append(make_heading_block(1, title_text))
            doc_title_set = True
            i += 1
            continue

        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            code_text = "\n".join(code_lines)
            if not lang:
                ct = code_text.strip()
                if any(k in ct for k in ["CREATE TABLE", "ALTER TABLE", "INSERT INTO", "SELECT ", "DROP TABLE"]):
                    lang = "sql"
                elif any(
                    k in ct
                    for k in [
                        "@FeignClient",
                        "public ",
                        "private ",
                        "interface ",
                        "class ",
                        "@Override",
                        "@GetMapping",
                        "@PostMapping",
                        "import ",
                    ]
                ):
                    lang = "java"
                elif ct.startswith("{") or ct.startswith("["):
                    lang = "json"
                elif any(k in ct for k in ["flowchart", "sequenceDiagram", "stateDiagram", "erDiagram", "gantt"]):
                    lang = "mermaid"
                elif any(k in ct for k in ["GET /", "POST /", "PUT /", "DELETE /"]):
                    lang = "bash"
            children.append(make_code_block(code_text, lang))
            continue

        if not line.strip():
            i += 1
            continue

        if re.match(r"^-{3,}$", line.strip()) or re.match(r"^\*{3,}$", line.strip()):
            children.append(make_divider_block())
            i += 1
            continue

        hm = re.match(r"^(#{1,9})\s+(.*)", line)
        if hm:
            level = len(hm.group(1))
            children.append(make_heading_block(level, hm.group(2)))
            i += 1
            continue

        stripped = line.lstrip()

        tm = re.match(r"^-\s*\[([ xX])\]\s*(.*)", stripped)
        if tm:
            done = tm.group(1).lower() == "x"
            children.append(make_todo_block(tm.group(2), done))
            i += 1
            continue

        if re.match(r"^[-*+]\s+", stripped):
            text = re.sub(r"^[-*+]\s+", "", stripped)
            children.append(make_bullet_block(text))
            i += 1
            continue

        om = re.match(r"^\d+\.\s+(.*)", stripped)
        if om:
            children.append(make_ordered_block(om.group(1)))
            i += 1
            continue

        if stripped.startswith("> ") or stripped == ">" or (stripped.startswith(">") and not stripped.startswith(">" * 3)):
            quote_lines = []
            while i < len(lines):
                ql = lines[i].lstrip()
                if ql.startswith("> "):
                    ql = ql[2:]
                elif ql.startswith(">"):
                    ql = ql[1:]
                else:
                    break
                quote_lines.append(ql)
                i += 1
            children.append(make_quote_block("\n".join(quote_lines)))
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s\-:|]+\|?\s*$", lines[i + 1]):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            if len(table_lines) >= 2:
                header_cells = [c.strip() for c in table_lines[0].split("|") if c.strip()]
                data_rows = []
                for row_line in table_lines[2:]:
                    cells = [c.strip() for c in row_line.split("|") if c.strip()]
                    data_rows.append(cells)
                col_size = len(header_cells)
                all_rows_for_width = [header_cells] + data_rows
                col_max_len = [0] * col_size
                for row_cells in all_rows_for_width:
                    for ci in range(min(len(row_cells), col_size)):
                        col_max_len[ci] = max(col_max_len[ci], len(row_cells[ci]))
                total_len = max(sum(col_max_len), 1)
                total_width = 700
                col_widths = [max(100, int(total_width * cl / total_len)) for cl in col_max_len]

                flush_blocks(children)
                children = []
                max_data_rows = 8

                def create_and_fill_table(h_cells, d_rows, c_size, c_widths):
                    sub_row_size = 1 + len(d_rows)
                    tb = {
                        "block_type": 31,
                        "table": {
                            "property": {
                                "row_size": sub_row_size,
                                "column_size": c_size,
                                "column_width": c_widths,
                                "header_row": True,
                            },
                        },
                    }
                    tr = api_call(
                        "POST",
                        f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children",
                        access_token,
                        {"children": [tb], "index": -1},
                    )
                    if tr.get("code", -1) != 0:
                        print(
                            f"⚠️ 表格创建失败({sub_row_size}x{c_size}), fallback: {tr.get('msg', '')[:80]}",
                            file=sys.stderr,
                        )
                        return False
                    counter[0] += 1
                    tc = tr.get("data", {}).get("children", [])
                    if tc:
                        cids = tc[0].get("table", {}).get("cells", [])
                        a_rows = [h_cells] + d_rows

                        def fill_cell(args):
                            cell_id, text, is_header = args
                            el = make_plain_elements(text) if is_header else make_text_elements(text)
                            get_resp = api_call("GET", f"/docx/v1/documents/{doc_token}/blocks/{cell_id}", access_token)
                            auto_children = get_resp.get("data", {}).get("block", {}).get("children", [])

                            if auto_children:
                                first_child_id = auto_children[0]
                                api_call(
                                    "PATCH",
                                    f"/docx/v1/documents/{doc_token}/blocks/{first_child_id}",
                                    access_token,
                                    {"update_text_elements": {"elements": el}},
                                )
                                for child_id in auto_children[1:]:
                                    api_call("DELETE", f"/docx/v1/documents/{doc_token}/blocks/{child_id}", access_token)
                            else:
                                cell_block = {"block_type": 2, "text": {"elements": el}}
                                api_call(
                                    "POST",
                                    f"/docx/v1/documents/{doc_token}/blocks/{cell_id}/children",
                                    access_token,
                                    {"children": [cell_block], "index": -1},
                                )
                            time.sleep(0.02)

                        num_workers = 5
                        tasks = []
                        for ri, rc in enumerate(a_rows):
                            for ci2 in range(c_size):
                                cidx = ri * c_size + ci2
                                if cidx >= len(cids):
                                    break
                                ct = rc[ci2] if ci2 < len(rc) else ""
                                tasks.append((cids[cidx], ct, ri == 0))

                        for batch_start in range(0, len(tasks), num_workers):
                            batch = tasks[batch_start:batch_start + num_workers]
                            with ThreadPoolExecutor(max_workers=num_workers) as pool:
                                futures = [pool.submit(fill_cell, task) for task in batch]
                                for future in futures:
                                    future.result()
                    time.sleep(0.5)
                    return True

                for chunk_start in range(0, len(data_rows), max_data_rows):
                    chunk = data_rows[chunk_start:chunk_start + max_data_rows]
                    if not create_and_fill_table(header_cells, chunk, col_size, col_widths):
                        children.append(make_code_block("\n".join(table_lines), "markdown"))
                        break
            continue

        if not line.strip():
            i += 1
            continue

        children.append(make_text_block(line))
        i += 1

    if not children and counter[0] == 0:
        print("❌ 内容为空", file=sys.stderr)
        sys.exit(1)

    flush_blocks(children)

    return {
        "docUrl": doc_url,
        "action": action,
        "blocksAdded": counter[0],
        "totalBatches": (len(children) + batch_size - 1) // batch_size,
        "status": "success",
    }
