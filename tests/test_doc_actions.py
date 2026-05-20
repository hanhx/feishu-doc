import os
import contextlib
import io
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from fd_modules.doc_actions import handle_read


class HandleReadTests(unittest.TestCase):
    def test_wiki_url_uses_wiki_action_name_for_error_messages(self):
        def api_call(method, path, access_token, body=None):
            return {"code": 99991679, "msg": "Unauthorized"}

        def check_resp(resp, action_name, auto_retry_login=False):
            self.assertEqual(action_name, "获取 wiki 文档内容")
            raise SystemExit(1)

        with self.assertRaises(SystemExit):
            handle_read(
                "https://nio.feishu.cn/wiki/wiki123",
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
                    "https://nio.feishu.cn/docs/doccn123",
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
