import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fd_modules import doc_blocks, drawio_import


class DrawioImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / "sample.drawio"
        self.source.write_text(
            '<mxfile><diagram><mxGraphModel><root><mxCell id="2" vertex="1"/></root></mxGraphModel></diagram></mxfile>',
            encoding="utf-8",
        )
        self.output = Path(directory.name) / "preview.png"
        self.url = "https://example.feishu.cn/wiki/fixture"
        self.heading = dict(doc_blocks.make_heading_block(1, "架构方案"), block_id="section")
        self.end = dict(doc_blocks.make_text_block(""), block_id="end")
        self.root = {"block_id": "root", "block_type": 1, "children": ["section", "end"]}

    def run_import(self, *location):
        options, url = drawio_import.parse_options(
            self.url, [str(self.source), *location, "--output", str(self.output), "--yes"]
        )
        return drawio_import.import_drawio(
            "fixture", "test-token", options, url, self.api_call, lambda response, *_args, **_kw: response
        )

    def api_call(self, method, url, _token):
        self.assertEqual(method, "GET")
        if url.endswith("/blocks/board"):
            return {"block": {"block_id": "board", "block_type": 43}}
        if url.endswith("/blocks/end"):
            return {"block": self.end}
        self.assertTrue(url.startswith("/docx/v1/documents/fixture/blocks?"))
        items = [self.root, self.heading, self.end]
        for block_id in self.root["children"]:
            if block_id.startswith("newboard"):
                items.append({"block_id": block_id, "block_type": 43})
            if block_id == "newparagraph":
                items.append(dict(self.end, block_id=block_id))
        if "duplicate" in self.root["children"]:
            items.append(dict(self.heading, block_id="duplicate"))
        return {"items": items}

    @patch("fd_modules.drawio_import.targeted_ops.confirm_with_preview")
    @patch("fd_modules.drawio_import.subprocess.run")
    def test_uses_read_only_document_api_and_passes_exact_position_to_browser(self, run, _confirm):
        run.return_value.returncode = 0
        run.return_value.stdout = '{"status":"imported","blockId":"newboard"}'
        for location, anchor, following, reuse in (
            (("--anchor", "架构方案"), "section", "end", False),
            (("--append",), "end", "", True),
        ):
            with self.subTest(location=location):
                self.assertEqual(self.run_import(*location)["blockId"], "newboard")
                request = json.loads(run.call_args.kwargs["input"])
                self.assertEqual((request["anchor"], request["nextBlock"], request["reuseEmpty"]),
                                 (anchor, following, reuse))
                self.assertEqual(run.call_args.args[0][2], self.url)

    @patch("fd_modules.drawio_import.subprocess.run")
    def test_ambiguous_heading_does_not_open_browser(self, run):
        self.root["children"].append("duplicate")
        with self.assertRaisesRegex(drawio_import.DrawioImportError, "精确匹配一次"):
            self.run_import("--anchor", "架构方案")
        run.assert_not_called()

    @patch("fd_modules.drawio_import.time.sleep")
    @patch("fd_modules.drawio_import.subprocess.run")
    def test_recovers_saved_board_once_at_the_original_position(self, run, _sleep):
        for location, new_order, recoverable in (
            (("--anchor", "架构方案"), ["section", "newboard", "end"], True),
            (("--anchor", "架构方案"), ["section", "newparagraph", "newboard", "end"], True),
            (("--append",), ["section", "newboard", "newparagraph"], True),
            (("--anchor", "架构方案"), ["section", "end", "newboard"], False),
            (("--anchor", "架构方案"), ["section", "newboard", "newboard2", "end"], False),
        ):
            with self.subTest(location=location, new_order=new_order):
                self.root["children"] = ["section", "end"]
                run.reset_mock()

                def browser_call(_command, *, input, **_kwargs):
                    request = json.loads(input)
                    if run.call_count == 1:
                        self.assertNotIn("resume", request)
                        self.root["children"] = new_order
                        run.return_value.stdout = '{"status":"locate_saved_board","paragraphId":"newparagraph"}'
                    else:
                        self.assertEqual(request, {"anchor": "newboard", "resume": True,
                                                   "sourceDigest": hashlib.sha256(self.source.read_bytes()).hexdigest()})
                        run.return_value.stdout = '{"status":"imported","blockId":"newboard"}'
                    run.return_value.returncode = 0
                    return run.return_value

                run.side_effect = browser_call
                if recoverable:
                    self.assertEqual(self.run_import(*location)["blockId"], "newboard")
                    self.assertEqual(run.call_count, 2)
                else:
                    with self.assertRaisesRegex(drawio_import.DrawioImportError, "唯一新画板"):
                        self.run_import(*location)
                    self.assertEqual(run.call_count, 1)

    @patch("fd_modules.drawio_import.targeted_ops.confirm_with_preview")
    @patch("fd_modules.drawio_import.subprocess.run")
    def test_replacement_only_accepts_a_whiteboard_block(self, run, confirm):
        run.return_value.returncode = 0
        run.return_value.stdout = '{"status":"replaced","blockId":"board"}'
        with self.assertRaisesRegex(drawio_import.DrawioImportError, "不是该文档中的画板"):
            self.run_import("--replace-block-id", "end")
        run.assert_not_called()
        confirm.assert_not_called()
        self.assertEqual(self.run_import("--replace-block-id", "board")["blockId"], "board")
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {
            "anchor": "board", "replace": True, "sourceDigest": hashlib.sha256(self.source.read_bytes()).hexdigest()
        })
        self.assertEqual(confirm.call_args.args[0]["position"], "replace_board_contents")

    @patch("fd_modules.drawio_import.subprocess.run")
    def test_empty_replacement_source_does_not_open_browser(self, run):
        self.source.write_text("<mxfile><diagram/></mxfile>", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.run_import("--replace-block-id", "board")
        run.assert_not_called()
