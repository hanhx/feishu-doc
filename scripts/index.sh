#!/usr/bin/env bash

set -e

# 脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FEISHU_FILE="$SCRIPT_DIR/../assets/.feishu"
TOKEN_CACHE="$SCRIPT_DIR/../assets/.token_cache"
USER_TOKEN_CACHE="$SCRIPT_DIR/../assets/.user_token_cache"
API_BASE="https://open.feishu.cn/open-apis"

usage() {
  echo "用法: $0 <action> <Feishu_URL> [content_file]"
  echo ""
  echo "  action       操作类型：read | write | append | clear"
  echo "  Feishu_URL   飞书文档地址，如 https://xxx.feishu.cn/wiki/TOKEN"
  echo "  content_file 写入时的内容文件路径（write 模式必填）"
  echo ""
  echo "认证方式（优先级从高到低）："
  echo "  1. user_access_token：先运行 login.sh 授权"
  echo "  2. tenant_access_token：在 ../assets/.feishu 配置 app_id + app_secret"
  exit 1
}

# 从 .feishu 读取配置
get_config() {
  local key="$1"
  [[ ! -f "$FEISHU_FILE" ]] && return
  while IFS='=' read -r k v || [[ -n "${k:-}" ]]; do
    k=$(echo "$k" | tr -d ' ')
    v=$(echo "$v" | tr -d ' ')
    [[ -z "$k" || "$k" =~ ^# ]] && continue
    [[ "$k" == "$key" ]] && { echo "$v"; return; }
  done < "$FEISHU_FILE"
}


# 获取 tenant_access_token（带缓存，2小时有效）
get_access_token() {
  local app_id="$1" app_secret="$2"

  # 检查缓存是否有效（1.5小时内）
  if [[ -f "$TOKEN_CACHE" ]]; then
    local cached_time cached_token
    cached_time=$(head -1 "$TOKEN_CACHE" 2>/dev/null || echo "0")
    cached_token=$(tail -1 "$TOKEN_CACHE" 2>/dev/null || echo "")
    local now
    now=$(date +%s)
    local diff=$((now - cached_time))
    if [[ $diff -lt 5400 && -n "$cached_token" ]]; then
      echo "$cached_token"
      return
    fi
  fi

  # 请求新 token
  local resp
  resp=$(curl -sS -X POST "${API_BASE}/auth/v3/tenant_access_token/internal" \
    -H "Content-Type: application/json" \
    -d "{\"app_id\":\"${app_id}\",\"app_secret\":\"${app_secret}\"}" 2>/dev/null)

  local token
  token=$(echo "$resp" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('tenant_access_token',''))" 2>/dev/null)

  if [[ -z "$token" ]]; then
    echo "❌ 获取 tenant_access_token 失败: $resp" >&2
    return 1
  fi

  # 缓存 token
  echo "$(date +%s)" > "$TOKEN_CACHE"
  echo "$token" >> "$TOKEN_CACHE"
  echo "$token"
}

# 获取 user_access_token（从缓存读取，过期自动用 refresh_token 刷新）
get_user_access_token() {
  local app_id="$1" app_secret="$2"
  [[ ! -f "$USER_TOKEN_CACHE" ]] && return 1

  python3 - "$USER_TOKEN_CACHE" "$app_id" "$app_secret" "$API_BASE" <<'PYTHON_SCRIPT'
import sys, json, time, urllib.request, urllib.error

cache_path, app_id, app_secret, api_base = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

with open(cache_path, 'r') as f:
    cache = json.loads(f.read())

access_token = cache.get("access_token", "")
refresh_token = cache.get("refresh_token", "")
expires_at = cache.get("expires_at", 0)

# token 未过期，直接返回（提前5分钟刷新）
if access_token and time.time() < expires_at - 300:
    print(access_token)
    sys.exit(0)

# token 过期，用 refresh_token 刷新
if not refresh_token:
    print("❌ refresh_token 为空，请重新运行 login.sh", file=sys.stderr)
    sys.exit(1)

# 先获取 app_access_token
req0 = urllib.request.Request(
    f"{api_base}/auth/v3/app_access_token/internal",
    data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
    method="POST")
req0.add_header("Content-Type", "application/json")
try:
    with urllib.request.urlopen(req0) as resp0:
        app_token = json.loads(resp0.read().decode("utf-8")).get("app_access_token", "")
except:
    print("❌ 获取 app_access_token 失败", file=sys.stderr)
    sys.exit(1)

if not app_token:
    sys.exit(1)

# 刷新 user_access_token
req = urllib.request.Request(
    f"{api_base}/authen/v1/oidc/refresh_access_token",
    data=json.dumps({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode("utf-8"),
    method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", f"Bearer {app_token}")

try:
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    print(f"❌ 刷新 token 失败，请重新运行 login.sh", file=sys.stderr)
    sys.exit(1)

if result.get("code", -1) != 0:
    print(f"❌ 刷新 token 失败: {result.get('msg', '')}，请重新运行 login.sh", file=sys.stderr)
    sys.exit(1)

data = result.get("data", {})
new_access_token = data.get("access_token", "")
new_refresh_token = data.get("refresh_token", "")
new_expires_in = data.get("expires_in", 0)

if not new_access_token:
    sys.exit(1)

# 更新缓存
cache["access_token"] = new_access_token
cache["refresh_token"] = new_refresh_token
cache["expires_at"] = int(time.time()) + new_expires_in
with open(cache_path, "w") as f:
    json.dump(cache, f, indent=2)

print(new_access_token)
PYTHON_SCRIPT
}

# 解析飞书 URL
parse_feishu_url() {
  local url="$1"
  [[ -z "$url" ]] && return 1
  url=$(echo "$url" | tr -d ' ')
  [[ "$url" != http* ]] && url="https://$url"

  local domain doc_type token
  domain=$(echo "$url" | sed -E -n 's|(https?://[^/]+).*|\1|p')
  doc_type=$(echo "$url" | sed -E -n 's|https?://[^/]+/([^/]+)/.*|\1|p')
  token=$(echo "$url" | sed -E -n 's|https?://[^/]+/[^/]+/([a-zA-Z0-9_-]+).*|\1|p')

  [[ -z "$domain" || -z "$doc_type" || -z "$token" ]] && return 1
  echo "$domain|$doc_type|$token"
}

# 调用飞书 Open API
feishu_api() {
  local method="$1" path="$2" access_token="$3" body="${4:-}"

  if [[ "$method" == "GET" ]]; then
    curl -sS "${API_BASE}${path}" \
      -H "Authorization: Bearer ${access_token}" \
      -H "Content-Type: application/json" 2>/dev/null
  else
    curl -sS -X "$method" "${API_BASE}${path}" \
      -H "Authorization: Bearer ${access_token}" \
      -H "Content-Type: application/json" \
      -d "$body" 2>/dev/null
  fi
}

# 主处理逻辑（python）
process() {
  local action="$1" doc_url="$2" access_token="$3" doc_type="$4" token="$5" content_file="${6:-}"

  python3 - "$action" "$doc_url" "$access_token" "$doc_type" "$token" "$content_file" "$API_BASE" <<'PYTHON_SCRIPT'
import sys, json, urllib.request, urllib.error

action = sys.argv[1]
doc_url = sys.argv[2]
access_token = sys.argv[3]
doc_type = sys.argv[4]
token = sys.argv[5]
content_file = sys.argv[6] if len(sys.argv) > 6 else ""
api_base = sys.argv[7] if len(sys.argv) > 7 else "https://open.feishu.cn/open-apis"

def api_call(method, path, body=None, retries=3):
    import time as _time
    url = f"{api_base}{path}"
    for attempt in range(retries):
        data = json.dumps(body).encode('utf-8') if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {access_token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                if result.get("code") == 429 and attempt < retries - 1:
                    _time.sleep(2 * (attempt + 1))
                    continue
                return result
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ""
            if e.code == 429 and attempt < retries - 1:
                _time.sleep(2 * (attempt + 1))
                continue
            try:
                return json.loads(error_body)
            except:
                return {"code": e.code, "msg": error_body}
    return {"code": 429, "msg": "rate limited after retries"}

def check_resp(resp, action_name):
    code = resp.get("code", -1)
    if code != 0:
        msg = resp.get("msg") or resp.get("message") or "未知错误"
        print(f"❌ {action_name}失败 (code={code}): {msg}", file=sys.stderr)
        if code in (99991668, 99991672, 99991679, 1770032):
            print("", file=sys.stderr)
            print("📋 权限不足，请检查以下配置（飞书开放平台 → 应用 → 权限管理）：", file=sys.stderr)
            print("   1. 开通权限: docx:document + docx:document:readonly", file=sys.stderr)
            print("   2. 发布应用版本（权限变更后需重新发布）", file=sys.stderr)
            print("   3. 重新授权: cd ~/.codeium/windsurf/skills/feishu-doc && bash scripts/login.sh", file=sys.stderr)
        elif code == 99991663:
            print("", file=sys.stderr)
            print("🔑 Token 已过期，请重新登录:", file=sys.stderr)
            print("   cd ~/.codeium/windsurf/skills/feishu-doc && bash scripts/login.sh", file=sys.stderr)
        sys.exit(1)
    return resp.get("data", {})

# wiki token 可直接作为 doc_token 使用（飞书 docx API 支持）
doc_token = token
title = ""

if action == "read":
    # 方式1：获取纯文本
    resp = api_call("GET", f"/docx/v1/documents/{doc_token}/raw_content")
    data = check_resp(resp, "获取文档内容")
    content = data.get("content", "")

    # 方式2：获取 blocks 并转为 markdown（支持翻页）
    items = []
    page_token = ""
    while True:
        url = f"/docx/v1/documents/{doc_token}/blocks?page_size=500"
        if page_token:
            url += f"&page_token={page_token}"
        resp2 = api_call("GET", url)
        blocks_data = resp2.get("data", {}) if resp2.get("code", -1) == 0 else {}
        items.extend(blocks_data.get("items", []))
        if not blocks_data.get("has_more", False):
            break
        page_token = blocks_data.get("page_token", "")
        if not page_token:
            break

    def extract_text(elements):
        if not elements: return ""
        parts = []
        for el in elements:
            if isinstance(el, dict):
                tr = el.get("text_run") or {}
                parts.append(tr.get("content", ""))
                mr = el.get("mention_user") or el.get("mention_doc") or {}
                if mr: parts.append(mr.get("content", ""))
        return "".join(parts)

    def block_to_md(block):
        btype = block.get("block_type", 0)
        if btype == 1:  # page
            page = block.get("page", {})
            return "# " + extract_text(page.get("elements", []))
        elif btype == 2:  # text
            return extract_text(block.get("text", {}).get("elements", []))
        elif btype in range(3, 12):  # heading 1-9
            level = btype - 2
            key = f"heading{level}"
            return "#" * level + " " + extract_text(block.get(key, {}).get("elements", []))
        elif btype == 12:  # bullet
            return "- " + extract_text(block.get("bullet", {}).get("elements", []))
        elif btype == 13:  # ordered
            return "1. " + extract_text(block.get("ordered", {}).get("elements", []))
        elif btype == 14:  # code
            code = block.get("code", {})
            lang_map = {0:"PlainText",1:"ABAP",2:"Ada",3:"Apache",4:"Apex",5:"Assembly",
                        6:"Bash",7:"CSharp",8:"CPP",9:"C",10:"COBOL",11:"CSS",12:"CoffeeScript",
                        13:"D",14:"Dart",15:"Delphi",16:"Django",17:"Dockerfile",18:"Erlang",
                        19:"Fortran",20:"FoxPro",21:"Go",22:"Groovy",23:"HTML",24:"HTMLBars",
                        25:"HTTP",26:"Haskell",27:"JSON",28:"Java",29:"JavaScript",30:"Julia",
                        31:"Kotlin",32:"LateX",33:"Lisp",34:"Logo",35:"Lua",36:"MATLAB",
                        37:"Makefile",38:"Markdown",39:"Nginx",40:"Objective-C",41:"OpenEdgeABL",
                        42:"PHP",43:"Perl",44:"PostScript",45:"Power Shell",46:"Prolog",
                        47:"ProtoBuf",48:"Python",49:"R",50:"RPG",51:"Ruby",52:"Rust",53:"SAS",
                        54:"SCSS",55:"SQL",56:"Scala",57:"Scheme",58:"Scratch",59:"Shell",
                        60:"Swift",61:"Thrift",62:"TypeScript",63:"VBScript",64:"Visual Basic",
                        65:"XML",66:"YAML"}
            lang = lang_map.get(code.get("style", {}).get("language", 0), "")
            return f"```{lang}\n{extract_text(code.get('elements', []))}\n```"
        elif btype == 15:  # quote
            return "> " + extract_text(block.get("quote_container", block.get("quote", {})).get("elements", []))
        elif btype == 17:  # todo
            todo = block.get("todo", {})
            done = todo.get("style", {}).get("done", False)
            return f"- [{'x' if done else ' '}] " + extract_text(todo.get("elements", []))
        elif btype == 23:  # divider
            return "---"
        elif btype == 27:  # image
            return "[图片]"
        elif btype == 22:  # table
            return "[表格]"
        elif btype == 18:  # bitable
            return "[多维表格]"
        elif btype == 31:  # grid
            return "[分栏]"
        elif btype == 19:  # callout
            return "[高亮块]"
        else:
            for key in block:
                if isinstance(block[key], dict) and "elements" in block[key]:
                    return extract_text(block[key]["elements"])
            return ""

    md_lines = []
    for item in items:
        line = block_to_md(item)
        if line is not None:
            md_lines.append(line)

    markdown = "\n".join(md_lines)

    out = {
        "docUrl": doc_url,
        "title": title if doc_type == "wiki" else "",
        "blockCount": len(items),
        "markdown": markdown,
        "rawContent": content
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

elif action == "clear":
    page_block_id = doc_token
    clear_resp = api_call("GET", f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}")
    clear_data = check_resp(clear_resp, "获取文档块")
    clear_children = clear_data.get("block", {}).get("children", [])
    # 清空标题
    api_call("PATCH", f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}",
        {"update_text_elements": {"elements": [{"text_run": {"content": " "}}]}})
    if not clear_children:
        out = {"docUrl": doc_url, "action": "clear", "blocksDeleted": 0, "status": "success"}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        del_count = len(clear_children)
        del_resp = api_call("DELETE",
            f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children/batch_delete",
            {"start_index": 0, "end_index": del_count})
        if del_resp.get("code") != 0:
            import time as _t
            remaining = del_count
            while remaining > 0:
                batch = min(50, remaining)
                api_call("DELETE",
                    f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children/batch_delete",
                    {"start_index": 0, "end_index": batch})
                remaining -= batch
                _t.sleep(0.3)
        out = {"docUrl": doc_url, "action": "clear", "blocksDeleted": del_count, "status": "success"}
        print(json.dumps(out, ensure_ascii=False, indent=2))

elif action in ("write", "append"):
    if not content_file:
        print(f"❌ {action} 模式需要指定内容文件路径", file=sys.stderr)
        sys.exit(1)

    with open(content_file, 'r', encoding='utf-8') as f:
        content = f.read()

    page_block_id = doc_token

    import re as re_mod

    def parse_inline_styles(text):
        """Parse markdown inline styles into feishu text_run elements with styles."""
        if not text:
            return [{"text_run": {"content": " "}}]
        elements = []
        # Pattern: **bold**, `code`, ~~strikethrough~~, [text](url)
        pattern = re_mod.compile(
            r'(\*\*(.+?)\*\*)'        # bold
            r'|(`([^`]+)`)'            # inline code
            r'|(~~(.+?)~~)'            # strikethrough
            r'|(\[([^\]]+)\]\(([^)]+)\))'  # link
        )
        pos = 0
        for m in pattern.finditer(text):
            # Add plain text before this match
            if m.start() > pos:
                elements.append({"text_run": {"content": text[pos:m.start()]}})
            if m.group(2):  # bold
                elements.append({"text_run": {"content": m.group(2), "text_element_style": {"bold": True}}})
            elif m.group(4):  # inline code
                elements.append({"text_run": {"content": m.group(4), "text_element_style": {"inline_code": True}}})
            elif m.group(6):  # strikethrough
                elements.append({"text_run": {"content": m.group(6), "text_element_style": {"strikethrough": True}}})
            elif m.group(8):  # link
                link_url = m.group(9)
                if link_url.startswith("http://") or link_url.startswith("https://"):
                    elements.append({"text_run": {"content": m.group(8), "text_element_style": {"link": {"url": link_url}}}})
                else:
                    elements.append({"text_run": {"content": f"[{m.group(8)}]({link_url})"}})
            pos = m.end()
        # Remaining text
        if pos < len(text):
            elements.append({"text_run": {"content": text[pos:]}})
        return elements if elements else [{"text_run": {"content": " "}}]

    def make_text_elements(text):
        return parse_inline_styles(text)

    def make_text_block(text):
        return {"block_type": 2, "text": {"elements": make_text_elements(text)}}

    def make_heading_block(level, text):
        # 飞书 API 不支持直接创建 heading block，用加粗文本模拟
        elements = [{"text_run": {"content": text, "text_element_style": {"bold": True}}}]
        return {"block_type": 2, "text": {"elements": elements}}

    def make_bullet_block(text):
        return {"block_type": 12, "bullet": {"elements": make_text_elements(text)}}

    def make_ordered_block(text):
        return {"block_type": 13, "ordered": {"elements": make_text_elements(text)}}

    def make_plain_elements(text):
        return [{"text_run": {"content": text}}] if text else [{"text_run": {"content": " "}}]

    def make_code_block(code_text, lang=""):
        lang_map = {"sql":56,"java":29,"javascript":30,"typescript":63,"python":49,
                    "go":22,"bash":7,"shell":60,"json":28,"yaml":67,"xml":66,
                    "html":24,"css":11,"groovy":23,"lua":36,"markdown":39,
                    "nginx":40,"php":43,"c":10,"cpp":9,"c++":9,"csharp":8,"c#":8,
                    "scala":57,"ruby":52,"rust":53,"r":50,"scss":55,
                    "mermaid":21,"plaintext":21,"":21}
        lang_code = lang_map.get(lang.lower(), 21)
        return {"block_type": 14, "code": {
            "elements": make_plain_elements(code_text),
            "style": {"language": lang_code}
        }}

    def make_quote_block(text):
        # callout 是容器块，需要标记后单独处理
        return {"_callout": True, "_callout_text": text}

    def make_divider_block():
        return {"block_type": 2, "text": {"elements": make_text_elements("───────────────────")}}

    def make_todo_block(text, done=False):
        return {"block_type": 17, "todo": {
            "elements": make_text_elements(text),
            "style": {"done": done}
        }}

    # 解析 markdown 为 blocks
    lines = content.split("\n")
    children = []
    counter = [0]  # counter[0] = total_added
    BATCH_SIZE = 50
    import time as time_mod

    def flush_blocks(block_list):
        pending_buf = []
        for blk in block_list:
            if blk.get("_callout"):
                while pending_buf:
                    batch = pending_buf[:BATCH_SIZE]
                    pending_buf = pending_buf[BATCH_SIZE:]
                    resp = api_call("POST",
                        f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children",
                        {"children": batch, "index": -1})
                    check_resp(resp, "写入文档")
                    counter[0] += len(batch)
                    time_mod.sleep(0.5)
                cb = {"block_type": 19, "callout": {"background_color": 15}}
                cr = api_call("POST",
                    f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children",
                    {"children": [cb], "index": -1})
                cd = check_resp(cr, "创建引用块")
                counter[0] += 1
                ci = cd.get("children", [{}])[0].get("block_id", "")
                if ci:
                    cc = {"block_type": 2, "text": {"elements": make_text_elements(blk["_callout_text"])}}
                    api_call("POST",
                        f"/docx/v1/documents/{doc_token}/blocks/{ci}/children",
                        {"children": [cc], "index": 0})
                time_mod.sleep(0.3)
            else:
                pending_buf.append(blk)
        while pending_buf:
            batch = pending_buf[:BATCH_SIZE]
            pending_buf = pending_buf[BATCH_SIZE:]
            resp = api_call("POST",
                f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children",
                {"children": batch, "index": -1})
            check_resp(resp, "写入文档")
            counter[0] += len(batch)
            time_mod.sleep(0.5)

    doc_title_set = False
    i = 0
    while i < len(lines):
        line = lines[i]

        # 第一个 H1 标题 → 设置为文档标题（page block title），append 模式跳过
        if not doc_title_set and re_mod.match(r'^#\s+(.+)', line) and not re_mod.match(r'^##', line):
            title_text = re_mod.match(r'^#\s+(.+)', line).group(1)
            if action == "write":
                api_call("PATCH", f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}",
                    {"update_text_elements": {"elements": [{"text_run": {"content": title_text}}]}})
            else:
                children.append(make_heading_block(1, title_text))
            doc_title_set = True
            i += 1
            continue

        # 代码块
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            code_text = "\n".join(code_lines)
            # 自动检测语言
            if not lang:
                ct = code_text.strip()
                if any(k in ct for k in ["CREATE TABLE", "ALTER TABLE", "INSERT INTO", "SELECT ", "DROP TABLE"]):
                    lang = "sql"
                elif any(k in ct for k in ["@FeignClient", "public ", "private ", "interface ", "class ", "@Override", "@GetMapping", "@PostMapping", "import "]):
                    lang = "java"
                elif (ct.startswith("{") or ct.startswith("[")):
                    lang = "json"
                elif any(k in ct for k in ["flowchart", "sequenceDiagram", "stateDiagram", "erDiagram", "gantt"]):
                    lang = "mermaid"
                elif any(k in ct for k in ["GET /", "POST /", "PUT /", "DELETE /"]):
                    lang = "bash"
            children.append(make_code_block(code_text, lang))
            continue

        # 空行 → 跳过
        if not line.strip():
            i += 1
            continue

        # 分割线
        if re_mod.match(r'^-{3,}$', line.strip()) or re_mod.match(r'^\*{3,}$', line.strip()):
            children.append(make_divider_block())
            i += 1
            continue

        # 标题
        hm = re_mod.match(r'^(#{1,9})\s+(.*)', line)
        if hm:
            level = len(hm.group(1))
            children.append(make_heading_block(level, hm.group(2)))
            i += 1
            continue

        # 去掉前导空格用于匹配（保留原始缩进信息）
        stripped = line.lstrip()

        # todo（支持缩进）
        tm = re_mod.match(r'^-\s*\[([ xX])\]\s*(.*)', stripped)
        if tm:
            done = tm.group(1).lower() == 'x'
            children.append(make_todo_block(tm.group(2), done))
            i += 1
            continue

        # 无序列表（支持缩进）
        if re_mod.match(r'^[-*+]\s+', stripped):
            text = re_mod.sub(r'^[-*+]\s+', '', stripped)
            children.append(make_bullet_block(text))
            i += 1
            continue

        # 有序列表（支持缩进）
        om = re_mod.match(r'^\d+\.\s+(.*)', stripped)
        if om:
            children.append(make_ordered_block(om.group(1)))
            i += 1
            continue

        # 引用（合并连续 > 行）
        if stripped.startswith("> ") or stripped == ">" or (stripped.startswith(">") and not stripped.startswith(">"*3)):
            quote_lines = []
            while i < len(lines):
                ql = lines[i].lstrip()
                if ql.startswith("> "): ql = ql[2:]
                elif ql.startswith(">"): ql = ql[1:]
                else: break
                quote_lines.append(ql)
                i += 1
            children.append(make_quote_block("\n".join(quote_lines)))
            continue

        # 表格 → 飞书原生表格
        if stripped.startswith("|") and i + 1 < len(lines) and re_mod.match(r'^\s*\|[-:|]+', lines[i+1]):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            if len(table_lines) >= 2:
                header_cells = [c.strip() for c in table_lines[0].split("|") if c.strip()]
                data_rows = []
                for row_line in table_lines[2:]:
                    cells = [c.strip() for c in row_line.split("|") if c.strip()]
                    data_rows.append(cells)
                # 用表头列数作为基准
                col_size = len(header_cells)
                row_size = 1 + len(data_rows)
                # 计算列宽（按内容长度比例分配，总宽700）
                all_rows_for_width = [header_cells] + data_rows
                col_max_len = [0] * col_size
                for row_cells in all_rows_for_width:
                    for ci in range(min(len(row_cells), col_size)):
                        col_max_len[ci] = max(col_max_len[ci], len(row_cells[ci]))
                total_len = max(sum(col_max_len), 1)
                total_width = 700
                col_widths = [max(100, int(total_width * cl / total_len)) for cl in col_max_len]
                # 先把当前 children 写入
                flush_blocks(children)
                children = []
                # 飞书 API 限制表格最多 9 行
                total_cells = row_size * col_size
                if row_size > 9:
                    children.append(make_code_block("\n".join(table_lines), "markdown"))
                else:
                    table_block = {
                        "block_type": 31,
                        "table": {"property": {
                            "row_size": row_size, "column_size": col_size,
                            "column_width": col_widths, "header_row": True
                        }}
                    }
                    t_resp = api_call("POST",
                        f"/docx/v1/documents/{doc_token}/blocks/{page_block_id}/children",
                        {"children": [table_block], "index": -1})
                    t_code = t_resp.get("code", -1)
                    if t_code != 0:
                        print(f"⚠️ 表格创建失败({row_size}x{col_size}={total_cells}), fallback代码块: {t_resp.get('msg','')[:80]}", file=sys.stderr)
                        children.append(make_code_block("\n".join(table_lines), "markdown"))
                    else:
                        t_data = t_resp.get("data", {})
                        counter[0] += 1
                        t_children = t_data.get("children", [])
                        if t_children:
                            table_info = t_children[0]
                            cell_ids = table_info.get("table", {}).get("cells", [])
                            all_rows = [header_cells] + data_rows
                            # 并发填充 cells
                            from concurrent.futures import ThreadPoolExecutor
                            def fill_cell(args):
                                cell_id, text, is_header = args
                                el = make_plain_elements(text) if is_header else make_text_elements(text)
                                cell_block = {"block_type": 2, "text": {"elements": el}}
                                api_call("POST",
                                    f"/docx/v1/documents/{doc_token}/blocks/{cell_id}/children",
                                    {"children": [cell_block], "index": 0})
                            tasks = []
                            for ri, row_cells in enumerate(all_rows):
                                for ci in range(col_size):
                                    cell_idx = ri * col_size + ci
                                    if cell_idx >= len(cell_ids):
                                        break
                                    cell_text = row_cells[ci] if ci < len(row_cells) else ""
                                    if not cell_text:
                                        continue
                                    tasks.append((cell_ids[cell_idx], cell_text, ri == 0))
                            with ThreadPoolExecutor(max_workers=5) as pool:
                                pool.map(fill_cell, tasks)
                    import time as time_mod
                    time_mod.sleep(0.5)
            continue

        # 普通文本
        children.append(make_text_block(line))
        i += 1

    if not children and counter[0] == 0:
        print("❌ 内容为空", file=sys.stderr)
        sys.exit(1)

    # 写入所有剩余 blocks
    flush_blocks(children)

    out = {
        "docUrl": doc_url,
        "action": "write",
        "blocksAdded": counter[0],
        "totalBatches": (len(children) + BATCH_SIZE - 1) // BATCH_SIZE,
        "status": "success"
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
PYTHON_SCRIPT
}

# --- 主逻辑 ---
[[ $# -lt 2 ]] && usage

ACTION="$1"
DOC_URL="$2"
CONTENT_FILE="${3:-}"

[[ -z "$DOC_URL" ]] && usage
[[ "$ACTION" != "read" && "$ACTION" != "write" && "$ACTION" != "append" && "$ACTION" != "clear" ]] && {
  echo "❌ 不支持的操作: $ACTION，请使用 read / write / append / clear" >&2
  exit 1
}

# 解析 URL
parsed=$(parse_feishu_url "$DOC_URL" || true)
if [[ -z "$parsed" ]]; then
  echo "❌ 请输入正确的飞书文档地址，格式示例：" >&2
  echo "  https://xxx.feishu.cn/wiki/TOKEN" >&2
  echo "  https://xxx.feishu.cn/docx/TOKEN" >&2
  exit 1
fi

domain=$(echo "$parsed" | cut -d'|' -f1)
doc_type=$(echo "$parsed" | cut -d'|' -f2)
token=$(echo "$parsed" | cut -d'|' -f3)

# 获取凭证
APP_ID=$(get_config "app_id" || true)
APP_SECRET=$(get_config "app_secret" || true)

# 优先使用 user_access_token（个人权限，无需逐个文档授权）
ACCESS_TOKEN=""
if [[ -f "$USER_TOKEN_CACHE" ]]; then
  ACCESS_TOKEN=$(get_user_access_token "$APP_ID" "$APP_SECRET" || true)
fi

# 回退到 tenant_access_token（应用权限）
if [[ -z "$ACCESS_TOKEN" ]]; then
  if [[ -z "$APP_ID" || -z "$APP_SECRET" ]]; then
    echo "❌ 未找到有效的认证信息" >&2
    echo "  方式1: 运行 login.sh 进行个人授权（推荐）" >&2
    echo "  方式2: 在 ../assets/.feishu 配置 app_id + app_secret" >&2
    exit 1
  fi
  ACCESS_TOKEN=$(get_access_token "$APP_ID" "$APP_SECRET" || true)
  if [[ -z "$ACCESS_TOKEN" ]]; then
    echo "❌ 获取 access_token 失败" >&2
    exit 1
  fi
fi

# 执行操作
process "$ACTION" "$DOC_URL" "$ACCESS_TOKEN" "$doc_type" "$token" "$CONTENT_FILE"
