import contextlib
import io
import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from index import check_resp


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
