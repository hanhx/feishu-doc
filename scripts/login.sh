#!/usr/bin/env bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FEISHU_FILE="$SCRIPT_DIR/../assets/.feishu"
USER_TOKEN_CACHE="$SCRIPT_DIR/../assets/.user_token_cache"
API_BASE="https://open.feishu.cn/open-apis"
PORT=9999
REDIRECT_URI="http://localhost:${PORT}/callback"

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

APP_ID=$(get_config "app_id" || true)
APP_SECRET=$(get_config "app_secret" || true)

# logout
if [[ "${1:-}" == "logout" ]]; then
  if [[ -f "$USER_TOKEN_CACHE" ]]; then
    rm -f "$USER_TOKEN_CACHE"
    echo "✅ 已退出登录，token 缓存已删除"
  else
    echo "ℹ️ 未登录（无 token 缓存）"
  fi
  exit 0
fi

if [[ -z "$APP_ID" || -z "$APP_SECRET" ]]; then
  echo "❌ 未配置 app_id 或 app_secret" >&2
  exit 1
fi

echo "🔐 飞书 OAuth 登录"
echo ""
echo "📋 请确保应用已开通以下权限（飞书开放平台 → 应用 → 权限管理）："
echo "   ✅ docx:document          （读写文档）"
echo "   ✅ docx:document:readonly （只读文档）"
echo ""
echo "⚙️  安全设置 → 重定向 URL → 添加: http://localhost:${PORT}/callback"
echo ""

python3 - "$APP_ID" "$APP_SECRET" "$USER_TOKEN_CACHE" "$API_BASE" "$PORT" "$REDIRECT_URI" <<'PYTHON_SCRIPT'
import sys, json, urllib.request, urllib.error, urllib.parse
import webbrowser, http.server, time

app_id, app_secret, token_cache_path, api_base = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
port, redirect_uri = int(sys.argv[5]), sys.argv[6]

authorization_code = None

class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global authorization_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if urllib.parse.urlparse(self.path).path == "/callback" and "code" in params:
            authorization_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>✅ 授权成功！可以关闭此页面。</h2>".encode("utf-8"))
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>❌ 授权失败</h2>".encode("utf-8"))
    def log_message(self, format, *args): pass

encoded_redirect = urllib.parse.quote(redirect_uri, safe='')
auth_url = (
    f"https://open.feishu.cn/open-apis/authen/v1/authorize"
    f"?app_id={app_id}"
    f"&redirect_uri={encoded_redirect}"
    f"&scope=docx:document:readonly%20docx:document"
)

server = http.server.HTTPServer(("localhost", port), CallbackHandler)
server.timeout = 120

print(f"🌐 打开浏览器授权...")
webbrowser.open(auth_url)
print(f"⏳ 等待授权回调 (http://localhost:{port}/callback) ...")

server.handle_request()
server.server_close()

if not authorization_code:
    print("❌ 未收到授权码，请重试", file=sys.stderr)
    sys.exit(1)

print(f"✅ 收到授权码: {authorization_code[:10]}...")

# 获取 app_access_token
req0 = urllib.request.Request(
    f"{api_base}/auth/v3/app_access_token/internal",
    data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
    method="POST")
req0.add_header("Content-Type", "application/json")
with urllib.request.urlopen(req0) as resp0:
    app_token = json.loads(resp0.read().decode("utf-8")).get("app_access_token", "")
if not app_token:
    print("❌ 获取 app_access_token 失败", file=sys.stderr)
    sys.exit(1)

# 用 code 换 user_access_token
req = urllib.request.Request(
    f"{api_base}/authen/v1/oidc/access_token",
    data=json.dumps({"grant_type": "authorization_code", "code": authorization_code}).encode("utf-8"),
    method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", f"Bearer {app_token}")

try:
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    print(f"❌ 换取 token 失败 (HTTP {e.code}): {e.read().decode('utf-8')}", file=sys.stderr)
    sys.exit(1)

if result.get("code", -1) != 0:
    print(f"❌ 换取 token 失败: {result.get('msg', '')}", file=sys.stderr)
    sys.exit(1)

data = result.get("data", {})
access_token = data.get("access_token", "")
refresh_token = data.get("refresh_token", "")
expires_in = data.get("expires_in", 0)

if not access_token:
    print(f"❌ 未获取到 access_token", file=sys.stderr)
    sys.exit(1)

cache = {
    "access_token": access_token,
    "refresh_token": refresh_token,
    "expires_at": int(time.time()) + expires_in,
    "app_id": app_id,
    "app_secret": app_secret
}
with open(token_cache_path, "w") as f:
    json.dump(cache, f, indent=2)

print(f"✅ 登录成功！")
print(f"   access_token 有效期: {expires_in // 60} 分钟")
print(f"   refresh_token 有效期: 30 天")
print(f"   token 已保存到: {token_cache_path}")
PYTHON_SCRIPT
