import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import index


class BrowserAssetTests(unittest.TestCase):
    url = "https://example.feishu.cn/wiki/doc123"

    def test_bitable_accepts_only_document_and_block_id(self):
        options = index.ba.parse_options(self.url, ["--block-id", "table1"])
        response = {"code": 0, "data": {"block": {"block_type": 18, "block_id": "table1"}}}
        completed = subprocess.CompletedProcess([], 0, '{"mode": "clipboard"}')
        with patch.object(index.ba.subprocess, "run", return_value=completed) as browser:
            result = index.ba.read_asset("doc123", "test-token", options, lambda *_: response, index.check_resp)
        self.assertEqual(browser.call_args.args[0][2:], [self.url, "table1", "bitable", ""])
        self.assertEqual(result["mode"], "clipboard")

    def test_reads_one_confirmed_block_and_drops_query_and_app_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "board.png"
            url = self.url + "?from=copy#heading"
            options = index.ba.parse_options(url, ["--block-id", "board1", "--output", str(target)])
            response = {"code": 0, "data": {"block": {"block_type": 43, "block_id": "board1"}}}
            result = {"blockId": "board1", "type": "whiteboard", "mode": "visible-preview", "path": str(target)}
            with patch.object(index, "api_call", return_value=response) as api, \
                 patch.dict(index.ba.os.environ, {"FEISHU_APP_ID": "test-id", "FEISHU_APP_SECRET": "test-secret"}), \
                 patch.object(index.ba.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(result))) as browser, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                index.process("read-asset", url, "test-token", "wiki", "doc123", options=options)

            api.assert_called_once_with("GET", "/docx/v1/documents/doc123/blocks/board1", "test-token")
            args, kwargs = browser.call_args
            self.assertEqual(args[0][2:], [self.url, "board1", "whiteboard", str(target)])
            self.assertNotIn("FEISHU_APP_ID", kwargs["env"])
            self.assertNotIn("FEISHU_APP_SECRET", kwargs["env"])
            self.assertEqual(json.loads(output.getvalue()), result)

    def test_ordinary_read_never_invokes_browser_even_with_detected_bitable(self):
        responses = [
            {"code": 0, "data": {"content": "正文"}},
            {"code": 0, "data": {"items": [{"block_type": 18, "block_id": "table1"}]}},
        ]
        with patch.object(index, "api_call", side_effect=responses) as api, \
             patch.object(index.ba.subprocess, "run") as browser, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            index.process("read", self.url, "test-token", "wiki", "doc123")

        self.assertEqual(api.call_count, 2)
        browser.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["assets"][0]["type"], "bitable")

    def test_wrong_or_unsupported_block_cannot_start_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            options = index.ba.parse_options(self.url, ["--block-id", "selected", "--output", f"{directory}/out.png"])
            for block in ({"block_type": 27, "block_id": "other"}, {"block_type": 2, "block_id": "selected"}):
                with self.subTest(block=block), patch.object(index.ba.subprocess, "run") as browser:
                    with self.assertRaises(index.ba.AssetReadError):
                        index.ba.read_asset("doc123", "test-token", options,
                                            lambda *_: {"code": 0, "data": {"block": block}}, index.check_resp)
                    browser.assert_not_called()
