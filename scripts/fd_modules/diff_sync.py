"""Conservative Docx diff sync. No credentials or task-specific state at import."""
import argparse
import difflib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from . import doc_blocks as db
from . import targeted_ops as ops
from .native_tables import INITIAL_ROW_LIMIT, split_cells


SCHEMA = 1
TEXT_KEYS = {1: "page", 2: "text", 12: "bullet", 13: "ordered", 14: "code", 17: "todo"}
TEXT_KEYS.update({level + 2: "heading" + str(level) for level in range(1, 10)})


class SyncError(ValueError):
    """A safe stop, including a partial or uncertain write requiring inspection."""

    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def parse_options(action, args):
    parser = argparse.ArgumentParser(prog="feishu-doc " + action)
    if action == "snapshot":
        parser.add_argument("output")
    else:
        parser.add_argument("content_file")
        parser.add_argument("--baseline", required=True)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--dry-run", action="store_true")
        parser.add_argument("--state", help="Required for --apply; must not already exist")
    result = vars(parser.parse_args(args))
    if result.get("apply") and not result.get("state"):
        parser.error("--apply requires --state")
    return result


def write_json(path, value, exclusive=False):
    """Local content snapshots/journals are private and never silently overwritten."""
    temporary = None
    if exclusive:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    else:
        fd, temporary = tempfile.mkstemp(prefix=".feishu-sync-", dir=str(Path(path).parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary:
            os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def checked(api_call, method, path, access_token, body=None):
    # A transport failure on mutation is never retried by the shared HTTP helper.
    try:
        response = api_call(method, path, access_token, body, retries=3 if method == "GET" else 1)
    except Exception as error:
        raise SyncError("API transport raised an error", uncertain=method != "GET") from error
    if not isinstance(response, dict) or response.get("code") != 0:
        code = response.get("code", -1) if isinstance(response, dict) else -1
        raise SyncError("API request failed: method=%s, code=%s" % (method, code),
                        uncertain=method != "GET" and (type(code) is not int or code == -1 or 500 <= code < 600))
    data = response.get("data")
    if not isinstance(data, dict):
        raise SyncError("API success response is missing data", uncertain=method != "GET")
    return data


def resolve_document(doc_type, token, access_token, api_call):
    if doc_type == "wiki":
        data = checked(api_call, "GET", "/wiki/v2/spaces/get_node?token=" + quote(token), access_token)
        node = data.get("node", {})
        if not isinstance(node, dict) or node.get("obj_type") != "docx":
            raise SyncError("Only Docx wiki nodes are supported")
        token = node.get("obj_token", "")
    elif doc_type != "docx":
        raise SyncError("snapshot/sync support only docx or Docx wiki nodes")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise SyncError("Invalid document ID")
    return token


def revision(doc_id, access_token, api_call):
    data = checked(api_call, "GET", "/docx/v1/documents/" + doc_id, access_token)
    document = data.get("document")
    value = document.get("revision_id") if isinstance(document, dict) else None
    if type(value) is not int or value < 1:
        raise SyncError("Document revision_id is required")
    return value


def fetch_snapshot(doc_id, access_token, api_call):
    before = revision(doc_id, access_token, api_call)
    blocks, cursor, seen = [], "", set()
    while True:
        path = "/docx/v1/documents/%s/blocks?page_size=500&document_revision_id=%s" % (doc_id, before)
        if cursor:
            path += "&page_token=" + quote(cursor)
        data = checked(api_call, "GET", path, access_token)
        items = data.get("items")
        if not isinstance(items, list):
            raise SyncError("Invalid block listing")
        blocks.extend(items)
        if not data.get("has_more"):
            break
        cursor = data.get("page_token")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise SyncError("Missing or repeated pagination cursor")
        seen.add(cursor)
    if revision(doc_id, access_token, api_call) != before:
        raise SyncError("Document changed while reading; take a new snapshot")
    if any(not isinstance(b, dict) or not isinstance(b.get("block_id"), str) for b in blocks):
        raise SyncError("Missing block IDs")
    result = {"schema": SCHEMA, "document_id": doc_id, "revision_id": before,
              "blocks": sorted(blocks, key=lambda block: block["block_id"])}
    # Validation rejects unsupported structures before they can become a write baseline.
    document_model(result)
    return result


def elements_model(elements):
    if not isinstance(elements, list):
        raise SyncError("Invalid text elements")
    result = []
    for element in elements:
        if not isinstance(element, dict) or set(element) != {"text_run"}:
            raise SyncError("Mentions, equations and other complex text elements are not supported")
        run = element["text_run"]
        if not isinstance(run, dict) or not isinstance(run.get("content"), str):
            raise SyncError("Invalid text run")
        if set(run) - {"content", "text_element_style"}:
            raise SyncError("Unsupported text-run fields")
        style = run.get("text_element_style", {})
        if not isinstance(style, dict):
            raise SyncError("Invalid inline style")
        # APIs may add default false flags or split runs. Neither is a content change.
        style = {key: value for key, value in style.items() if value is not False and value is not None}
        if not run["content"]:
            continue
        if result and result[-1][1] == style:
            result[-1][0] += run["content"]
        else:
            result.append([run["content"], style])
    return result


def block_model(block):
    kind = block.get("block_type")
    if kind == 22 and "divider" in block:
        return {"type": 22}
    if kind not in TEXT_KEYS:
        raise SyncError("Unsupported block type: %s" % kind)
    payload = block.get(TEXT_KEYS[kind])
    if not isinstance(payload, dict):
        raise SyncError("Missing text payload")
    model = {"type": kind, "elements": elements_model(payload.get("elements"))}
    if kind == 14:
        model["language"] = payload.get("style", {}).get("language", 21)
    if kind == 17:
        model["done"] = bool(payload.get("style", {}).get("done", False))
    return model


def cell_model(block):
    model = block_model(block)
    if not "".join(part[0] for part in model["elements"]).strip():
        model["elements"] = []
    return model


def split_text_runs(definition):
    key = TEXT_KEYS.get(definition.get("block_type"))
    if not key:
        return definition
    elements = []
    for element in definition[key]["elements"]:
        run = element["text_run"]
        for offset in range(0, max(1, len(run["content"])), 1500):
            part = dict(run)
            part["content"] = run["content"][offset:offset + 1500]
            elements.append({"text_run": part})
    definition[key]["elements"] = elements
    return definition


def document_model(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA:
        raise SyncError("Unsupported snapshot schema")
    blocks = snapshot.get("blocks")
    if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
        raise SyncError("Invalid snapshot blocks")
    mapping = {b.get("block_id"): b for b in blocks}
    if None in mapping or len(mapping) != len(blocks):
        raise SyncError("Missing or duplicate block IDs")
    root = mapping.get(snapshot.get("document_id"), {})
    if root.get("block_type") != 1:
        raise SyncError("Document root is missing")
    visited = {root["block_id"]}

    def take(block_id):
        if block_id not in mapping or block_id in visited:
            raise SyncError("Broken, duplicated or cyclic block reference")
        visited.add(block_id)
        return mapping[block_id]

    body, table_number = [], 0
    for block_id in root.get("children", []):
        block = take(block_id)
        if block.get("block_type") == 31 and "table" in block:
            table = block["table"]
            prop = table.get("property", {})
            rows, cols = prop.get("row_size"), prop.get("column_size")
            if type(rows) is not int or type(cols) is not int or rows < 1 or cols < 1:
                raise SyncError("Invalid table shape")
            cells = table.get("cells", [])
            spans = prop.get("merge_info", [])
            unmerged = not spans or (isinstance(spans, list) and len(spans) == len(cells) and all(
                isinstance(span, dict) and span.get("row_span") == span.get("col_span") == 1 for span in spans
            ))
            if len(cells) != rows * cols or block.get("children", cells) != cells or not unmerged:
                raise SyncError("Merged or irregular tables are not supported")
            values = []
            for cell_id in cells:
                cell = take(cell_id)
                if cell.get("block_type") != 32 or len(cell.get("children", [])) != 1:
                    raise SyncError("Only one plain text block per table cell is supported")
                text = take(cell["children"][0])
                if text.get("block_type") != 2 or text.get("children"):
                    raise SyncError("Complex table cells are not supported")
                values.append(cell_model(text))
            body.append({"type": 31, "number": table_number, "rows": rows, "cols": cols, "cells": values})
            table_number += 1
        else:
            if block.get("children"):
                raise SyncError("Nested blocks are not supported")
            body.append(block_model(block))
    if visited != set(mapping):
        raise SyncError("Unexpected unreachable blocks in snapshot")
    return {"title": block_model(root), "body": body}


def table_cells(line):
    line = line.strip()
    if not line.startswith("|") or not line.endswith("|"):
        raise SyncError("Tables must use leading and trailing pipes")
    return split_cells(line)


def parse_target(markdown, split_legacy_tables=False):
    lines = markdown.splitlines()
    if not lines or not lines[0].startswith("# ") or not lines[0][2:].strip():
        raise SyncError("Target Markdown must begin with '# document title'")
    title = {"block_type": 1, "page": {"elements": db.make_plain_elements(lines[0][2:].strip())}}
    definitions, pending, fenced, i = [], [], False, 1

    def flush():
        definitions.extend(ops.parse_markdown_for_targeted_insert(
            "\n".join(pending), db.make_heading_block, db.make_code_block,
            db.make_divider_block, db.make_todo_block, db.make_bullet_block,
            db.make_ordered_block, db.make_quote_block, db.make_text_block))
        pending.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            if re.search(r"!\[|<\s*(?:img|table|iframe)\b", line, re.I):
                raise SyncError("Images and embedded blocks are not supported")
            if re.match(r"^\s+([-*+]\s|\d+\.\s)", line):
                raise SyncError("Nested lists are not supported")
            if line.strip().startswith("|") and i + 1 < len(lines) and re.fullmatch(r"[\s|:\-]+", lines[i + 1]):
                flush()
                header = table_cells(line)
                separators = table_cells(lines[i + 1])
                if len(header) != len(separators) or not all(re.fullmatch(r":?-{3,}:?", s) for s in separators):
                    raise SyncError("Invalid table separator")
                rows = [header]; i += 2
                while i < len(lines) and lines[i].strip().startswith("|"):
                    cells = table_cells(lines[i])
                    if len(cells) != len(header):
                        raise SyncError("Unequal table row lengths")
                    rows.append(cells); i += 1
                if split_legacy_tables and len(rows) > INITIAL_ROW_LIMIT:
                    page_size = INITIAL_ROW_LIMIT - 1
                    for offset in range(1, len(rows), page_size):
                        definitions.append({"table_rows": [header] + rows[offset:offset + page_size]})
                else:
                    definitions.append({"table_rows": rows})
                continue
        pending.append(line); i += 1
    if fenced:
        raise SyncError("Unclosed code fence")
    flush()
    body, number = [], 0
    for definition in definitions:
        if "table_rows" in definition:
            rows = definition["table_rows"]
            model = {"type": 31, "number": number, "rows": len(rows), "cols": len(rows[0]),
                     "cells": [cell_model(db.make_text_block(cell)) for row in rows for cell in row]}
            number += 1
        else:
            split_text_runs(definition)
            model = block_model(definition)
        body.append(model)
    return {"title": block_model(title), "body": body}, title, definitions


def build_plan(baseline, markdown):
    old = document_model(baseline)
    new, title, definitions = parse_target(markdown)
    old_tables = [b for b in old["body"] if b["type"] == 31]
    new_tables = [b for b in new["body"] if b["type"] == 31]
    old_shapes = [(b["rows"], b["cols"]) for b in old_tables]
    new_shapes = [(b["rows"], b["cols"]) for b in new_tables]
    if old_shapes != new_shapes and any(rows > INITIAL_ROW_LIMIT for rows, _ in new_shapes):
        # An older writer may already have split the input into several native tables.
        new, title, definitions = parse_target(markdown, split_legacy_tables=True)
        new_tables = [b for b in new["body"] if b["type"] == 31]
        new_shapes = [(b["rows"], b["cols"]) for b in new_tables]
    if old_shapes != new_shapes:
        raise SyncError("Adding/removing tables or changing table rows/columns is not supported")
    mapping = {b["block_id"]: b for b in baseline["blocks"]}
    root = mapping[baseline["document_id"]]
    children = [mapping[b] for b in root.get("children", [])]
    operations, changed, deleted = [], set(), set()

    def patch(block, definition):
        split_text_runs(definition)
        changed.add(block["block_id"])
        operations.append({"method": "PATCH", "suffix": "/blocks/batch_update", "body": {"requests": [
            {"block_id": block["block_id"], "update_text_elements": {
                "elements": definition[TEXT_KEYS[definition["block_type"]]]["elements"]}}]}})

    if old["title"] != new["title"]:
        patch(root, title)
    for block, model in zip(children, old["body"]):
        if model["type"] != 31:
            continue
        desired = new_tables[model["number"]]
        if model == desired:
            continue
        rows = next(d["table_rows"] for d, m in zip(definitions, new["body"]) if m is desired)
        for pos, (a, b) in enumerate(zip(model["cells"], desired["cells"])):
            if a != b:
                cell = mapping[block["table"]["cells"][pos]]
                patch(mapping[cell["children"][0]], db.make_text_block(rows[pos // model["cols"]][pos % model["cols"]]))

    def key(model):
        return ("table", model["number"]) if model["type"] == 31 else digest(model)

    matcher = difflib.SequenceMatcher(a=[key(m) for m in old["body"]], b=[key(m) for m in new["body"]], autojunk=False)
    for tag, i1, i2, j1, j2 in reversed(matcher.get_opcodes()):
        if tag == "equal":
            continue
        before, after = old["body"][i1:i2], new["body"][j1:j2]
        if any(b["type"] == 31 for b in before + after):
            raise SyncError("Moving or replacing tables is not supported")
        paired = len(before) == len(after) and all(
            a["type"] == b["type"] and a.get("language") == b.get("language") and a.get("done") == b.get("done")
            for a, b in zip(before, after))
        if paired:
            for block, definition in zip(children[i1:i2], definitions[j1:j2]):
                patch(block, definition)
            continue
        if before:
            deleted.update(b["block_id"] for b in children[i1:i2])
            for offset in range(0, i2 - i1, 50):
                operations.append({"method": "DELETE", "suffix": "/blocks/%s/children/batch_delete" % baseline["document_id"],
                                   "body": {"start_index": i1, "end_index": i1 + min(50, i2 - i1 - offset)},
                                   "block_ids": [b["block_id"] for b in children[i1 + offset:min(i1 + offset + 50, i2)]]})
        for offset in range(j1, j2, 30):
            operations.append({"method": "POST", "suffix": "/blocks/%s/children" % baseline["document_id"],
                               "body": {"children": definitions[offset:min(offset + 30, j2)], "index": i1 + offset - j1}})
    protected = {b["block_id"]: b for b in baseline["blocks"]
                 if b["block_id"] != root["block_id"] and b["block_id"] not in changed | deleted}
    return {"operations": operations, "expected": new, "protected": protected}


def handle_sync(doc_id, access_token, content_file, options, api_call):
    if options.get("apply") and options.get("state") and Path(options["state"]).exists():
        raise SyncError("State file already exists; inspect the prior run before starting another")
    baseline = json.loads(Path(options["baseline"]).read_text(encoding="utf-8"))
    if not isinstance(baseline, dict) or baseline.get("document_id") != doc_id:
        raise SyncError("Baseline belongs to a different document")
    markdown = Path(content_file).read_text(encoding="utf-8")
    plan = build_plan(baseline, markdown)
    current = fetch_snapshot(doc_id, access_token, api_call)
    if current != baseline:
        raise SyncError("Remote document differs from baseline; do not overwrite other edits")
    mapping = {b["block_id"]: b for b in baseline["blocks"]}

    def styles(elements):
        return list({json.dumps(part[1], sort_keys=True): part[1] for part in elements_model(elements)}.values())[:20]

    def preview(operation):
        body = operation["body"]
        value = {"method": operation["method"], "path": operation["suffix"]}
        if "requests" in body:
            value["updates"] = [{"block_id": request["block_id"],
                                  "before_text": db.extract_block_text(mapping[request["block_id"]])[:200],
                                  "after_text": db.extract_text(request["update_text_elements"]["elements"])[:200],
                                  "before_styles": styles(mapping[request["block_id"]][TEXT_KEYS[mapping[request["block_id"]]["block_type"]]]["elements"]),
                                  "after_styles": styles(request["update_text_elements"]["elements"]),
                                  "elements_changed": True} for request in body["requests"]]
        elif "children" in body:
            value.update(index=body["index"], insert=[{"block_type": block["block_type"],
                         "text": db.extract_block_text(block)[:200],
                         "language": block_model(block).get("language"),
                         "done": block_model(block).get("done")} for block in body["children"]])
        else:
            value.update(start_index=body["start_index"], end_index=body["end_index"],
                         block_ids=operation["block_ids"])
        return value

    result = {"action": "sync", "document_id": doc_id, "status": "preview",
              "operations": [preview(o) for o in plan["operations"]],
              "source_sha256": hashlib.sha256(markdown.encode()).hexdigest()}
    if not options.get("apply"):
        return result
    state_path = options.get("state")
    if not state_path:
        raise SyncError("--apply requires a new --state file")
    journal = {"schema": SCHEMA, "document_id": doc_id, "baseline_sha256": digest(baseline),
               "source_sha256": result["source_sha256"], "status": "started", "completed": [],
               "revision_id": baseline["revision_id"], "inflight": None}
    write_json(state_path, journal, exclusive=True)
    try:
        for index, operation in enumerate(plan["operations"]):
            if revision(doc_id, access_token, api_call) != journal["revision_id"]:
                raise SyncError("Document changed before next write; inspect partial results")
            journal["inflight"] = {"index": index, "method": operation["method"], "path": operation["suffix"]}
            write_json(state_path, journal)
            path = "/docx/v1/documents/" + doc_id + operation["suffix"]
            path += "?document_revision_id=" + str(journal["revision_id"])
            data = checked(api_call, operation["method"], path, access_token, operation["body"])
            new_revision = data.get("document_revision_id")
            if type(new_revision) is not int or new_revision <= journal["revision_id"]:
                raise SyncError("Write acknowledged without a usable revision; inspect before further writes", uncertain=True)
            journal["revision_id"] = new_revision
            journal["completed"].append(index)
            journal["inflight"] = None
            write_json(state_path, journal)
        after = fetch_snapshot(doc_id, access_token, api_call)
        if after["revision_id"] != journal["revision_id"] or document_model(after) != plan["expected"]:
            raise SyncError("Read-back differs from expected result; inspect document")
        after_map = {b["block_id"]: b for b in after["blocks"]}
        if any(after_map.get(bid) != value for bid, value in plan["protected"].items()):
            raise SyncError("An unchanged block was modified; inspect document")
        journal["status"] = "verified"
        write_json(state_path, journal)
    except Exception as error:
        if isinstance(error, SyncError) and not error.uncertain and journal["inflight"]:
            journal["rejected_operation"] = journal["inflight"]
            journal["inflight"] = None
        journal["status"] = "uncertain" if journal["inflight"] else "stopped"
        journal["stop_reason"] = str(error) if isinstance(error, SyncError) else type(error).__name__
        write_json(state_path, journal)
        raise SyncError("Sync stopped; inspect state file and remote blocks before retrying") from error
    result.update(status="verified", state_file=str(state_path), completed_operations=len(journal["completed"]))
    return result


def run(action, doc_type, token, access_token, options, api_call):
    doc_id = resolve_document(doc_type, token, access_token, api_call)
    if action == "snapshot":
        snapshot = fetch_snapshot(doc_id, access_token, api_call)
        write_json(options["output"], snapshot, exclusive=True)
        return {"action": "snapshot", "document_id": doc_id, "revision_id": snapshot["revision_id"],
                "output": options["output"], "status": "saved"}
    return handle_sync(doc_id, access_token, options["content_file"], options, api_call)
