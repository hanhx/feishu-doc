import contextlib
import copy
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from fd_modules import diff_sync as sync
from fd_modules import doc_blocks as db
import index


class FakeDocx:
    """In-memory API boundary, including revisions, pagination and native table IDs."""

    def __init__(self, markdown="# Example\n\nold text", doc_id="example_doc"):
        self.doc_id, self.rev, self.counter = doc_id, 1, 0
        self.blocks, self.calls, self.writes = {}, [], []
        self.fail_at, self.fail_response, self.raise_at = None, None, None
        self.drift_after_write, self.corrupt_after_write = False, False
        self.response_after_commit = None
        self.page_limit = 500
        _, title, definitions = sync.parse_target(markdown)
        root = copy.deepcopy(title)
        root.update(block_id=doc_id, children=[])
        self.blocks[doc_id] = root
        for definition in definitions:
            root["children"].append(self.add(definition))

    def add(self, definition):
        self.counter += 1
        block_id = "block_%s" % self.counter
        if "table_rows" in definition:
            rows = definition["table_rows"]
            table = {"block_id": block_id, "block_type": 31, "children": [],
                     "table": {"property": {"row_size": len(rows), "column_size": len(rows[0])}, "cells": []}}
            self.blocks[block_id] = table
            for row in rows:
                for cell in row:
                    self.counter += 1
                    cell_id = "cell_%s" % self.counter
                    text_id = self.add(db.make_text_block(cell))
                    self.blocks[cell_id] = {"block_id": cell_id, "block_type": 32, "children": [text_id]}
                    table["children"].append(cell_id)
                    table["table"]["cells"].append(cell_id)
            return block_id
        block = copy.deepcopy(definition)
        block["block_id"] = block_id
        self.blocks[block_id] = block
        return block_id

    def snapshot(self):
        return {"schema": 1, "document_id": self.doc_id, "revision_id": self.rev,
                "blocks": sorted(copy.deepcopy(list(self.blocks.values())), key=lambda b: b["block_id"])}

    def __call__(self, method, path, access_token, body=None, retries=3):
        self.calls.append((method, path, copy.deepcopy(body), retries))
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if parsed.path == "/wiki/v2/spaces/get_node":
            return {"code": 0, "data": {"node": {"obj_type": "docx", "obj_token": self.doc_id}}}
        prefix = "/docx/v1/documents/" + self.doc_id
        if method == "GET":
            if parsed.path == prefix:
                return {"code": 0, "data": {"document": {"revision_id": self.rev}}}
            if parsed.path == prefix + "/blocks":
                offset = int(query.get("page_token", [0])[0])
                items = list(reversed(list(self.blocks.values())))
                end = offset + self.page_limit
                return {"code": 0, "data": {"items": copy.deepcopy(items[offset:end]),
                        "has_more": end < len(items), "page_token": str(end)}}
            raise AssertionError("Unexpected GET " + path)
        self.writes.append((method, path, copy.deepcopy(body), retries))
        if self.raise_at == len(self.writes):
            raise TimeoutError("simulated transport timeout")
        if self.fail_at == len(self.writes):
            return self.fail_response
        if int(query["document_revision_id"][0]) != self.rev:
            return {"code": 1770001, "msg": "revision mismatch"}
        if method == "PATCH":
            for request in body["requests"]:
                block = self.blocks[request["block_id"]]
                key = sync.TEXT_KEYS[block["block_type"]]
                block[key]["elements"] = copy.deepcopy(request["update_text_elements"]["elements"])
        elif method == "DELETE":
            root = self.blocks[self.doc_id]
            removed = root["children"][body["start_index"]:body["end_index"]]
            del root["children"][body["start_index"]:body["end_index"]]
            for block_id in removed:
                del self.blocks[block_id]
        elif method == "POST":
            ids = [self.add(definition) for definition in body["children"]]
            pos = body["index"]
            self.blocks[self.doc_id]["children"][pos:pos] = ids
        else:
            raise AssertionError("Unexpected write")
        self.rev += 1
        response = {"code": 0, "data": {"document_revision_id": self.rev}}
        if self.drift_after_write:
            self.rev += 1
        if self.corrupt_after_write:
            first = self.blocks[self.doc_id]["children"][0]
            self.blocks[first][sync.TEXT_KEYS[self.blocks[first]["block_type"]]]["elements"] = db.make_plain_elements("unexpected")
        return self.response_after_commit or response


class DiffSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        # All tests, including CLI routing, must stay offline.
        self.network = patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def prepare(self, fake, desired):
        self.prepared = getattr(self, "prepared", 0) + 1
        self.baseline = self.root / "baseline.json"
        self.target = self.root / "desired.md"
        self.state = self.root / ("run_%s.json" % self.prepared)
        self.baseline.write_text(json.dumps(fake.snapshot()), encoding="utf-8")
        self.target.write_text(desired, encoding="utf-8")
        return {"baseline": str(self.baseline), "state": str(self.state), "apply": True}

    def apply(self, fake, desired, apply=True):
        options = self.prepare(fake, desired)
        options["apply"] = apply
        return sync.handle_sync(fake.doc_id, "[REDACTED_SECRET]", self.target, options, fake)

    def test_preview_is_default_and_has_no_writes_or_state(self):
        fake = FakeDocx()
        result = self.apply(fake, "# Example\n\nnew text", apply=False)
        self.assertEqual(result["status"], "preview")
        self.assertEqual(len(result["operations"]), 1)
        update = result["operations"][0]["updates"][0]
        self.assertEqual(update["before_text"], "old text")
        self.assertEqual(update["after_text"], "new text")
        self.assertEqual(fake.writes, [])
        self.assertFalse(self.state.exists())

    def test_snapshot_reads_all_pages_and_private_exclusive_file(self):
        fake = FakeDocx("# Example\n\na\n\nb\n\nc")
        fake.page_limit = 2
        path = self.root / "snapshot.json"
        result = sync.run("snapshot", "docx", fake.doc_id, "[REDACTED_SECRET]", {"output": str(path)}, fake)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(json.loads(path.read_text()), fake.snapshot())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            sync.run("snapshot", "docx", fake.doc_id, "[REDACTED_SECRET]", {"output": str(path)}, fake)
        self.assertEqual(fake.writes, [])

    def test_wiki_resolves_object_id_without_task_binding(self):
        fake = FakeDocx(doc_id="another_document")
        resolved = sync.resolve_document("wiki", "wiki_node", "[REDACTED_SECRET]", fake)
        self.assertEqual(resolved, fake.doc_id)
        self.assertEqual(fake.writes, [])

    def test_no_change_performs_no_mutation(self):
        fake = FakeDocx()
        self.apply(fake, "# Example\n\nold text")
        self.assertEqual(fake.writes, [])
        self.assertEqual(json.loads(self.state.read_text())["status"], "verified")

    def test_changes_text_and_title_but_preserves_block_ids_and_other_styles(self):
        fake = FakeDocx("# Example\n\nold\n\nkeep")
        ids = fake.blocks[fake.doc_id]["children"][:]
        fake.blocks[ids[1]]["text"]["style"] = {"align": 2}
        original = copy.deepcopy(fake.blocks[ids[1]])
        self.apply(fake, "# Changed title\n\nnew\n\nkeep")
        self.assertEqual(fake.blocks[fake.doc_id]["children"], ids)
        self.assertEqual(fake.blocks[ids[1]], original)
        self.assertTrue(all(call[3] == 1 for call in fake.writes))

    def test_bold_and_link_only_change_is_not_ignored(self):
        for desired in ["**old text**", "[old text](https://example.invalid/new)"]:
            with self.subTest(desired=desired):
                fake = FakeDocx()
                self.apply(fake, "# Example\n\n" + desired)
                self.assertEqual(len(fake.writes), 1)

    def test_style_preview_exposes_style_change_even_when_text_is_unchanged(self):
        fake = FakeDocx()
        result = self.apply(fake, "# Example\n\n**old text**", apply=False)
        update = result["operations"][0]["updates"][0]
        self.assertEqual(update["before_text"], update["after_text"])
        self.assertNotEqual(update["before_styles"], update["after_styles"])
        self.assertEqual(update["after_styles"], [{"bold": True}])

    def test_adjacent_run_splits_and_false_flags_are_semantically_equal(self):
        fake = FakeDocx()
        first = fake.blocks[fake.doc_id]["children"][0]
        fake.blocks[first]["text"]["elements"] = [
            {"text_run": {"content": "old ", "text_element_style": {"bold": False}}},
            {"text_run": {"content": "text"}}]
        self.apply(fake, "# Example\n\nold text")
        self.assertEqual(fake.writes, [])

    def test_mermaid_content_updates_existing_block_with_long_runs_split(self):
        fake = FakeDocx("# Example\n\n```mermaid\nsequenceDiagram\n```\n\nkeep")
        code_id = fake.blocks[fake.doc_id]["children"][0]
        content = "sequenceDiagram\n" + "A->>B: hello\n" * 400
        self.apply(fake, "# Example\n\n```mermaid\n" + content.rstrip() + "\n```\n\nkeep")
        self.assertEqual(fake.blocks[fake.doc_id]["children"][0], code_id)
        runs = fake.blocks[code_id]["code"]["elements"]
        self.assertTrue(all(len(run["text_run"]["content"]) <= 1500 for run in runs))

    def test_code_language_change_replaces_block_and_retains_following_block(self):
        fake = FakeDocx("# Example\n\n```java\nx\n```\n\nkeep")
        old = fake.blocks[fake.doc_id]["children"][:]
        self.apply(fake, "# Example\n\n```python\nx\n```\n\nkeep")
        new = fake.blocks[fake.doc_id]["children"]
        self.assertNotEqual(old[0], new[0])
        self.assertEqual(old[1], new[1])

    def test_flat_lists_todo_headings_and_insert_delete(self):
        fake = FakeDocx("# Example\n\n## Heading\n\n- old\n\n1. first\n\n- [ ] task\n\nremove\n\nkeep")
        target = "# Example\n\n## Heading\n\n- new\n\n1. first\n\n- [x] task\n\nkeep\n\nnew tail"
        self.apply(fake, target)
        self.assertEqual(sync.document_model(fake.snapshot()), sync.parse_target(target)[0])

    def test_same_shape_table_changes_only_cell_and_preserves_empty_and_pipes(self):
        original = "# Example\n\n| A | B |\n| --- | --- |\n| | old |\n| a\\|b | `x|y` |"
        fake = FakeDocx(original)
        ids = set(fake.blocks)
        target = original.replace("| | old |", "| | **new** |")
        self.apply(fake, target)
        self.assertEqual(set(fake.blocks), ids)
        self.assertEqual(len(fake.writes), 1)
        cells = sync.document_model(fake.snapshot())["body"][0]["cells"]
        self.assertEqual(cells[2]["elements"], [])
        self.assertEqual(cells[4]["elements"][0][0], "a|b")
        self.assertEqual(cells[5]["elements"][0][0], "x|y")

    def test_table_and_adjacent_paragraph_changes_can_be_planned_together(self):
        fake = FakeDocx("# Example\n\nold\n\n| A |\n| --- |\n| old |\n\nend")
        self.apply(fake, "# Example\n\nnew\n\nextra\n\n| A |\n| --- |\n| new |\n\nchanged")
        self.assertEqual(json.loads(self.state.read_text())["status"], "verified")

    def test_table_structure_change_rejected_before_any_write(self):
        original = "# Example\n\n| A |\n| --- |\n| old |"
        for desired in [original + "\n| extra |", "# Example\n\nno table", "# Example\n\n| A | B |\n| --- | --- |\n| a | b |"]:
            with self.subTest(desired=desired):
                fake = FakeDocx(original)
                with self.assertRaises(sync.SyncError):
                    self.apply(fake, desired)
                self.assertEqual(fake.writes, [])

    def test_unsupported_remote_images_and_nested_blocks_fail_closed(self):
        for block in [{"block_type": 27, "image": {"token": "placeholder"}},
                      {"block_type": 2, "text": {"elements": db.make_plain_elements("old")}, "children": ["nested"]}]:
            fake = FakeDocx()
            first = fake.blocks[fake.doc_id]["children"][0]
            fake.blocks[first] = dict(block, block_id=first)
            with self.assertRaises(sync.SyncError):
                self.apply(fake, "# Example\n\nnew")
            self.assertEqual(fake.writes, [])

    def test_unsupported_target_structures_rejected_before_write(self):
        for text in ["> quoted callout", "![image](https://example.invalid/x)", "  - nested", "```java\nunclosed"]:
            with self.subTest(text=text):
                fake = FakeDocx()
                with self.assertRaises(sync.SyncError):
                    self.apply(fake, "# Example\n\n" + text)
                self.assertEqual(fake.writes, [])

    def test_remote_style_only_drift_fails_before_write(self):
        fake = FakeDocx()
        options = self.prepare(fake, "# Example\n\nnew")
        first = fake.blocks[fake.doc_id]["children"][0]
        fake.blocks[first]["text"]["elements"][0]["text_run"]["text_element_style"] = {"bold": True}
        with self.assertRaises(sync.SyncError):
            sync.handle_sync(fake.doc_id, "[REDACTED_SECRET]", self.target, options, fake)
        self.assertEqual(fake.writes, [])
        self.assertFalse(self.state.exists())

    def test_baseline_document_mismatch(self):
        fake = FakeDocx()
        options = self.prepare(fake, "# Example\n\nnew")
        with self.assertRaises(sync.SyncError):
            sync.handle_sync("another_doc", "[REDACTED_SECRET]", self.target, options, fake)
        self.assertEqual(fake.calls, [])

    def test_unknown_write_result_stops_and_does_not_retry(self):
        fake = FakeDocx()
        fake.fail_at, fake.fail_response = 1, {"code": -1, "msg": "simulated connection error"}
        with self.assertRaises(sync.SyncError):
            self.apply(fake, "# Example\n\nnew")
        journal = json.loads(self.state.read_text())
        self.assertEqual(journal["status"], "uncertain")
        self.assertIsNotNone(journal["inflight"])
        self.assertEqual(len(fake.writes), 1)
        self.assertEqual(fake.writes[0][3], 1)
        with self.assertRaises(sync.SyncError):
            sync.handle_sync(fake.doc_id, "[REDACTED_SECRET]", self.target,
                             {"baseline": str(self.baseline), "state": str(self.state), "apply": True}, fake)
        self.assertEqual(len(fake.writes), 1)

    def test_transport_exception_does_not_expose_error_details(self):
        fake = FakeDocx()
        fake.raise_at = 1
        with self.assertRaises(sync.SyncError):
            self.apply(fake, "# Example\n\nnew")
        journal = json.loads(self.state.read_text())
        self.assertEqual(journal["status"], "uncertain")
        self.assertNotIn("REDACTED_SECRET", self.state.read_text())

    def test_write_committed_then_transport_result_unknown_is_not_replayed(self):
        fake = FakeDocx()
        fake.response_after_commit = {"code": -1}
        with self.assertRaises(sync.SyncError):
            self.apply(fake, "# Example\n\nchanged")
        first = fake.blocks[fake.doc_id]["children"][0]
        self.assertEqual(db.extract_block_text(fake.blocks[first]), "changed")
        self.assertEqual(json.loads(self.state.read_text())["status"], "uncertain")
        with self.assertRaises(sync.SyncError):
            sync.handle_sync(fake.doc_id, "[REDACTED_SECRET]", self.target,
                             {"baseline": str(self.baseline), "state": str(self.state), "apply": True}, fake)
        self.assertEqual(len(fake.writes), 1)

    def test_success_without_revision_is_uncertain_and_stops_following_operations(self):
        fake = FakeDocx()
        fake.response_after_commit = {"code": 0, "data": {}}
        with self.assertRaises(sync.SyncError):
            self.apply(fake, "# New title\n\nchanged")
        self.assertEqual(len(fake.writes), 1)
        self.assertEqual(json.loads(self.state.read_text())["status"], "uncertain")

    def test_native_fixture_not_built_by_markdown_parser(self):
        fake = FakeDocx()
        fake.blocks = {
            fake.doc_id: {"block_type": 1, "block_id": fake.doc_id, "children": ["manual_text"],
                          "page": {"elements": [{"text_run": {"content": "Example"}}]}},
            "manual_text": {"block_type": 2, "block_id": "manual_text", "text": {"elements": [
                {"text_run": {"content": "native ", "text_element_style": {"bold": True}}},
                {"text_run": {"content": "link", "text_element_style": {"link": {"url": "https://example.invalid/old"}}}}]}}
        }
        self.apply(fake, "# Example\n\n**native **[link](https://example.invalid/new)")
        self.assertEqual(fake.blocks[fake.doc_id]["children"], ["manual_text"])
        parts = fake.blocks["manual_text"]["text"]["elements"]
        self.assertTrue(parts[0]["text_run"]["text_element_style"]["bold"])
        self.assertEqual(parts[1]["text_run"]["text_element_style"]["link"]["url"], "https://example.invalid/new")

    def test_large_insert_and_delete_obey_batch_bounds(self):
        fake = FakeDocx("# Example\n\n" + "\n\n".join("old_%s" % i for i in range(110)))
        self.apply(fake, "# Example\n\n" + "\n\n".join("## New_%s" % i for i in range(65)))
        for method, _, body, _ in fake.writes:
            if method == "DELETE":
                self.assertLessEqual(body["end_index"] - body["start_index"], 50)
            if method == "POST":
                self.assertLessEqual(len(body["children"]), 30)

    def test_large_delete_preview_lists_each_original_id_once(self):
        fake = FakeDocx("# Example\n\n" + "\n\n".join("old_%s" % i for i in range(110)))
        result = self.apply(fake, "# Example\n", apply=False)
        listed = [bid for op in result["operations"] for bid in op.get("block_ids", [])]
        self.assertEqual(listed, fake.blocks[fake.doc_id]["children"])

    def test_malformed_baseline_is_safe_failure_without_remote_write(self):
        fake = FakeDocx()
        options = self.prepare(fake, "# Example\n\nnew")
        self.baseline.write_text("[]", encoding="utf-8")
        with self.assertRaises(sync.SyncError):
            sync.handle_sync(fake.doc_id, "[REDACTED_SECRET]", self.target, options, fake)
        self.assertEqual(fake.calls, [])

    def test_merged_table_and_duplicate_references_are_rejected(self):
        fake = FakeDocx("# Example\n\n| A |\n| --- |\n| old |")
        table_id = fake.blocks[fake.doc_id]["children"][0]
        fake.blocks[table_id]["table"]["property"]["merge_info"] = [{"row_span": 2}]
        with self.assertRaises(sync.SyncError):
            self.apply(fake, "# Example\n\n| A |\n| --- |\n| new |")
        self.assertEqual(fake.writes, [])
        fake = FakeDocx()
        fake.blocks[fake.doc_id]["children"] *= 2
        with self.assertRaises(sync.SyncError):
            self.apply(fake, "# Example\n\nnew")
        self.assertEqual(fake.writes, [])

    def test_wrong_wiki_type_and_read_failure_cannot_be_used_as_baseline(self):
        def api(method, path, access_token, body=None, retries=3):
            return {"code": 0, "data": {"node": {"obj_type": "sheet", "obj_token": "other"}}}
        with self.assertRaises(sync.SyncError):
            sync.resolve_document("wiki", "node", "[REDACTED_SECRET]", api)
        fake = FakeDocx()
        def denied(method, path, access_token, body=None, retries=3):
            return {"code": 99991679}
        with self.assertRaises(sync.SyncError):
            sync.fetch_snapshot(fake.doc_id, "[REDACTED_SECRET]", denied)

    def test_partial_success_keeps_checkpoints_and_known_rejection(self):
        fake = FakeDocx("# Example\n\na\n\nb")
        fake.fail_at, fake.fail_response = 2, {"code": 99991679, "msg": "permission denied"}
        with self.assertRaises(sync.SyncError):
            self.apply(fake, "# New title\n\na\n\nchanged")
        journal = json.loads(self.state.read_text())
        self.assertEqual(journal["completed"], [0])
        self.assertEqual(journal["status"], "stopped")
        self.assertIsNone(journal["inflight"])
        self.assertEqual(journal["rejected_operation"]["index"], 1)
        self.assertEqual(len(fake.writes), 2)

    def test_concurrent_change_after_first_write_stops_next_write(self):
        fake = FakeDocx()
        fake.drift_after_write = True
        with self.assertRaises(sync.SyncError):
            self.apply(fake, "# New title\n\nchanged")
        self.assertEqual(len(fake.writes), 1)
        self.assertEqual(json.loads(self.state.read_text())["completed"], [0])

    def test_readback_mismatch_never_claims_success(self):
        fake = FakeDocx()
        fake.corrupt_after_write = True
        with self.assertRaises(sync.SyncError):
            self.apply(fake, "# Example\n\nchanged")
        self.assertEqual(json.loads(self.state.read_text())["status"], "stopped")

    def test_cli_requires_explicit_apply_and_state_and_rejects_unknown_flags(self):
        self.assertFalse(sync.parse_options("sync", ["new.md", "--baseline", "base.json"])["apply"])
        with contextlib.redirect_stderr(io.StringIO()):
            for flags in [["--apply"], ["--apply", "--dry-run"], ["--yes"]]:
                with self.subTest(flags=flags), self.assertRaises(SystemExit):
                    sync.parse_options("sync", ["new.md", "--baseline", "base.json"] + flags)

    def test_process_routes_sync_and_snapshot_without_legacy_mutation(self):
        for action in ["snapshot", "sync"]:
            with self.subTest(action=action), patch.object(index.ds, "run", return_value={"status": "preview"}) as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    index.process(action, "https://example.invalid/docx/example_doc", "[REDACTED_SECRET]", "docx", "example_doc", options={"baseline": "base.json"})
                self.assertEqual(run.call_args.args[0], action)

    def test_main_parses_sync_before_credentials_or_network(self):
        args = ["index.py", "sync", "https://example.invalid/docx/example_doc", "new.md", "--baseline", "base.json"]
        with patch.object(sys, "argv", args), patch.object(index, "get_config", return_value="placeholder"), \
             patch.object(index.os.path, "isfile", return_value=True), \
             patch.object(index, "get_user_access_token", return_value="[REDACTED_SECRET]"), patch.object(index, "process") as process:
            index.main()
        self.assertEqual(process.call_args.args[0], "sync")
        self.assertFalse(process.call_args.args[-1]["apply"])

    def test_existing_read_and_write_process_routes_remain_available(self):
        for action, helper in [("read", "handle_read"), ("write", "handle_write_append"), ("append", "handle_write_append")]:
            with self.subTest(action=action), patch.object(index.da, helper, return_value={"status": "success"}) as handle:
                with contextlib.redirect_stdout(io.StringIO()):
                    index.process(action, "https://example.invalid/docx/example_doc", "[REDACTED_SECRET]", "docx", "example_doc", "source.md")
                self.assertEqual(handle.call_count, 1)

    def test_full_cli_snapshot_preview_apply_uses_only_fake_api(self):
        fake = FakeDocx("# Example\n\nold\n\nkeep", doc_id="cli_document")
        baseline = self.root / "cli-baseline.json"
        desired = self.root / "cli-desired.md"
        state = self.root / "cli-state.json"
        desired.write_text("# Example\n\nnew\n\nkeep", encoding="utf-8")
        url = "https://example.invalid/docx/" + fake.doc_id

        def invoke(args):
            out = io.StringIO()
            with patch.object(sys, "argv", ["index.py"] + args), \
                 patch.object(index, "get_config", return_value="placeholder"), \
                 patch.object(index.os.path, "isfile", return_value=True), \
                 patch.object(index, "get_user_access_token", return_value="[REDACTED_SECRET]"), \
                 patch.object(index, "api_call", fake), contextlib.redirect_stdout(out):
                index.main()
            return json.loads(out.getvalue())

        self.assertEqual(invoke(["snapshot", url, str(baseline)])["status"], "saved")
        args = ["sync", url, str(desired), "--baseline", str(baseline)]
        self.assertEqual(invoke(args)["status"], "preview")
        self.assertEqual(fake.writes, [])
        self.assertEqual(invoke(args + ["--apply", "--state", str(state)])["status"], "verified")
        self.assertEqual(len(fake.writes), 1)
        self.assertEqual(json.loads(state.read_text())["status"], "verified")


if __name__ == "__main__":
    unittest.main()
