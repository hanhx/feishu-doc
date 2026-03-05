#!/usr/bin/env python3

import sys
import os
import json
import time
import re
import urllib.request
import urllib.error

from fd_modules import doc_actions as da
from fd_modules import doc_blocks as db
from fd_modules import targeted_ops as ops

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FEISHU_FILE = os.path.join(SCRIPT_DIR, "..", "assets", ".feishu")
FEISHU_FILE_LOCAL = os.path.join(SCRIPT_DIR, "..", "assets", ".feishu.local")
TOKEN_CACHE = os.path.join(SCRIPT_DIR, "..", "assets", ".token_cache")
USER_TOKEN_CACHE = os.path.join(SCRIPT_DIR, "..", "assets", ".user_token_cache")
API_BASE = "https://open.feishu.cn/open-apis"


def usage():
    print(f"用法: {sys.argv[0]} <action> <Feishu_URL> [content_file] [--options]")
    print()
    print("  action       操作类型：read | write | append | clear | insert-targeted | delete-section")
    print("  Feishu_URL   飞书文档地址，如 https://xxx.feishu.cn/wiki/TOKEN")
    print("  content_file 写入时的内容文件路径（write/append/insert-targeted 模式必填）")
    print()
    print("定点插入：")
    print(
        "  python3 scripts/index.py insert-targeted <Feishu_URL> <content_file> "
        "--anchor-type heading|text --anchor \"关键词\" --match fuzzy|regex --position after|section_end [--yes]"
    )
    print("  说明：默认先预览并要求输入 yes，传 --yes 可跳过交互确认")
    print()
    print("删除章节：")
    print("  python3 scripts/index.py delete-section <Feishu_URL> --anchor \"章节名\" --match fuzzy|regex [--yes]")
    print("  说明：先删子章节/内容，最后删标题；默认先预览并要求输入 yes")
    print()
    print("认证方式（优先级从高到低）：")
    print("  1. user_access_token：先运行 login.py 授权")
    print("  2. tenant_access_token：在 ../assets/.feishu.local 或 ../assets/.feishu 配置 app_id + app_secret")
    sys.exit(1)


# 读取配置（环境变量优先，.feishu 文件兜底）
def get_config(key):
    env_map = {"app_id": "FEISHU_APP_ID", "app_secret": "FEISHU_APP_SECRET"}
    env_val = os.environ.get(env_map.get(key, ""), "")
    if env_val:
        return env_val

    def read_config_file(path):
        if not os.path.isfile(path):
            return ""
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k == key:
                        return v
        return ""

    for path in (FEISHU_FILE_LOCAL, FEISHU_FILE):
        value = read_config_file(path)
        if value:
            return value
    return ""


# 获取 tenant_access_token（带缓存，2小时有效）
def get_access_token(app_id, app_secret):
    # 检查缓存是否有效（1.5小时内）
    if os.path.isfile(TOKEN_CACHE):
        try:
            with open(TOKEN_CACHE, "r") as f:
                lines = f.read().strip().split("\n")
            if len(lines) >= 2:
                cached_time = int(lines[0])
                cached_token = lines[1]
                if time.time() - cached_time < 5400 and cached_token:
                    return cached_token
        except Exception:
            pass

    # 请求新 token
    req = urllib.request.Request(
        f"{API_BASE}/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"❌ 获取 tenant_access_token 失败: {e}", file=sys.stderr)
        return ""

    token = result.get("tenant_access_token", "")
    if not token:
        print(f"❌ 获取 tenant_access_token 失败: {result}", file=sys.stderr)
        return ""

    # 缓存 token
    with open(TOKEN_CACHE, "w") as f:
        f.write(f"{int(time.time())}\n{token}\n")
    return token


# 获取 user_access_token（从缓存读取，过期自动用 refresh_token 刷新）
def get_user_access_token(app_id, app_secret):
    if not os.path.isfile(USER_TOKEN_CACHE):
        return ""

    with open(USER_TOKEN_CACHE, "r") as f:
        cache_text = f.read().strip()
    if not cache_text:
        return ""
    try:
        cache = json.loads(cache_text)
    except json.JSONDecodeError:
        print("❌ token 缓存损坏，请重新登录", file=sys.stderr)
        return ""

    access_token = cache.get("access_token", "")
    refresh_token = cache.get("refresh_token", "")
    expires_at = cache.get("expires_at", 0)

    # token 未过期，直接返回（提前5分钟刷新）
    if access_token and time.time() < expires_at - 300:
        return access_token

    # token 过期，用 refresh_token 刷新
    if not refresh_token:
        print("❌ refresh_token 为空，请重新运行 login.py", file=sys.stderr)
        return ""

    # 先获取 app_access_token
    req0 = urllib.request.Request(
        f"{API_BASE}/auth/v3/app_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        method="POST",
    )
    req0.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req0) as resp0:
            app_token = json.loads(resp0.read().decode("utf-8")).get("app_access_token", "")
    except Exception:
        print("❌ 获取 app_access_token 失败", file=sys.stderr)
        return ""

    if not app_token:
        return ""

    # 刷新 user_access_token
    req = urllib.request.Request(
        f"{API_BASE}/authen/v1/oidc/refresh_access_token",
        data=json.dumps({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {app_token}")

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        print("❌ 刷新 token 失败，请重新运行 login.py", file=sys.stderr)
        return ""

    if result.get("code", -1) != 0:
        print(f"❌ 刷新 token 失败: {result.get('msg', '')}，请重新运行 login.py", file=sys.stderr)
        return ""

    data = result.get("data", {})
    new_access_token = data.get("access_token", "")
    new_refresh_token = data.get("refresh_token", "")
    new_expires_in = data.get("expires_in", 0)

    if not new_access_token:
        return ""

    # 更新缓存
    cache["access_token"] = new_access_token
    cache["refresh_token"] = new_refresh_token
    cache["expires_at"] = int(time.time()) + new_expires_in
    with open(USER_TOKEN_CACHE, "w") as f:
        json.dump(cache, f, indent=2)

    return new_access_token


# 解析飞书 URL
def parse_feishu_url(url):
    if not url:
        return None
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url

    m = re.match(r"(https?://[^/]+)/([^/]+)/([a-zA-Z0-9_-]+)", url)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


# 调用飞书 Open API
def api_call(method, path, access_token, body=None, retries=3):
    url = f"{API_BASE}{path}"
    for attempt in range(retries):
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {access_token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("code") == 429 and attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                return result
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else ""
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            try:
                return json.loads(error_body)
            except Exception:
                return {"code": e.code, "msg": error_body}
    return {"code": 429, "msg": "rate limited after retries"}


def check_resp(resp, action_name, auto_retry_login=False):
    code = resp.get("code", -1)
    if code != 0:
        msg = resp.get("msg") or resp.get("message") or "未知错误"
        
        # Token 过期或无效，尝试自动登录
        if code in (99991663, 99991664) and auto_retry_login:
            print(f"🔑 检测到 Token 问题 (code={code})，自动启动登录流程...", file=sys.stderr)
            print("", file=sys.stderr)
            import subprocess
            login_script = os.path.join(SCRIPT_DIR, "login.py")
            try:
                # 先退出登录清除旧 token
                subprocess.run(["python3", login_script, "logout"], check=False, capture_output=True)
                # 启动登录流程（会打开浏览器）
                result = subprocess.run(["python3", login_script], check=True, capture_output=False)
                if result.returncode == 0:
                    print("", file=sys.stderr)
                    print("✅ 登录完成，请重新执行命令", file=sys.stderr)
                    sys.exit(0)
            except subprocess.CalledProcessError:
                print("❌ 自动登录失败，请手动运行: python3 scripts/login.py", file=sys.stderr)
                sys.exit(1)
        
        print(f"❌ {action_name}失败 (code={code}): {msg}", file=sys.stderr)
        print("", file=sys.stderr)
        if code in (99991668, 99991672, 99991679, 1770032):
            print("📋 权限不足，请按以下步骤排查：", file=sys.stderr)
            print("", file=sys.stderr)
            print("   1️⃣  确认飞书应用已开通权限", file=sys.stderr)
            print("      打开 https://open.feishu.cn/app → 进入应用 → 权限管理", file=sys.stderr)
            print("      搜索并开通: docx:document + docx:document:readonly", file=sys.stderr)
            print("", file=sys.stderr)
            print("   2️⃣  重新发布应用版本", file=sys.stderr)
            print("      版本管理与发布 → 创建版本 → 提交发布", file=sys.stderr)
            print("      ⚠️ 每次改权限后都要重新发布，否则不生效", file=sys.stderr)
            print("", file=sys.stderr)
            print("   3️⃣  重新授权登录", file=sys.stderr)
            print("      python3 scripts/login.py logout && python3 scripts/login.py", file=sys.stderr)
            print("", file=sys.stderr)
            print("   如果仍然失败，确认你对该文档有编辑权限（飞书中能正常打开和编辑）", file=sys.stderr)
        elif code == 99991663:
            print("🔑 Token 已过期，请重新登录：", file=sys.stderr)
            print("   python3 scripts/login.py logout && python3 scripts/login.py", file=sys.stderr)
        elif code == 99991664:
            print("� Token 无效，可能未登录或缓存损坏，请重新登录：", file=sys.stderr)
            print("   python3 scripts/login.py logout && python3 scripts/login.py", file=sys.stderr)
        else:
            print("💡 排查建议：", file=sys.stderr)
            print("   1. 确认已运行 login.py 完成授权登录", file=sys.stderr)
            print("   2. 确认飞书应用权限已开通并发布", file=sys.stderr)
            print("   3. 重新登录: python3 scripts/login.py logout && python3 scripts/login.py", file=sys.stderr)
        print("", file=sys.stderr)
        print("📖 完整配置指南: https://github.com/hanhx/feishu-doc#readme", file=sys.stderr)
        sys.exit(1)
    return resp.get("data", {})


def process(action, doc_url, access_token, doc_type, token, content_file="", options=None):
    doc_token = token
    options = options or {}

    if action == "read":
        out = da.handle_read(
            doc_url,
            doc_type,
            doc_token,
            access_token,
            api_call,
            check_resp,
            db.block_to_md,
            db.collect_descendant_ids,
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))

    elif action == "clear":
        out = da.handle_clear(doc_url, doc_token, access_token, api_call, check_resp)
        print(json.dumps(out, ensure_ascii=False, indent=2))

    elif action == "insert-targeted":
        if not content_file:
            print("❌ insert-targeted 模式需要指定内容文件路径", file=sys.stderr)
            sys.exit(1)

        anchor_type = str(options.get("anchor_type", "heading")).strip().lower()
        anchor = str(options.get("anchor", "")).strip()
        match_mode = str(options.get("match", "fuzzy")).strip().lower()
        position = str(options.get("position", "after")).strip().lower()
        yes_flag = ops.flag_enabled(options.get("yes", False))

        if anchor_type not in ("heading", "text"):
            print("❌ --anchor-type 仅支持 heading 或 text", file=sys.stderr)
            sys.exit(1)
        if match_mode not in ("fuzzy", "regex"):
            print("❌ --match 仅支持 fuzzy 或 regex", file=sys.stderr)
            sys.exit(1)
        if position not in ("after", "section_end"):
            print("❌ --position 仅支持 after 或 section_end", file=sys.stderr)
            sys.exit(1)
        if position == "section_end" and anchor_type != "heading":
            print("❌ section_end 仅支持标题锚点（--anchor-type heading）", file=sys.stderr)
            sys.exit(1)
        if not anchor:
            print("❌ insert-targeted 需要 --anchor", file=sys.stderr)
            sys.exit(1)

        with open(content_file, "r", encoding="utf-8") as f:
            content = f.read()

        block_list = ops.parse_markdown_for_targeted_insert(
            content,
            db.make_heading_block,
            db.make_code_block,
            db.make_divider_block,
            db.make_todo_block,
            db.make_bullet_block,
            db.make_ordered_block,
            db.make_quote_block,
            db.make_text_block,
        )
        if not block_list:
            print("❌ 内容为空", file=sys.stderr)
            sys.exit(1)

        items, block_map = ops.fetch_all_blocks(doc_token, access_token, api_call, check_resp)
        parent_map, index_map = ops.build_parent_index_maps(block_map)

        candidates = ops.find_anchor_candidates(
            items,
            block_map,
            anchor_type,
            anchor,
            match_mode,
            db.get_block_text_by_id,
            db.extract_text,
        )
        target = ops.resolve_single_candidate(candidates, items, anchor, anchor_type, db.extract_text)
        target_id = target["block_id"]

        if target_id == doc_token:
            print("❌ 定点插入不支持文档标题（page）作为锚点，请选择具体章节标题或正文文本", file=sys.stderr)
            sys.exit(1)

        parent_id = parent_map.get(target_id)
        if not parent_id and target_id == doc_token:
            parent_id = doc_token
        if not parent_id:
            print("❌ 无法定位锚点父块", file=sys.stderr)
            sys.exit(1)

        siblings = block_map.get(parent_id, {}).get("children", [])
        target_index = index_map.get((parent_id, target_id), 0)
        insert_index = target_index + 1

        if position == "section_end":
            target_block = block_map.get(target_id, {})
            lvl = ops.heading_level(target_block)
            if lvl is None:
                print("❌ section_end 需要标题锚点", file=sys.stderr)
                sys.exit(1)
            insert_index = ops.compute_section_end_index(siblings, target_index, lvl, block_map)

        preview = {
            "anchorType": anchor_type,
            "anchor": anchor,
            "matchMode": match_mode,
            "position": position,
            "targetBlockId": target_id,
            "targetText": target["text"],
            "parentBlockId": parent_id,
            "insertIndex": insert_index,
            "blocksToAdd": len(block_list),
            "contentPreview": ops.summarize_content(content),
        }
        ops.confirm_with_preview(preview, yes_flag, "insert-targeted")

        added = ops.insert_blocks_at_index(
            doc_token,
            access_token,
            parent_id,
            insert_index,
            block_list,
            api_call,
            check_resp,
            db.make_text_elements,
        )
        out = {
            "docUrl": doc_url,
            "action": "insert-targeted",
            "anchor": target["text"],
            "position": position,
            "blocksAdded": added,
            "status": "success",
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))

    elif action == "delete-section":
        anchor = str(options.get("anchor", "")).strip()
        match_mode = str(options.get("match", "fuzzy")).strip().lower()
        yes_flag = ops.flag_enabled(options.get("yes", False))

        if not anchor:
            print("❌ delete-section 需要 --anchor", file=sys.stderr)
            sys.exit(1)
        if match_mode not in ("fuzzy", "regex"):
            print("❌ --match 仅支持 fuzzy 或 regex", file=sys.stderr)
            sys.exit(1)

        items, block_map = ops.fetch_all_blocks(doc_token, access_token, api_call, check_resp)
        parent_map, index_map = ops.build_parent_index_maps(block_map)

        candidates = ops.find_anchor_candidates(
            items,
            block_map,
            "heading",
            anchor,
            match_mode,
            db.get_block_text_by_id,
            db.extract_text,
        )
        target = ops.resolve_single_candidate(candidates, items, anchor, "heading", db.extract_text)
        target_id = target["block_id"]

        if target_id == doc_token:
            print("❌ delete-section 不支持删除文档标题（page），请指定具体章节标题", file=sys.stderr)
            sys.exit(1)

        parent_id = parent_map.get(target_id)
        if not parent_id and target_id == doc_token:
            parent_id = doc_token
        if not parent_id:
            print("❌ 无法定位目标章节父块", file=sys.stderr)
            sys.exit(1)

        siblings = block_map.get(parent_id, {}).get("children", [])
        target_index = index_map.get((parent_id, target_id), 0)
        target_block = block_map.get(target_id, {})
        lvl = ops.heading_level(target_block)
        if lvl is None:
            print("❌ delete-section 仅支持标题锚点", file=sys.stderr)
            sys.exit(1)

        section_end = ops.compute_section_end_index(siblings, target_index, lvl, block_map)
        children_to_delete = max(0, section_end - (target_index + 1))
        total_to_delete = children_to_delete + 1

        preview = {
            "anchor": anchor,
            "matchMode": match_mode,
            "targetBlockId": target_id,
            "targetHeading": target["text"],
            "parentBlockId": parent_id,
            "deleteRange": {
                "childrenStartIndex": target_index + 1,
                "childrenEndIndex": section_end,
                "headingIndex": target_index,
            },
            "blocksToDelete": total_to_delete,
            "rule": "先删除子章节/内容，再删除标题",
        }
        ops.confirm_with_preview(preview, yes_flag, "delete-section")

        deleted_children = ops.delete_children_range(
            doc_token,
            access_token,
            parent_id,
            target_index + 1,
            section_end,
            api_call,
            check_resp,
        )
        deleted_heading = ops.delete_children_range(
            doc_token,
            access_token,
            parent_id,
            target_index,
            target_index + 1,
            api_call,
            check_resp,
        )

        out = {
            "docUrl": doc_url,
            "action": "delete-section",
            "anchor": target["text"],
            "blocksDeleted": deleted_children + deleted_heading,
            "status": "success",
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))

    elif action in ("write", "append"):
        out = da.handle_write_append(
            action,
            doc_url,
            doc_token,
            access_token,
            content_file,
            {
                "api_call": api_call,
                "check_resp": check_resp,
                "make_heading_block": db.make_heading_block,
                "make_code_block": db.make_code_block,
                "make_divider_block": db.make_divider_block,
                "make_todo_block": db.make_todo_block,
                "make_bullet_block": db.make_bullet_block,
                "make_ordered_block": db.make_ordered_block,
                "make_quote_block": db.make_quote_block,
                "make_text_block": db.make_text_block,
                "make_plain_elements": db.make_plain_elements,
                "make_text_elements": db.make_text_elements,
            },
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))


# --- 主逻辑 ---
def main():
    if len(sys.argv) < 3:
        usage()

    action = sys.argv[1]
    doc_url = sys.argv[2]
    args_after_url = sys.argv[3:]
    content_file = ""
    options = {}

    if action in ("write", "append"):
        content_file = args_after_url[0] if args_after_url else ""
    elif action == "insert-targeted":
        pos, options = ops.parse_cli_options(args_after_url)
        content_file = pos[0] if pos else ""
    elif action == "delete-section":
        _, options = ops.parse_cli_options(args_after_url)

    if not doc_url:
        usage()

    if action not in ("read", "write", "append", "clear", "insert-targeted", "delete-section"):
        print(
            f"❌ 不支持的操作: {action}，请使用 read / write / append / clear / insert-targeted / delete-section",
            file=sys.stderr,
        )
        sys.exit(1)

    # 解析 URL
    parsed = parse_feishu_url(doc_url)
    if not parsed:
        print("❌ 请输入正确的飞书文档地址，格式示例：", file=sys.stderr)
        print("  https://xxx.feishu.cn/wiki/TOKEN", file=sys.stderr)
        print("  https://xxx.feishu.cn/docx/TOKEN", file=sys.stderr)
        sys.exit(1)

    domain, doc_type, token = parsed

    # 获取凭证
    app_id = get_config("app_id")
    app_secret = get_config("app_secret")

    # 必须使用 user_access_token（个人授权）
    if not app_id or not app_secret:
        print("❌ 未找到应用凭证，请先完成配置：", file=sys.stderr)
        print("", file=sys.stderr)
        print("   1️⃣  配置应用凭证（二选一）：", file=sys.stderr)
        print("      方式A: 环境变量（推荐）", file=sys.stderr)
        print("        export FEISHU_APP_ID=cli_xxxx", file=sys.stderr)
        print("        export FEISHU_APP_SECRET=xxxx", file=sys.stderr)
        print("      方式B: 编辑 assets/.feishu.local 或 assets/.feishu 文件", file=sys.stderr)
        print("        app_id=cli_xxxx", file=sys.stderr)
        print("        app_secret=xxxx", file=sys.stderr)
        print("", file=sys.stderr)
        print("   2️⃣  授权登录：", file=sys.stderr)
        print("      python3 scripts/login.py", file=sys.stderr)
        print("", file=sys.stderr)
        print("   💡 没有 App ID？参考: https://github.com/hanhx/feishu-doc#readme", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(USER_TOKEN_CACHE):
        print("🔑 检测到未登录，自动启动登录流程...", file=sys.stderr)
        print("", file=sys.stderr)
        import subprocess
        login_script = os.path.join(SCRIPT_DIR, "login.py")
        try:
            result = subprocess.run(["python3", login_script], check=True, capture_output=False)
            if result.returncode == 0:
                print("", file=sys.stderr)
                print("✅ 登录完成，请重新执行命令", file=sys.stderr)
                sys.exit(0)
        except subprocess.CalledProcessError:
            print("❌ 自动登录失败，请手动运行: python3 scripts/login.py", file=sys.stderr)
            sys.exit(1)

    access_token = get_user_access_token(app_id, app_secret)
    if not access_token:
        print("🔑 Token 获取失败，自动启动登录流程...", file=sys.stderr)
        print("", file=sys.stderr)
        import subprocess
        login_script = os.path.join(SCRIPT_DIR, "login.py")
        try:
            subprocess.run(["python3", login_script, "logout"], check=False, capture_output=True)
            result = subprocess.run(["python3", login_script], check=True, capture_output=False)
            if result.returncode == 0:
                print("", file=sys.stderr)
                print("✅ 登录完成，请重新执行命令", file=sys.stderr)
                sys.exit(0)
        except subprocess.CalledProcessError:
            print("❌ 自动登录失败，请手动运行: python3 scripts/login.py", file=sys.stderr)
            sys.exit(1)

    # 执行操作
    process(action, doc_url, access_token, doc_type, token, content_file, options)


if __name__ == "__main__":
    main()
