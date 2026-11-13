import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from index import check_resp
import index


class MermaidDocumentTests(unittest.TestCase):
    def test_authoring_commands_submit_editable_mermaid_and_read_returns_source(self):
        source = 'flowchart LR\n    A["预览"] --> B["提交"]'
        fence = f"```mermaid\n{source}\n```"
        heading = dict(index.db.make_heading_block(2, "建单方案"), block_id="heading")
        root = {"block_type": 1, "block_id": "doc", "children": ["heading"]}
        url = "https://example.feishu.cn/docx/doc"
        children = []

        def api_call(method, path, access_token, body=None):
            if method == "GET" and path.endswith("/raw_content"):
                return {"code": 0, "data": {}}
            if method == "GET" and "/blocks?" in path:
                return {"code": 0, "data": {"items": [root, heading, *children]}}
            if method == "GET" and path.endswith("/blocks/doc"):
                return {"code": 0, "data": {"block": root}}
            if method == "POST" and path.endswith("/blocks/doc/children"):
                children.extend(body["children"])
                return {"code": 0, "data": {"children": body["children"]}}
            if method in ("PATCH", "DELETE") and action == "write":
                return {"code": 0}
            self.fail(f"Unexpected API call: {method} {path}")

        with tempfile.TemporaryDirectory() as directory, patch.object(index, "api_call", api_call):
            content = Path(directory) / "design.md"
            content.write_text(f"{fence}\n\n```python\nprint('flowchart')\n```\n\n```\nflowchart\n```", encoding="utf-8")
            for action in ("write", "append", "insert-targeted"):
                with self.subTest(action=action), contextlib.redirect_stdout(io.StringIO()):
                    children.clear()
                    index.process(action, url, "test-token", "docx", "doc", content,
                                  {"anchor": "建单方案", "yes": True})
                    self.assertEqual([block["block_type"] for block in children], [40, 14, 14])
                    add_on = children[0]["add_ons"]
                    self.assertEqual(add_on["component_type_id"], "blk_631fefbbae02400430b8f9f4")
                    self.assertEqual(json.loads(add_on["record"]), {"data": source, "theme": "default", "view": "chart"})

            with contextlib.redirect_stdout(io.StringIO()) as output:
                index.process("read", url, "test-token", "docx", "doc")
            self.assertIn(fence, json.loads(output.getvalue())["markdown"])

    def test_unrelated_or_malformed_widget_is_not_read_as_mermaid(self):
        for component_id, record in (("other", '{"data":"flowchart LR"}'),
                                     (index.db.MERMAID_COMPONENT_TYPE_ID, "{invalid")):
            with self.subTest(component_id=component_id):
                block = {"block_type": 40, "add_ons": {"component_type_id": component_id, "record": record}}
                self.assertEqual(index.db.block_to_md(block), "")


class CheckRespTests(unittest.TestCase):
    def test_permission_error_shows_unified_permission_hint(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                check_resp({"code": 99991679, "msg": "Unauthorized"}, "获取 wiki 文档内容")

        output = stderr.getvalue()
        self.assertIn("docx:document + docx:document:readonly", output)
        self.assertIn("权限不足", output)

if __name__ == "__main__":
    unittest.main()
