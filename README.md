# feishu-doc 使用说明

让 AI IDE（如 OpenCode、Claude Code、Windsurf、Cursor 等）直接在终端读写飞书文档，无需打开浏览器。

**下载地址**：https://github.com/hanhx/feishu-doc

---

## 快速开始

### 1. 安装

支持三种安装方式：**AI 安装（推荐）**、**手动安装**、**软链接**。

#### 方式 1：AI 安装（推荐）

直接在 AI IDE 中告诉 AI 助手：

> "帮我安装 feishu-doc skill，仓库地址是 https://github.com/hanhx/feishu-doc"

AI 会自动识别当前 IDE 并安装到正确的目录。

#### 方式 2：手动安装

根据你使用的 AI IDE，选择对应的命令：

**OpenCode**
```bash
git clone https://github.com/hanhx/feishu-doc.git ~/.opencode/skills/feishu-doc
```

**Claude Code**
```bash
git clone https://github.com/hanhx/feishu-doc.git ~/.claude/skills/feishu-doc
```

**Windsurf**
```bash
git clone https://github.com/hanhx/feishu-doc.git ~/.codeium/windsurf/skills/feishu-doc
```

**Cursor**
```bash
git clone https://github.com/hanhx/feishu-doc.git ~/.cursor/skills/feishu-doc
```

安装完成后，AI IDE 会自动识别 `SKILL.md` 并加载该 skill。

#### 方式 3：软链接

如果你已经克隆到其他位置，可以创建软链接：

```bash
# OpenCode
ln -s /path/to/feishu-doc ~/.opencode/skills/feishu-doc

# Claude Code
ln -s /path/to/feishu-doc ~/.claude/skills/feishu-doc

# Windsurf
ln -s /path/to/feishu-doc ~/.codeium/windsurf/skills/feishu-doc

# Cursor
ln -s /path/to/feishu-doc ~/.cursor/skills/feishu-doc
```

> **💡 提示**：
> - 不同 IDE 版本的 skills 目录可能不同，请以对应 IDE 官方文档为准
> - 如果下载 zip 包，解压后文件夹名为 `feishu-doc-main`，需重命名为 `feishu-doc` 或创建软链接

### 2. 配置 App ID 和 App Secret

> 💡 **推荐**：直接联系作者获取已注册好的 App ID 和 App Secret，无需自己创建应用。
> 
> 如需自己申请，参考 [附录：自建飞书应用](#附录自建飞书应用)。

支持两种配置方式，**环境变量优先**：

**方式1：环境变量（推荐）**

在 `~/.zshrc`（Mac 默认）或 `~/.bash_profile` 中添加：

```bash
export FEISHU_APP_ID=cli_xxxx
export FEISHU_APP_SECRET=xxxx
```

保存后执行 `source ~/.zshrc` 生效。

**方式2：配置文件**

编辑 `assets/.feishu`，填入你的凭证：

```
app_id=cli_xxxx
app_secret=xxxx
```

### 3. 开始使用

配置完成后，直接在 AI IDE 聊天中告诉 AI 助手：

**读取文档**
> "帮我读一下这个飞书文档 https://xxx.feishu.cn/wiki/TOKEN"

**写入文档**
> "帮我把这个方案写到 https://xxx.feishu.cn/wiki/TOKEN"

**清空文档**
> "帮我清空 https://xxx.feishu.cn/wiki/TOKEN"

**在某章节后追加（支持模糊匹配 / 正则）**
> "在『技术方案』章节末尾追加这段内容"

**删除某个章节后重写**
> "先删除『技术方案』章节，再把这段新内容写进去"

> ⚠️ 安全保护：
> - `insert-targeted` 和 `delete-section` 默认都会先展示预览，并要求输入 `yes` 确认后才执行。
> - 非交互执行时需要显式传 `--yes`。

**首次使用时**，系统会自动检测到未登录并打开浏览器授权页，你只需点击「授权」即可。Token 过期时也会自动重新登录。

---

## 支持的格式

- 标题（H1~H9，第一个 H1 自动设为文档标题）
- 代码块（自动识别 Java、SQL、JSON、Python、Go、Shell、mermaid 等语言）
- 无序列表、有序列表
- 待办事项（`- [ ]` / `- [x]`）
- 引用（渲染为飞书 Callout 容器）
- 表格（自动拆分为飞书原生表格，每个子表最多 8 行数据 + 1 行表头，大表格无缝支持）
- 分割线
- 行内样式：**加粗**、`行内代码`、~~删除线~~、[超链接](url)

---

## 高级操作（定点插入 / 章节删除）

### 1) 定点插入（insert-targeted）

```bash
python3 scripts/index.py insert-targeted "<Feishu_URL>" "<content_file>" \
  --anchor-type heading --anchor "技术方案" --match fuzzy --position section_end
```

- `--anchor-type`：`heading`（按标题）或 `text`（按文本）
- `--match`：`fuzzy`（模糊匹配）或 `regex`（正则匹配）
- `--position`：`after`（锚点后）或 `section_end`（章节末尾，仅 heading）

### 2) 删除章节（delete-section）

```bash
python3 scripts/index.py delete-section "<Feishu_URL>" \
  --anchor "技术方案" --match fuzzy
```

删除规则：
- 先删除该章节下的所有内容（直到下一个同级或更高级标题）
- 最后删除目标标题本身

### 3) 非交互自动执行（显式确认）

```bash
# 定点插入
python3 scripts/index.py insert-targeted "<Feishu_URL>" "<content_file>" \
  --anchor-type heading --anchor "技术方案" --match fuzzy --position section_end --yes

# 删除章节
python3 scripts/index.py delete-section "<Feishu_URL>" \
  --anchor "技术方案" --match fuzzy --yes
```

---

## 常见问题

### 权限不足（forBidden）

检查：
1. 应用是否已开通 `docx:document` 和 `docx:document:readonly` 权限
2. 修改权限后是否重新发布了应用版本
3. 是否重新运行了 `login.py` 授权

### Token 过期

**会自动重新登录**，无需手动操作。如需手动重新登录或切换账号：
```bash
python3 scripts/login.py logout && python3 scripts/login.py
```

### 表格超过 9 行

飞书 API 限制单次创建表格最多 9 行。超过 9 行的表格会自动拆分为多个子表格，每个子表最多 8 行数据 + 1 行表头，均为飞书原生表格渲染。如果子表创建失败，会自动 fallback 为 markdown 代码块展示。

---

## 安全性说明

### App ID + App Secret

- **跟应用绑定，不跟个人绑定**。同一个飞书组织内的成员可以共用同一对 app_id / app_secret。
- app_id + app_secret 用于启动 OAuth 授权流程，**本身不能直接访问任何文档**。
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
| app_id + app_secret | 启动 OAuth 授权流程，本身不能访问文档 | 低 |
| user_access_token | 以个人身份读写文档（2h 过期） | 中，但需本人授权才能获取 |
| refresh_token | 刷新出新的 access_token（30 天有效） | 高，泄露等于身份冒用 |

### 团队共享建议

1. **共享 app_id + app_secret**：团队成员使用同一份 `.feishu` 配置即可
2. **各自登录**：每人运行 `login.py` 完成个人授权，token 缓存互不影响
3. **不共享 token 缓存**：`.user_token_cache` 文件仅限本人使用

---

## 附录：自建飞书应用

如果不使用作者提供的 App ID，可以自己创建飞书应用：

### 1. 创建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app)，登录后点击「创建企业自建应用」
2. 填写应用名称（如 `feishu-doc` 或 `AI IDE Doc`），创建完成后进入应用详情页
3. 记录 **App ID** 和 **App Secret**

### 2. 开通权限

进入应用详情 → **权限管理** → 搜索并开通以下权限：

| 权限标识 | 说明 | 必须 |
|---------|------|------|
| `docx:document` | 查看、编辑和管理云文档 | ✅ |
| `docx:document:readonly` | 查看云文档 | ✅ |

> **读写权限与你的个人账号一致**：授权登录后，AI 助手使用你的身份访问飞书。你能看到的文档就能读，你能编辑的文档就能写，不需要额外给应用分享文档权限。

### 3. 安全设置

进入应用详情 → **安全设置** → **重定向 URL** → 添加：

```
http://127.0.0.1:9999/callback
```

> ⚠️ 必须使用 `127.0.0.1`，不要用 `localhost`，否则会报 20029 错误。

### 4. 发布应用

进入应用详情 → **版本管理与发布** → 创建版本 → 提交发布。

> ⚠️ 每次修改权限后都需要重新发布版本，否则新权限不生效。

### 5. 手动登录（可选）

如需手动触发登录流程，在 skill 根目录执行：

```bash
python3 scripts/login.py
```

> 💡 OpenCode 常见路径：`~/.opencode/skills/feishu-doc`  
> 💡 Claude Code 常见路径：`~/.claude/skills/feishu-doc`  
> 💡 Windsurf 常见路径：`~/.codeium/windsurf/skills/feishu-doc`  
> 💡 Cursor 常见路径：`~/.cursor/skills/feishu-doc`

**Token 有效期**：
- **access_token**：2 小时，脚本自动刷新
- **refresh_token**：30 天，过期后自动重新登录
