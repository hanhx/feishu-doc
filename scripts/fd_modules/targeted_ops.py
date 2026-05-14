import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor


def parse_cli_options(args):
    options = {}
    positionals = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            key = arg[2:]
            if "=" in key:
                k, v = key.split("=", 1)
                options[k.replace("-", "_")] = v
            else:
                k = key.replace("-", "_")
                if i + 1 < len(args) and not args[i + 1].startswith("--"):
                    options[k] = args[i + 1]
                    i += 1
                else:
                    options[k] = True
        else:
            positionals.append(arg)
        i += 1
    return positionals, options


def flag_enabled(val):
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def fetch_all_blocks(doc_token, access_token, api_call, check_resp):
    items = []
    page_token = ""
    while True:
        url = f"/docx/v1/documents/{doc_token}/blocks?page_size=500"
        if page_token:
            url += f"&page_token={page_token}"
        resp = api_call("GET", url, access_token)
        data = check_resp(resp, "获取文档块", auto_retry_login=True)
        items.extend(data.get("items", []))
        if not data.get("has_more", False):
            break
        page_token = data.get("page_token", "")
        if not page_token:
            break
    block_map = {it.get("block_id"): it for it in items if it.get("block_id")}
    return items, block_map


def build_parent_index_maps(block_map):
    parent_map = {}
    index_map = {}
    for parent_id, block in block_map.items():
        children = block.get("children", []) or []
        for idx, child_id in enumerate(children):
            parent_map[child_id] = parent_id
            index_map[(parent_id, child_id)] = idx
    return parent_map, index_map


def heading_level(block):
    btype = block.get("block_type", 0)
    if 3 <= btype <= 11:
        return btype - 2
    return None


def heading_text(block, extract_text):
    btype = block.get("block_type", 0)
    if 3 <= btype <= 11:
        level = btype - 2
        return extract_text(block.get(f"heading{level}", {}).get("elements", [])).strip()
    return ""


def match_query(text, query, mode):
    t = (text or "").strip()
    q = (query or "").strip()
    if not t or not q:
        return False
    if mode == "regex":
        try:
            return re.search(q, t, re.IGNORECASE) is not None
        except re.error as e:
            print(f"❌ 正则表达式错误: {e}", file=sys.stderr)
            sys.exit(1)
    return q.lower() in t.lower()


def find_anchor_candidates(
    items,
    block_map,
    anchor_type,
    anchor,
    match_mode,
    get_block_text_by_id,
    extract_text,
):
    candidates = []
    for item in items:
        block_id = item.get("block_id", "")
        if not block_id:
            continue
        if anchor_type == "heading":
            lvl = heading_level(item)
            if lvl is None:
                continue
            text = heading_text(item, extract_text)
            if match_query(text, anchor, match_mode):
                candidates.append({"block_id": block_id, "text": text, "level": lvl, "kind": "heading"})
        else:
            text = get_block_text_by_id(block_id, block_map).strip()
            if match_query(text, anchor, match_mode):
                candidates.append({"block_id": block_id, "text": text, "level": None, "kind": "text"})
    return candidates


def collect_heading_candidates(items, extract_text):
    rows = []
    for item in items:
        lvl = heading_level(item)
        if lvl is None:
            continue
        txt = heading_text(item, extract_text)
        if txt:
            rows.append({"block_id": item.get("block_id", ""), "level": lvl, "text": txt})
    return rows


def resolve_single_candidate(candidates, items, anchor, anchor_type, extract_text):
    if not candidates:
        print(f"❌ 未匹配到{anchor_type}锚点: {anchor}", file=sys.stderr)
        hints = collect_heading_candidates(items, extract_text)
        if hints:
            print("\n可用标题候选：", file=sys.stderr)
            for h in hints[:30]:
                print(f"  - [H{h['level']}] {h['text']} (block_id={h['block_id']})", file=sys.stderr)
        sys.exit(1)
    if len(candidates) > 1:
        print(f"❌ 匹配到多个{anchor_type}锚点，请提供更精确条件:", file=sys.stderr)
        for c in candidates[:30]:
            suffix = f"H{c['level']}" if c["level"] else "text"
            preview = c["text"][:80]
            print(f"  - [{suffix}] {preview} (block_id={c['block_id']})", file=sys.stderr)
        sys.exit(1)
    return candidates[0]


def compute_section_end_index(children_ids, start_index, start_level, block_map):
    end_index = len(children_ids)
    for i in range(start_index + 1, len(children_ids)):
        blk = block_map.get(children_ids[i], {})
        lvl = heading_level(blk)
        if lvl is not None and lvl <= start_level:
            end_index = i
            break
    return end_index


def summarize_content(content, max_lines=5):
    lines = [ln for ln in content.splitlines() if ln.strip()]
    if not lines:
        return ""
    return "\n".join(lines[:max_lines])


def confirm_with_preview(preview, yes_flag, action_name):
    print(json.dumps({"action": action_name, "preview": preview}, ensure_ascii=False, indent=2))
    if yes_flag:
        return
    if not sys.stdin.isatty():
        print(f"❌ {action_name} 需要确认：请在命令中增加 --yes 或在交互终端执行", file=sys.stderr)
        sys.exit(1)
    ans = input("⚠️ 将执行上述变更，输入 yes 确认，其它任意输入取消: ").strip()
    if ans != "yes":
        print("已取消执行", file=sys.stderr)
        sys.exit(1)


def parse_markdown_for_targeted_insert(
    content,
    make_heading_block,
    make_code_block,
    make_divider_block,
    make_todo_block,
    make_bullet_block,
    make_ordered_block,
    make_quote_block,
    make_text_block,
):
    lines = content.split("\n")
    children = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            children.append(make_code_block("\n".join(code_lines), lang))
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
            children.append(make_heading_block(len(hm.group(1)), hm.group(2)))
            i += 1
            continue

        stripped = line.lstrip()
        tm = re.match(r"^-\s*\[([ xX])\]\s*(.*)", stripped)
        if tm:
            children.append(make_todo_block(tm.group(2), tm.group(1).lower() == "x"))
            i += 1
            continue

        if re.match(r"^[-*+]\s+", stripped):
            children.append(make_bullet_block(re.sub(r"^[-*+]\s+", "", stripped)))
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

        # 表格：解析为原生表格标记，由 insert_blocks_at_index 负责创建
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
                children.append({
                    "_native_table": True,
                    "_header_cells": header_cells,
                    "_data_rows": data_rows,
                    "_col_size": col_size,
                    "_col_widths": col_widths,
                    "_raw_lines": table_lines,
                })
            continue

        children.append(make_text_block(line))
        i += 1

    return children


def insert_blocks_at_index(
    doc_token,
    access_token,
    parent_block_id,
    insert_index,
    block_list,
    api_call,
    check_resp,
    make_text_elements,
    make_plain_elements=None,
):
    batch_size = 50
    current_index = insert_index
    pending = []
    added = 0

    def flush_pending():
        nonlocal pending, current_index, added
        while pending:
            batch = pending[:batch_size]
            pending = pending[batch_size:]
            idx = current_index if current_index is not None else -1
            resp = api_call(
                "POST",
                f"/docx/v1/documents/{doc_token}/blocks/{parent_block_id}/children",
                access_token,
                {"children": batch, "index": idx},
            )
            check_resp(resp, "插入文档内容", auto_retry_login=True)
            added += len(batch)
            if current_index is not None:
                current_index += len(batch)
            time.sleep(0.3)

    for blk in block_list:
        if blk.get("_callout"):
            flush_pending()
            idx = current_index if current_index is not None else -1
            callout_block = {"block_type": 19, "callout": {"background_color": 15}}
            cr = api_call(
                "POST",
                f"/docx/v1/documents/{doc_token}/blocks/{parent_block_id}/children",
                access_token,
                {"children": [callout_block], "index": idx},
            )
            cd = check_resp(cr, "插入引用块", auto_retry_login=True)
            added += 1
            if current_index is not None:
                current_index += 1

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
        elif blk.get("_native_table"):
            flush_pending()
            header_cells = blk["_header_cells"]
            data_rows = blk["_data_rows"]
            col_size = blk["_col_size"]
            col_widths = blk["_col_widths"]
            raw_lines = blk["_raw_lines"]
            max_data_rows = 8
            _plain_el = make_plain_elements if make_plain_elements else make_text_elements

            def create_and_fill_table(h_cells, d_rows, c_size, c_widths):
                idx = current_index if current_index is not None else -1
                tb = {
                    "block_type": 31,
                    "table": {
                        "property": {
                            "row_size": 1 + len(d_rows),
                            "column_size": c_size,
                            "column_width": c_widths,
                            "header_row": True,
                        },
                    },
                }
                tr = api_call(
                    "POST",
                    f"/docx/v1/documents/{doc_token}/blocks/{parent_block_id}/children",
                    access_token,
                    {"children": [tb], "index": idx},
                )
                if tr.get("code", -1) != 0:
                    print(f"⚠️ 表格创建失败, fallback: {tr.get('msg', '')[:80]}", file=sys.stderr)
                    return False
                tc = tr.get("data", {}).get("children", [])
                if tc:
                    cids = tc[0].get("table", {}).get("cells", [])
                    a_rows = [h_cells] + d_rows

                    def fill_cell(args):
                        cell_id, text, is_header = args
                        el = _plain_el(text) if is_header else make_text_elements(text)
                        get_resp = api_call("GET", f"/docx/v1/documents/{doc_token}/blocks/{cell_id}", access_token)
                        auto_children = get_resp.get("data", {}).get("block", {}).get("children", [])
                        if auto_children:
                            first_child_id = auto_children[0]
                            api_call("PATCH", f"/docx/v1/documents/{doc_token}/blocks/{first_child_id}", access_token,
                                     {"update_text_elements": {"elements": el}})
                            for child_id in auto_children[1:]:
                                api_call("DELETE", f"/docx/v1/documents/{doc_token}/blocks/{child_id}", access_token)
                        else:
                            cell_block = {"block_type": 2, "text": {"elements": el}}
                            api_call("POST", f"/docx/v1/documents/{doc_token}/blocks/{cell_id}/children",
                                     access_token, {"children": [cell_block], "index": -1})
                        time.sleep(0.02)

                    tasks = []
                    for ri, rc in enumerate(a_rows):
                        for ci2 in range(c_size):
                            cidx = ri * c_size + ci2
                            if cidx >= len(cids):
                                break
                            ct = rc[ci2] if ci2 < len(rc) else ""
                            tasks.append((cids[cidx], ct, ri == 0))
                    for batch_start in range(0, len(tasks), 5):
                        batch = tasks[batch_start:batch_start + 5]
                        with ThreadPoolExecutor(max_workers=5) as pool:
                            futures = [pool.submit(fill_cell, task) for task in batch]
                            for future in futures:
                                future.result()
                time.sleep(0.5)
                return True

            success = False
            for chunk_start in range(0, len(data_rows), max_data_rows):
                chunk = data_rows[chunk_start:chunk_start + max_data_rows]
                if not create_and_fill_table(header_cells, chunk, col_size, col_widths):
                    # fallback: code block
                    fb = api_call(
                        "POST",
                        f"/docx/v1/documents/{doc_token}/blocks/{parent_block_id}/children",
                        access_token,
                        {"children": [{"block_type": 14, "code": {"language": 1, "elements": [{"text_run": {"content": "\n".join(raw_lines)}}]}}],
                         "index": current_index if current_index is not None else -1},
                    )
                    check_resp(fb, "插入表格(fallback代码块)", auto_retry_login=True)
                added += 1
                if current_index is not None:
                    current_index += 1
        else:
            pending.append(blk)

    flush_pending()
    return added


def delete_children_range(doc_token, access_token, parent_block_id, start_index, end_index, api_call, check_resp):
    if end_index <= start_index:
        return 0
    remaining = end_index - start_index
    deleted = 0
    while remaining > 0:
        batch = min(50, remaining)
        resp = api_call(
            "DELETE",
            f"/docx/v1/documents/{doc_token}/blocks/{parent_block_id}/children/batch_delete",
            access_token,
            {"start_index": start_index, "end_index": start_index + batch},
        )
        check_resp(resp, "删除文档块", auto_retry_login=True)
        remaining -= batch
        deleted += batch
        time.sleep(0.2)
    return deleted
