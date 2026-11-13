import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import unquote, urlsplit, urlunsplit
from xml.etree import ElementTree
import zlib

from . import doc_blocks, targeted_ops


class DrawioImportError(Exception):
    pass


def parse_source(source):
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", source.replace(b"\x00", b""), re.IGNORECASE):
        raise ValueError(".drawio 源文件不能包含 DTD 或实体声明")
    return ElementTree.fromstring(source)


def has_drawable_cells(document):
    pages = document.findall("diagram") if document.tag == "mxfile" else [document]
    for page in pages:
        model = page.find("mxGraphModel") if page.tag == "diagram" else page
        if model is None and (page.text or "").strip():
            try:
                inflater = zlib.decompressobj(-zlib.MAX_WBITS)
                compressed = base64.b64decode("".join(page.text.split()), validate=True)
                xml = inflater.decompress(compressed, 32 * 1024 * 1024)
                if not inflater.eof:
                    continue
                model = parse_source(unquote(xml.decode("utf-8")).encode("utf-8"))
            except (ValueError, ElementTree.ParseError, zlib.error):
                continue
        if model is not None and model.tag == "mxGraphModel" and any(
            cell.get("vertex") == "1" or cell.get("edge") == "1" for cell in model.iter("mxCell")
        ):
            return True
    return False


def parse_options(doc_url, args):
    parser = argparse.ArgumentParser(prog="feishu-doc import-drawio", allow_abbrev=False)
    parser.add_argument("source", type=Path)
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--anchor", help="章节标题全文")
    location.add_argument("--append", action="store_true", help="追加到文末")
    location.add_argument("--replace-block-id", help="原位替换指定画板的全部图形")
    parser.add_argument("--output", type=Path, required=True, help="新建的 PNG 截图路径")
    parser.add_argument("--yes", action="store_true", help="执行已获授权的写入")
    options = parser.parse_args(args)
    url = urlsplit(doc_url)
    if (url.scheme != "https" or not re.fullmatch(r"[a-z0-9-]+\.feishu\.cn", url.netloc)
            or not re.fullmatch(r"/(?:wiki|docx)/[a-zA-Z0-9_-]+/?", url.path)):
        parser.error("需要 HTTPS 飞书 wiki 或 docx 链接")
    if options.anchor is not None and not options.anchor.strip():
        parser.error("--anchor 需要非空章节标题")
    if options.replace_block_id is not None and not re.fullmatch(r"[a-zA-Z0-9_-]+", options.replace_block_id):
        parser.error("--replace-block-id 需要有效的画板块 ID")
    if options.source.suffix.lower() != ".drawio" or not options.source.is_file():
        parser.error("需要本地 .drawio 源文件")
    try:
        source = options.source.read_bytes()
        document = parse_source(source)
    except ValueError as error:
        parser.error(str(error))
    except (OSError, ElementTree.ParseError):
        parser.error("无法读取 .drawio 源文件")
    if document.tag != "mxGraphModel" and (document.tag != "mxfile" or document.find("diagram") is None):
        parser.error("需要有效的 draw.io 源文件")
    if options.replace_block_id and not has_drawable_cells(document):
        parser.error("替换用的 .drawio 源文件必须包含可绘制图形")
    options.source_digest = hashlib.sha256(source).hexdigest()
    if (options.output.suffix.lower() != ".png" or not options.output.parent.is_dir()
            or os.path.lexists(options.output)):
        parser.error("--output 必须是已有目录中的新 PNG 文件")
    return options, urlunsplit((url.scheme, url.netloc, url.path, "", ""))


def document_children(doc_token, access_token, api_call, check_resp):
    items, blocks = targeted_ops.fetch_all_blocks(doc_token, access_token, api_call, check_resp)
    roots = [item for item in items if item.get("block_type") == 1]
    if len(roots) != 1:
        raise DrawioImportError("未能定位唯一的文档根块")
    children = roots[0].get("children") or []
    if not children or any(child not in blocks for child in children):
        raise DrawioImportError("无法读取完整的文档顶层块，无法确认插入位置")
    return children, blocks


def insertion_request(children, blocks, options):
    if options.append:
        anchor = children[-1]
    else:
        matches = [block_id for block_id in children
                   if targeted_ops.heading_text(blocks[block_id], doc_blocks.extract_text) == options.anchor.strip()]
        if len(matches) != 1:
            raise DrawioImportError("章节标题必须在文档顶层精确匹配一次，请核对标题")
        anchor = matches[0]

    index = children.index(anchor)
    next_block = children[index + 1] if index + 1 < len(children) else ""
    return {
        "anchor": anchor,
        "heading": options.anchor.strip() if options.anchor else "",
        "nextBlock": next_block,
        "existingBoards": [block_id for block_id in children if blocks[block_id].get("block_type") == 43],
        "reuseEmpty": options.append and blocks[anchor].get("block_type") == 2
        and not doc_blocks.extract_text(blocks[anchor].get("text", {}).get("elements", [])).strip("\u200b \r\n\t"),
    }


def recover_saved_board(read_children, original, request, paragraph_id):
    if not isinstance(paragraph_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", paragraph_id):
        raise DrawioImportError("无法核对插入段落，请回读文档后处理")
    original_ids = set(original)
    anchor = request["anchor"]
    position = original.index(anchor) + (0 if request["reuseEmpty"] else 1)
    following = original[position + 1:] if request["reuseEmpty"] else original[position:]
    prefix = original[:position]
    for attempt in range(3):
        if attempt:
            time.sleep(0.6)
        children, blocks = read_children()
        if children[:position] != prefix or (following and children[-len(following):] != following):
            continue
        inserted = children[position:len(children) - len(following)]
        candidates = [block_id for block_id in inserted
                      if block_id not in original_ids and blocks[block_id].get("block_type") == 43]
        if len(candidates) != 1:
            continue
        board_id = candidates[0]
        if len(inserted) == 1:
            return board_id
        # Feishu may keep the insertion paragraph or leave a blank paragraph at the end.
        if len(inserted) == 2:
            extra = next(block_id for block_id in inserted if block_id != board_id)
            if extra != paragraph_id and (following or inserted[-1] != extra):
                continue
            paragraph = blocks[extra]
            text = doc_blocks.extract_text(paragraph.get("text", {}).get("elements", []))
            if extra not in original_ids and paragraph.get("block_type") == 2 and not text.strip("\u200b \r\n\t"):
                return board_id
    raise DrawioImportError("文档回读未确认指定位置的唯一新画板，请核对现有块后继续，避免重复创建")


def import_drawio(doc_token, access_token, options, url, api_call, check_resp):
    if options.replace_block_id:
        target = options.replace_block_id
        response = api_call("GET", f"/docx/v1/documents/{doc_token}/blocks/{target}", access_token)
        block = check_resp(response, "核对目标画板").get("block")
        if not isinstance(block, dict) or block.get("block_id") != target or block.get("block_type") != 43:
            raise DrawioImportError("指定的块不是该文档中的画板")
        request = {"anchor": target, "replace": True, "sourceDigest": options.source_digest}
        position = "replace_board_contents"
    else:
        def read_children():
            return document_children(doc_token, access_token, api_call, check_resp)

        children, blocks = read_children()
        request = {**insertion_request(children, blocks, options), "sourceDigest": options.source_digest}
        position = "end" if options.append else "after_heading"
    targeted_ops.confirm_with_preview(
        {"docUrl": url, "section": options.anchor, "position": position, "anchorBlockId": request["anchor"],
         "source": str(options.source.absolute()), "output": str(options.output.absolute())},
        options.yes, "import-drawio",
    )
    script = Path(__file__).resolve().parents[1] / "import_drawio.mjs"
    env = {key: value for key, value in os.environ.items() if key not in {"FEISHU_APP_ID", "FEISHU_APP_SECRET"}}
    command = ["node", str(script), url, str(options.source.absolute()), str(options.output.absolute())]

    def run_browser(browser_request):
        try:
            result = subprocess.run(command, input=json.dumps(browser_request), stdout=subprocess.PIPE,
                                    text=True, env=env)
        except FileNotFoundError as error:
            raise DrawioImportError("需要 Node.js 20 或以上版本") from error
        if result.returncode:
            raise DrawioImportError("导入流程未完成，请根据浏览器提示核对文档状态")
        try:
            outcome = json.loads(result.stdout)
            if isinstance(outcome, dict):
                return outcome
        except ValueError as error:
            raise DrawioImportError("无法读取导入结果，请先回读文档核对画板") from error
        raise DrawioImportError("无法读取导入结果，请先回读文档核对画板")

    result = run_browser(request)
    if result.get("status") != "locate_saved_board":
        return result
    if options.replace_block_id:
        raise DrawioImportError("替换模式收到意外的新建结果，请回读目标画板")
    board_id = recover_saved_board(read_children, children, request, result.get("paragraphId"))
    return run_browser({"anchor": board_id, "resume": True, "sourceDigest": options.source_digest})
