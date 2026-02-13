# feishu-doc 使用说明

让 Windsurf（Cascade）直接在终端读写飞书文档，无需打开浏览器。

---

## 一、飞书应用配置

### 1.1 创建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app)，登录后点击「创建企业自建应用」
2. 填写应用名称（如 `Windsurf Doc`），创建完成后进入应用详情页
3. 记录 **App ID** 和 **App Secret**（后续配置需要）

### 1.2 开通权限

进入应用详情 → **权限管理** → 搜索并开通以下权限：

| 权限标识 | 说明 | 必须 |
|---------|------|------|
| `docx:document` | 查看、编辑和管理云文档 | ✅ |
| `docx:document:readonly` | 查看云文档 | ✅ |

> **读写权限与你的个人账号一致**：授权登录后，Cascade 使用你的身份访问飞书。你能看到的文档就能读，你能编辑的文档就能写，不需要额外给应用分享文档权限。

### 1.3 安全设置

进入应用详情 → **安全设置** → **重定向 URL** → 添加：

```
http://127.0.0.1:9999/callback
```

> ⚠️ 必须使用 `127.0.0.1`，不要用 `localhost`，否则会报 20029 错误。

### 1.4 发布应用

进入应用详情 → **版本管理与发布** → 创建版本 → 提交发布。

> ⚠️ 每次修改权限后都需要重新发布版本，否则新权限不生效。

---

## 二、本地配置

支持两种方式配置应用凭证，**环境变量优先**。

### 方式1：环境变量（推荐）

在 `~/.bash_profile`（或 `~/.zshrc`）中添加：

```bash
export FEISHU_APP_ID=cli_xxxx
export FEISHU_APP_SECRET=xxxx
```

保存后执行 `source ~/.bash_profile` 生效。凭证不会进入代码仓库，安全可靠。

### 方式2：配置文件

编辑 `assets/.feishu`，填入你的凭证：

```
app_id=cli_xxxx
app_secret=xxxx
```

---

## 三、授权登录

### 首次登录

在 Windsurf 终端执行：

```bash
cd ~/.codeium/windsurf/skills/feishu-doc && python3 scripts/login.py
```

脚本会：
1. 自动打开浏览器跳转飞书授权页
2. 你点击「授权」后，token 自动保存到本地
3. 终端显示 `✅ 登录成功！` 即完成

### Token 有效期

- **access_token**：2 小时，脚本自动用 refresh_token 刷新，无需手动操作
- **refresh_token**：30 天，过期后需重新登录

### 退出登录

```bash
cd ~/.codeium/windsurf/skills/feishu-doc && python3 scripts/login.py logout
```

### 重新登录

token 过期或需要切换账号时：

```bash
cd ~/.codeium/windsurf/skills/feishu-doc && python3 scripts/login.py logout && python3 scripts/login.py
```

---

## 四、如何让 Cascade 写飞书文档

授权完成后，你可以直接在 Windsurf 聊天中告诉 Cascade：

### 读取文档

> "帮我读一下这个飞书文档 https://xxx.feishu.cn/wiki/TOKEN"

Cascade 会调用 skill 读取文档内容，返回 markdown 格式。

### 写入文档

> "帮我把这个方案写到 https://xxx.feishu.cn/wiki/TOKEN"

Cascade 会：
1. 先确认是否清空文档（不会自动清空）
2. 将内容转为飞书原生格式写入

### 清空文档

> "帮我清空 https://xxx.feishu.cn/wiki/TOKEN"

清空文档的标题和所有内容。

### 写入支持的格式

- 标题（H1 ~ H6，第一个 H1 自动设为文档标题）
- 代码块（自动识别 Java、SQL、JSON、Python、Go、Shell 等语言）
- 无序列表、有序列表
- 待办事项（`- [ ]` / `- [x]`）
- 引用（渲染为飞书 Callout）
- 表格（≤9 行用飞书原生表格，>9 行用代码块展示）
- 分割线
- 行内样式：**加粗**、`行内代码`、~~删除线~~、[超链接](url)

---

## 五、常见问题

### 权限不足（forBidden）

检查：
1. 应用是否已开通 `docx:document` 和 `docx:document:readonly` 权限
2. 修改权限后是否重新发布了应用版本
3. 是否重新运行了 `login.py` 授权

### Token 过期

运行重新登录命令：
```bash
cd ~/.codeium/windsurf/skills/feishu-doc && python3 scripts/login.py logout && python3 scripts/login.py
```

### 表格超过 9 行

飞书 API 限制单个表格最多 9 行。超过 9 行的表格会自动以 markdown 代码块形式展示，内容不会丢失。

---

## 六、安全性说明

### App ID + App Secret

- **跟应用绑定，不跟个人绑定**。同一个飞书组织内的成员可以共用同一对 app_id / app_secret。
- 仅凭 app_id + app_secret 只能获取 `tenant_access_token`（应用身份），只能访问**明确分享给应用的文档**，无法访问任何人的私人文档。
- 建议：**不要提交到公开仓库**，通过 `.gitignore` 忽略 `assets/.feishu` 文件。

### User Access Token

- 必须由用户本人在浏览器中**点击授权**才能获得，仅凭 app_id + app_secret 无法伪造。
- 权限范围与你的飞书账号一致：你能编辑的文档才能写，你能查看的文档才能读。
- 有效期 2 小时，脚本自动刷新。

### Refresh Token

- 用于刷新 access_token，有效期 30 天。
- ⚠️ **泄露 refresh_token = 身份被冒用**。请妥善保管 `assets/.user_token_cache` 文件，不要分享给他人。

### 凭证风险总结

| 凭证 | 能做什么 | 泄露风险 |
|------|---------|---------|
| app_id + app_secret | 获取应用 token，仅访问授权给应用的文档 | 低 |
| user_access_token | 以个人身份读写文档（2h 过期） | 中，但需本人授权才能获取 |
| refresh_token | 刷新出新的 access_token（30 天有效） | 高，泄露等于身份冒用 |

### 团队共享建议

1. **共享 app_id + app_secret**：团队成员使用同一份 `.feishu` 配置即可
2. **各自登录**：每人运行 `login.py` 完成个人授权，token 缓存互不影响
3. **不共享 token 缓存**：`.user_token_cache` 文件仅限本人使用
