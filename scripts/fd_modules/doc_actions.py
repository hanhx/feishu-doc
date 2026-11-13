import json
import re
import sys
import time
from . import doc_blocks, native_tables, targeted_ops


ASSET_TYPES = {18: "bitable", 23: "file", 27: "image", 30: "sheet", 43: "whiteboard"}


def handle_read(doc_url, doc_type, doc_token, access_token, api_call, check_resp, block_to_md, collect_descendant_ids):
    if doc_type == "docs":
        print("❌ 暂不支持旧版 docs/doccn 文档", file=sys.stderr)
        print("", file=sys.stderr)
        print("   请将文档转为新版 docx 后再读取。", file=sys.stderr)
        print("   旧版链接示例: https://xxx.feishu.cn/docs/doccn...", file=sys.stderr)
        print("   新版链接示例: https://xxx.feishu.cn/docx/... 或 https://xxx.feishu.cn/wiki/...", file=sys.stderr)
        sys.exit(1)

    action_name = "获取 wiki 文档内容" if doc_type == "wiki" else "获取文档内容"
    resp = api_call("GET", f"/docx/v1/documents/{doc_token}/raw_content", access_token)
    data = check_resp(resp, action_name, auto_retry_login=True)
    content = data.get("content", "")

    items, block_map = targeted_ops.fetch_all_blocks(doc_token, access_token, api_call, check_resp)
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
    assets = []
    headings = []
    for block_index, item in enumerate(items, start=1):
        block_id = item.get("block_id", "")
        block_type = item.get("block_type")
        asset_type = ASSET_TYPES.get(block_type)
        if asset_type and block_id and "divider" not in item:
            asset = {
                "type": asset_type,
                "blockId": block_id,
                "blockIndex": block_index,
                "sectionPath": [title for _, title in headings],
            }
            file_data = item.get("file")
            if asset_type == "file" and isinstance(file_data, dict):
                name = file_data.get("name")
                if isinstance(name, str) and name.strip():
                    asset["name"] = name
                    if name.strip().lower().endswith((".xlsx", ".xls")):
                        asset["type"] = "excel"
            assets.append(asset)
        if block_id and block_id in skip_block_ids:
            continue
        level = targeted_ops.heading_level(item)
        if level is not None:
            while headings and headings[-1][0] >= level:
                headings.pop()
            heading = targeted_ops.heading_text(item, doc_blocks.extract_text)
            if heading:
                headings.append((level, heading))
        line = block_to_md(item, block_map)
        if line is not None:
            md_lines.append(line)

    markdown = "\n".join(md_lines)
    title = ""
    for line in md_lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break

    return {
        "docUrl": doc_url,
        "title": title,
        "blockCount": len(items),
        "markdown": markdown,
        "rawContent": content,
        "assets": assets,
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

        parsed_table = native_tables.parse_table(lines, i)
        if parsed_table:
            table, i = parsed_table
            flush_blocks(children)
            children = []
            native_tables.write_table(doc_token, access_token, page_block_id, -1, table["_native_table"], api_call)
            counter[0] += 1
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
