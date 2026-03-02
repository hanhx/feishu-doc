#!/usr/bin/env python3

import sys
import os
import json
import time
import urllib.request
import urllib.error
import urllib.parse
import webbrowser
import http.server

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FEISHU_FILE = os.path.join(SCRIPT_DIR, "..", "assets", ".feishu")
USER_TOKEN_CACHE = os.path.join(SCRIPT_DIR, "..", "assets", ".user_token_cache")
API_BASE = "https://open.feishu.cn/open-apis"
PORT = 9999
REDIRECT_URI = f"http://127.0.0.1:{PORT}/callback"


def get_config(key):
    env_map = {"app_id": "FEISHU_APP_ID", "app_secret": "FEISHU_APP_SECRET"}
    env_val = os.environ.get(env_map.get(key, ""), "")
    if env_val:
        return env_val
    if not os.path.isfile(FEISHU_FILE):
        return ""
    with open(FEISHU_FILE, "r") as f:
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


def main():
    app_id = get_config("app_id")
    app_secret = get_config("app_secret")

    # logout
    if len(sys.argv) > 1 and sys.argv[1] == "logout":
        if os.path.isfile(USER_TOKEN_CACHE):
            os.remove(USER_TOKEN_CACHE)
            print("✅ 已退出登录，token 缓存已删除")
        else:
            print("ℹ️ 未登录（无 token 缓存）")
        return

    if not app_id or not app_secret:
        print("❌ 未配置 app_id 或 app_secret", file=sys.stderr)
        sys.exit(1)

    print("🔐 飞书 OAuth 登录")
    print()
    print("📋 请确保应用已开通以下权限（飞书开放平台 → 应用 → 权限管理）：")
    print("   ✅ docx:document          （读写文档）")
    print("   ✅ docx:document:readonly （只读文档）")
    print()
    print(f"⚙️  安全设置 → 重定向 URL → 添加: http://127.0.0.1:{PORT}/callback")
    print()

    authorization_code = [None]

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path == "/callback" and "code" in params:
                authorization_code[0] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                success_html = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>授权成功 - feishu-doc</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 16px;
            padding: 48px 40px;
            max-width: 480px;
            width: 100%;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            text-align: center;
            animation: slideUp 0.5s ease-out;
        }
        @keyframes slideUp {
            from { opacity: 0; transform: translateY(30px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .icon {
            width: 80px;
            height: 80px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 24px;
            animation: scaleIn 0.6s ease-out 0.2s both;
        }
        @keyframes scaleIn {
            from { transform: scale(0); }
            to { transform: scale(1); }
        }
        .icon svg {
            width: 48px;
            height: 48px;
            stroke: white;
            stroke-width: 3;
            fill: none;
            stroke-linecap: round;
            stroke-linejoin: round;
        }
        h1 {
            font-size: 28px;
            color: #1a202c;
            margin-bottom: 12px;
            font-weight: 600;
        }
        p {
            font-size: 16px;
            color: #718096;
            line-height: 1.6;
            margin-bottom: 32px;
        }
        .info {
            background: #f7fafc;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 24px;
            text-align: left;
        }
        .info-item {
            display: flex;
            align-items: center;
            margin-bottom: 12px;
            font-size: 14px;
            color: #4a5568;
        }
        .info-item:last-child { margin-bottom: 0; }
        .info-item svg {
            width: 20px;
            height: 20px;
            margin-right: 12px;
            stroke: #667eea;
            stroke-width: 2;
            fill: none;
        }
        .close-btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 14px 32px;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 500;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
            width: 100%;
        }
        .close-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(102, 126, 234, 0.4);
        }
        .close-btn:active {
            transform: translateY(0);
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="icon">
            <svg viewBox="0 0 24 24">
                <polyline points="20 6 9 17 4 12"></polyline>
            </svg>
        </div>
        <h1>授权成功！</h1>
        <p>feishu-doc 已成功获得访问权限，现在可以开始使用了。</p>
        <div class="info">
            <div class="info-item">
                <svg viewBox="0 0 24 24">
                    <circle cx="12" cy="12" r="10"></circle>
                    <polyline points="12 6 12 12 16 14"></polyline>
                </svg>
                <span>Token 有效期：2 小时（自动刷新）</span>
            </div>
            <div class="info-item">
                <svg viewBox="0 0 24 24">
                    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
                </svg>
                <span>权限范围：你的飞书文档读写权限</span>
            </div>
        </div>
        <button class="close-btn" onclick="window.close()">关闭此页面</button>
    </div>
</body>
</html>
                """
                self.wfile.write(success_html.encode("utf-8"))
            else:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                error_html = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>授权失败 - feishu-doc</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 16px;
            padding: 48px 40px;
            max-width: 480px;
            width: 100%;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            text-align: center;
        }
        .icon {
            width: 80px;
            height: 80px;
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 24px;
        }
        .icon svg {
            width: 48px;
            height: 48px;
            stroke: white;
            stroke-width: 3;
            fill: none;
        }
        h1 {
            font-size: 28px;
            color: #1a202c;
            margin-bottom: 12px;
            font-weight: 600;
        }
        p {
            font-size: 16px;
            color: #718096;
            line-height: 1.6;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="icon">
            <svg viewBox="0 0 24 24">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="15" y1="9" x2="9" y2="15"></line>
                <line x1="9" y1="9" x2="15" y2="15"></line>
            </svg>
        </div>
        <h1>授权失败</h1>
        <p>请返回终端重新尝试授权流程。</p>
    </div>
</body>
</html>
                """
                self.wfile.write(error_html.encode("utf-8"))

        def log_message(self, format, *args):
            pass

    encoded_redirect = urllib.parse.quote(REDIRECT_URI, safe="")
    auth_url = (
        f"https://open.feishu.cn/open-apis/authen/v1/authorize"
        f"?app_id={app_id}"
        f"&redirect_uri={encoded_redirect}"
        f"&scope=docx:document:readonly%20docx:document"
    )

    server = http.server.HTTPServer(("127.0.0.1", PORT), CallbackHandler)
    server.timeout = 120

    print("🌐 打开浏览器授权...")
    webbrowser.open(auth_url)
    print(f"⏳ 等待授权回调 (http://127.0.0.1:{PORT}/callback) ...")

    server.handle_request()
    server.server_close()

    if not authorization_code[0]:
        print("❌ 未收到授权码，请重试", file=sys.stderr)
        sys.exit(1)

    print(f"✅ 收到授权码: {authorization_code[0][:10]}...")

    # 获取 app_access_token
    req0 = urllib.request.Request(
        f"{API_BASE}/auth/v3/app_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        method="POST",
    )
    req0.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req0) as resp0:
        app_token = json.loads(resp0.read().decode("utf-8")).get("app_access_token", "")
    if not app_token:
        print("❌ 获取 app_access_token 失败", file=sys.stderr)
        sys.exit(1)

    # 用 code 换 user_access_token
    req = urllib.request.Request(
        f"{API_BASE}/authen/v1/oidc/access_token",
        data=json.dumps({"grant_type": "authorization_code", "code": authorization_code[0]}).encode("utf-8"),
        method="POST",
    )
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
        print("❌ 未获取到 access_token", file=sys.stderr)
        sys.exit(1)

    cache = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + expires_in,
        "app_id": app_id,
        "app_secret": app_secret,
    }
    with open(USER_TOKEN_CACHE, "w") as f:
        json.dump(cache, f, indent=2)

    print("✅ 登录成功！")
    print(f"   access_token 有效期: {expires_in // 60} 分钟")
    print("   refresh_token 有效期: 30 天")
    print(f"   token 已保存到: {USER_TOKEN_CACHE}")


if __name__ == "__main__":
    main()
