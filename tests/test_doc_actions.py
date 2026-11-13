import os
import contextlib
import io
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from fd_modules.doc_actions import handle_read
from fd_modules.doc_blocks import block_to_md, collect_descendant_ids


class HandleReadTests(unittest.TestCase):
    def read_pages(self, pages):
        base = "/docx/v1/documents/doc123"
        responses = {f"{base}/raw_content": {"content": "原文"}}
        page_path = f"{base}/blocks?page_size=500"
        for page in pages:
            responses[page_path] = page
            page_path = f"{base}/blocks?page_size=500&page_token={page.get('page_token')}"

        def api_call(method, path, access_token):
            self.assertEqual(method, "GET")
            return {"code": 0, "data": responses.pop(path)}

        def check_resp(resp, action_name, auto_retry_login=False):
            self.assertEqual(resp["code"], 0)
            return resp["data"]

        result = handle_read(
            "https://example.feishu.cn/docx/doc123", "docx", "doc123", "test-token",
            api_call, check_resp, block_to_md, collect_descendant_ids,
        )
        self.assertEqual(responses, {})
        return result

    def test_text_only_returns_empty_assets_using_existing_requests(self):
        blocks = [{"block_type": 1, "block_id": "doc123", "page": {"elements": [
            {"text_run": {"content": "方案"}},
        ]}}, {"block_type": 22, "block_id": "divider", "divider": {}}]
        result = self.read_pages([{"items": blocks}])
        self.assertEqual(result["assets"], [])
        self.assertEqual(result["markdown"], "# 方案\n---")

    def test_assets_keep_order_across_pages_and_include_nested_images(self):
        def heading(level, text):
            return {
                "block_id": text, "block_type": level + 2,
                f"heading{level}": {"elements": [{"text_run": {"content": text}}]},
            }

        result = self.read_pages([
            {"items": [heading(1, "方案"), heading(2, "流程"),
                       {"block_id": "callout", "block_type": 19, "children": ["image"]},
                       {"block_id": "image", "block_type": 27, "image": {"token": "media"}}],
             "has_more": True, "page_token": "next"},
            {"items": [{"block_id": "board", "block_type": 43}, heading(2, "数据"),
                       {"block_id": "xlsx", "block_type": 23, "file": {"name": "清单.XLSX"}},
                       {"block_id": "unknown", "block_type": 23, "file": {}},
                       {"block_id": "sheet", "block_type": 30}]},
        ])
        self.assertEqual(result["assets"], [
            {"type": "image", "blockId": "image", "blockIndex": 4, "sectionPath": ["方案", "流程"]},
            {"type": "whiteboard", "blockId": "board", "blockIndex": 5, "sectionPath": ["方案", "流程"]},
            {"type": "excel", "blockId": "xlsx", "blockIndex": 7, "sectionPath": ["方案", "数据"], "name": "清单.XLSX"},
            {"type": "file", "blockId": "unknown", "blockIndex": 8, "sectionPath": ["方案", "数据"]},
            {"type": "sheet", "blockId": "sheet", "blockIndex": 9, "sectionPath": ["方案", "数据"]},
        ])
        self.assertEqual(result["markdown"], "# 方案\n## 流程\n[画板]\n## 数据\n[文件]\n[文件]\n[电子表格]")

    def test_wiki_url_uses_wiki_action_name_for_error_messages(self):
        def api_call(method, path, access_token, body=None):
            return {"code": 99991679, "msg": "Unauthorized"}

        def check_resp(resp, action_name, auto_retry_login=False):
            self.assertEqual(action_name, "获取 wiki 文档内容")
            raise SystemExit(1)

        with self.assertRaises(SystemExit):
            handle_read(
                "https://example.feishu.cn/wiki/wiki123",
                "wiki",
                "wiki123",
                "token",
                api_call,
                check_resp,
                lambda item, block_map: None,
                lambda block_id, block_map: [],
            )

    def test_docs_url_is_rejected_without_api_call(self):
        calls = []

        def api_call(method, path, access_token, body=None):
            calls.append((method, path, access_token, body))
            return {"code": 0, "data": {"content": "不应调用"}}

        def check_resp(resp, action_name, auto_retry_login=False):
            self.fail("docs/doccn 链接不应调用 check_resp")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                handle_read(
                    "https://example.feishu.cn/docs/doccn123",
                    "docs",
                    "doccn123",
                    "token",
                    api_call,
                    check_resp,
                    lambda item, block_map: None,
                    lambda block_id, block_map: [],
                )

        self.assertEqual(calls, [])
        output = stderr.getvalue()
        self.assertIn("暂不支持旧版 docs/doccn 文档", output)
        self.assertIn("请将文档转为新版 docx 后再读取", output)


if __name__ == "__main__":
    unittest.main()
