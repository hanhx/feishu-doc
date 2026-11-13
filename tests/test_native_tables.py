import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_diff_sync import FakeDocx, db, index, sync
from fd_modules.native_tables import NativeTableError


LONG_TABLE = "| 编号 | 内容 |\n| --- | --- |\n" + "\n".join(
    f"| {number} | " + (r"\| **正文** |" if number == 1 else "|") for number in range(1, 13)
)


class TableWriterDocx(FakeDocx):
    def __init__(self):
        super().__init__("# Example\n\n## 锚点\n\n旧正文")
        self.initial_shapes = []
        self.row_appends = []
        self.fail_row_append = False

    def __call__(self, method, path, access_token, body=None, retries=3):
        parsed = urlsplit(path)
        suffix = parsed.path.split(self.doc_id, 1)[1]
        query = parse_qs(parsed.query)
        root = self.blocks[self.doc_id]
        if method == "GET" and suffix == f"/blocks/{self.doc_id}":
            return {"code": 0, "data": {"block": copy.deepcopy(root)}}
        if method == "PATCH" and suffix == f"/blocks/{self.doc_id}":
            return {"code": 0}  # Whole-document write clears the title first.
        if method == "DELETE" and suffix == f"/blocks/{self.doc_id}/children/batch_delete":
            path += f"?document_revision_id={self.rev}"
        if method == "GET" and suffix.startswith("/blocks/") and suffix.endswith("/children"):
            cells = self.blocks[suffix.split("/")[2]]["table"]["cells"]
            start = int(query.get("page_token", [0])[0])
            end = start + 8
            return {"code": 0, "data": {"items": [self.blocks[c] for c in cells[start:end]],
                                         "has_more": end < len(cells), "page_token": str(end)}}
        if method == "POST" and suffix == f"/blocks/{self.doc_id}/children":
            ids = []
            for definition in body["children"]:
                if definition["block_type"] == 31:
                    assert query["document_revision_id"] == [str(self.rev)] and retries == 1
                    shape = definition["table"]["property"]
                    self.initial_shapes.append(shape["row_size"])
                    rows = [[""] * shape["column_size"] for _ in range(shape["row_size"])]
                    definition = {"table_rows": rows}
                ids.append(self.add(definition))
            position = body["index"] if body["index"] >= 0 else len(root["children"])
            root["children"][position:position] = ids
            self.rev += 1
            return {"code": 0, "data": {"children": [copy.deepcopy(self.blocks[b]) for b in ids],
                                         "document_revision_id": self.rev}}
        if method == "PATCH" and "insert_table_row" in body:
            assert query["document_revision_id"] == [str(self.rev)] and retries == 1
            assert body == {"insert_table_row": {"row_index": -1}}
            table_id = suffix.split("/")[2]
            self.row_appends.append(table_id)
            if self.fail_row_append:
                return {"code": -1}
            block = self.blocks[table_id]
            table = block["table"]
            for _ in range(table["property"]["column_size"]):
                text_id = self.add(db.make_text_block(""))
                cell_id = self.add({"block_type": 32, "children": [text_id]})
                block["children"].append(cell_id)
                table["cells"].append(cell_id)
            table["property"]["row_size"] += 1
            self.rev += 1
            return {"code": 0, "data": {"block": copy.deepcopy(block), "document_revision_id": self.rev}}
        return super().__call__(method, path, access_token, body, retries)


class NativeTableTests(unittest.TestCase):
    def test_authoring_commands_write_one_long_table_with_all_cells_in_order(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(index.da.time, "sleep"):
            content = Path(directory) / "design.md"
            content.write_text(f"表前\n\n{LONG_TABLE}\n\n表后", encoding="utf-8")
            for action in ("write", "append", "insert-targeted"):
                fake = TableWriterDocx()
                url = f"https://example.feishu.cn/docx/{fake.doc_id}"
                with self.subTest(action=action), patch.object(index, "api_call", fake), contextlib.redirect_stdout(io.StringIO()):
                    index.process(action, url, "test-token", "docx", fake.doc_id, content,
                                  {"anchor": "锚点", "yes": True})
                children = fake.blocks[fake.doc_id]["children"]
                tables = [fake.blocks[block_id] for block_id in children if fake.blocks[block_id]["block_type"] == 31]
                self.assertEqual(len(tables), 1)
                table = tables[0]
                self.assertEqual(fake.initial_shapes, [9])
                self.assertEqual(fake.row_appends, [table["block_id"]] * 4)
                self.assertEqual(table["table"]["property"]["row_size"], 13)
                position = children.index(table["block_id"])
                self.assertEqual(db.extract_block_text(fake.blocks[children[position - 1]]), "表前")
                self.assertEqual(db.extract_block_text(fake.blocks[children[position + 1]]), "表后")
                texts = [fake.blocks[fake.blocks[c]["children"][0]] for c in table["table"]["cells"]]
                self.assertEqual([db.extract_block_text(b) for b in texts[::2]], ["编号", *map(str, range(1, 13))])
                self.assertEqual(db.extract_block_text(texts[3]), "| 正文")
                self.assertTrue(texts[3]["text"]["elements"][-1]["text_run"]["text_element_style"]["bold"])
                self.assertFalse(db.extract_block_text(texts[-1]).strip())

    def test_uncertain_row_append_stops_without_repeating_or_splitting(self):
        fake = TableWriterDocx()
        fake.fail_row_append = True
        rows = [[str(n)] for n in range(13)]
        with self.assertRaises(NativeTableError):
            index.da.native_tables.write_table(fake.doc_id, "test-token", fake.doc_id, -1, rows, fake)
        self.assertEqual(fake.initial_shapes, [9])
        self.assertEqual(len(fake.row_appends), 1)

    def test_sync_preserves_single_and_previously_split_table_shapes(self):
        markdown = "# Example\n\n" + LONG_TABLE
        header, delimiter, *data = LONG_TABLE.splitlines()
        legacy = "# Example\n\n" + "\n\n".join(
            "\n".join([header, delimiter, *part]) for part in (data[:8], data[8:])
        )
        for existing in (markdown, legacy):
            fake = FakeDocx(existing)
            for block in fake.blocks.values():
                if block["block_type"] == 31:
                    block["table"]["property"]["merge_info"] = [
                        {"row_span": 1, "col_span": 1} for _ in block["table"]["cells"]
                    ]
            plan = sync.build_plan(fake.snapshot(), markdown.replace("| 12 | |", "| 12 | tail |"))
            self.assertEqual([op["method"] for op in plan["operations"]], ["PATCH"])
