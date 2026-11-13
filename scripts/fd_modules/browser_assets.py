import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit

from .doc_actions import ASSET_TYPES


class AssetReadError(Exception):
    pass


BLOCK_ID = re.compile(r"[a-zA-Z0-9_-]+")


def browser_url(parser, doc_url):
    url = urlsplit(doc_url)
    if (url.scheme != "https" or not re.fullmatch(r"[a-zA-Z0-9-]+\.feishu\.cn", url.netloc)
            or not re.fullmatch(r"/(?:docx|wiki)/[a-zA-Z0-9_-]+/?", url.path)):
        parser.error("需要 HTTPS 飞书 docx 或 wiki 文档链接")
    return urlunsplit((url.scheme, url.netloc, url.path, "", ""))


def parse_read_options(doc_url, args):
    parser = argparse.ArgumentParser(prog="feishu-doc read", allow_abbrev=False)
    parser.add_argument("--with-assets", action="store_true")
    parser.add_argument("--block-id", action="append", default=[])
    parser.add_argument("--output-dir", type=Path)
    options = parser.parse_args(args)
    if (options.block_id or options.output_dir is not None) and not options.with_assets:
        parser.error("--block-id 和 --output-dir 需要 --with-assets")
    if options.with_assets:
        options.url = browser_url(parser, doc_url)
        if any(not BLOCK_ID.fullmatch(block_id) for block_id in options.block_id):
            parser.error("--block-id 必须是 read 返回的块 ID")
        if options.output_dir is not None and not options.output_dir.is_dir():
            parser.error("--output-dir 必须是已有目录")
    return options


def parse_options(doc_url, args):
    parser = argparse.ArgumentParser(prog="feishu-doc read-asset", allow_abbrev=False)
    parser.add_argument("--block-id", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--range", dest="cell_range", metavar="X1,Y1:X2,Y2",
                        help="多维表格可见网格内的拖选起点和终点")
    options = parser.parse_args(args)
    url = browser_url(parser, doc_url)
    if not BLOCK_ID.fullmatch(options.block_id):
        parser.error("--block-id 必须是 read 返回的块 ID")
    if options.cell_range and not re.fullmatch(r"\d{1,5},\d{1,5}:\d{1,5},\d{1,5}", options.cell_range):
        parser.error("--range 格式为 X1,Y1:X2,Y2")
    output = options.output.absolute() if options.output else None
    if output is not None and (not output.parent.is_dir() or os.path.lexists(output)):
        parser.error("--output 必须指向已有目录中的新文件")
    return {"block_id": options.block_id, "output": output, "cell_range": options.cell_range,
            "url": url}


def read_asset(doc_token, access_token, options, api_call, check_resp):
    block_id = options["block_id"]
    result = api_call("GET", f"/docx/v1/documents/{doc_token}/blocks/{block_id}", access_token)
    block = check_resp(result, "核对文档资源块").get("block")
    if not isinstance(block, dict) or block.get("block_id") != block_id:
        raise AssetReadError("飞书未返回所指定的块，无法定位资源")

    asset_type = ASSET_TYPES.get(block.get("block_type"))
    if asset_type is None or "divider" in block:
        raise AssetReadError("请选择图片、Excel 附件、内嵌表格、多维表格或画板块")
    output = options["output"]
    cell_range = options.get("cell_range")
    copy_table = asset_type == "bitable" and (output is None or output.suffix.lower() == ".tsv")
    if output is None and not copy_table:
        raise AssetReadError("此资源需要指定 --output")
    if cell_range and not copy_table:
        raise AssetReadError("--range 仅用于多维表格 TSV 复制")
    if asset_type == "file":
        file_data = block.get("file")
        name = file_data.get("name") if isinstance(file_data, dict) else None
        extension = output.suffix.lower()
        if extension not in (".xlsx", ".xls"):
            raise AssetReadError("Excel 附件的 --output 必须以 .xlsx 或 .xls 结尾")
        if isinstance(name, str) and name.strip() and Path(name.strip()).suffix.lower() != extension:
            raise AssetReadError("此文件块不是 Excel 附件")
        asset_type = "excel"
    elif not copy_table and output.suffix.lower() != ".png":
        raise AssetReadError("页面预览的 --output 必须以 .png 结尾")

    args = [options["url"], block_id, asset_type, str(output or "")]
    if cell_range:
        args.append(cell_range)
    return run_browser(args)


def read_assets(assets, options):
    selected_ids = set(options.block_id)
    available_ids = {asset["blockId"] for asset in assets}
    results = [{"blockId": block_id, "error": {"code": "BLOCK_NOT_IN_DOCUMENT", "message": "文档中没有该资源块。"}}
               for block_id in dict.fromkeys(options.block_id) if block_id not in available_ids]
    selected = [asset for asset in assets if not selected_ids or asset["blockId"] in selected_ids]
    supported = []
    for asset in selected:
        block_id, kind = asset["blockId"], asset["type"]
        if kind == "file":
            if block_id in selected_ids:
                results.append({"blockId": block_id, "type": kind,
                                "error": {"code": "UNSUPPORTED_ASSET", "message": "仅支持已识别的 Excel 附件。"}})
            continue
        if not BLOCK_ID.fullmatch(block_id):
            results.append({"blockId": block_id, "type": kind,
                            "error": {"code": "INVALID_BLOCK_ID", "message": "飞书返回的资源块 ID 无效。"}})
            continue
        supported.append(asset)
    if not supported:
        return results

    try:
        directory = (options.output_dir.absolute() if options.output_dir is not None
                     else Path(tempfile.mkdtemp(prefix="feishu-assets-")))
    except OSError as error:
        raise AssetReadError("无法创建资源输出目录。") from error
    jobs = []
    for asset in supported:
        block_id, kind = asset["blockId"], asset["type"]
        suffix = Path(asset["name"].strip()).suffix.lower() if kind == "excel" else (".tsv" if kind == "bitable" else ".png")
        job = {"blockId": block_id, "type": kind, "output": str(directory / f"{block_id}{suffix}")}
        if kind == "bitable":
            job["fallbackOutput"] = str(directory / f"{block_id}.png")
        jobs.append(job)
    batch = run_browser(["--batch", options.url], jobs)
    if (not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch)
            or [item.get("blockId") for item in batch] != [job["blockId"] for job in jobs]):
        raise AssetReadError("浏览器批量读取结果与所选资源不一致。")
    return batch + results


def run_browser(args, jobs=None):
    script = Path(__file__).resolve().parents[1] / "read_browser_asset.mjs"
    env = {key: value for key, value in os.environ.items() if key not in {"FEISHU_APP_ID", "FEISHU_APP_SECRET"}}
    payload = {} if jobs is None else {"input": json.dumps(jobs)}
    try:
        result = subprocess.run(
            ["node", str(script), *args],
            stdout=subprocess.PIPE, text=True, env=env, **payload,
        )
    except FileNotFoundError as error:
        raise AssetReadError("浏览器读取需要 Node.js 20 或以上版本") from error
    if result.returncode:
        raise AssetReadError("浏览器未能读取指定资源，请查看上方原因")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise AssetReadError("浏览器读取返回无效结果，请核对本地文件后再试") from error
