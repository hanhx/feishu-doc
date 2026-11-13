import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit, urlunsplit

from .doc_actions import ASSET_TYPES


class AssetReadError(Exception):
    pass


def parse_options(doc_url, args):
    parser = argparse.ArgumentParser(prog="feishu-doc read-asset", allow_abbrev=False)
    parser.add_argument("--block-id", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--range", dest="cell_range", metavar="X1,Y1:X2,Y2",
                        help="多维表格可见网格内的拖选起点和终点")
    options = parser.parse_args(args)
    url = urlsplit(doc_url)
    if (url.scheme != "https" or not re.fullmatch(r"[a-zA-Z0-9-]+\.feishu\.cn", url.netloc)
            or not re.fullmatch(r"/(?:docx|wiki)/[a-zA-Z0-9_-]+/?", url.path)):
        parser.error("需要 HTTPS 飞书 docx 或 wiki 文档链接")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", options.block_id):
        parser.error("--block-id 必须是 read 返回的块 ID")
    if options.cell_range and not re.fullmatch(r"\d{1,5},\d{1,5}:\d{1,5},\d{1,5}", options.cell_range):
        parser.error("--range 格式为 X1,Y1:X2,Y2")
    output = options.output.absolute() if options.output else None
    if output is not None and (not output.parent.is_dir() or os.path.lexists(output)):
        parser.error("--output 必须指向已有目录中的新文件")
    return {"block_id": options.block_id, "output": output, "cell_range": options.cell_range,
            "url": urlunsplit((url.scheme, url.netloc, url.path, "", ""))}


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

    script = Path(__file__).resolve().parents[1] / "read_browser_asset.mjs"
    env = {key: value for key, value in os.environ.items() if key not in {"FEISHU_APP_ID", "FEISHU_APP_SECRET"}}
    try:
        command = ["node", str(script), options["url"], block_id, asset_type, str(output or "")]
        if cell_range:
            command.append(cell_range)
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE, text=True, env=env,
        )
    except FileNotFoundError as error:
        raise AssetReadError("浏览器读取需要 Node.js 20 或以上版本") from error
    if result.returncode:
        raise AssetReadError("浏览器未能读取指定资源，请查看上方原因")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise AssetReadError("浏览器读取返回无效结果，请核对本地文件后再试") from error
