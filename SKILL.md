---
name: feishu-doc
description: Reads and writes Feishu/Lark wiki and docx documents via Open API (app_id/app_secret). Supports large tables (auto-split into ≤9-row native tables). Use when the user provides a Feishu document URL and needs to read or update its content. Run via python3 in terminal, do not open browser.
---

# feishu-doc

通过飞书开放平台 Open API 读取和写入飞书文档内容，输出结构化 JSON（包含 markdown 格式的文档内容）。

**重要**：通过 `scripts/` 目录下的 Python 脚本获取/修改文档内容，**无需打开浏览器**。请直接在终端执行脚本。

## When to Use

- User provides a Feishu wiki/docx URL and needs the document content
- Reading Feishu documents as requirements or design references
- Writing or appending content to Feishu documents

## 调用方式

在 `feishu-doc` skill 根目录执行以下命令（`SKILL.md` 与 `scripts/` 同级）。

如不在 skill 根目录，可先进入目录：
- Windsurf: `cd ~/.codeium/windsurf/skills/feishu-doc`
- Cursor: `cd ~/.cursor/skills/feishu-doc`

**读取文档**：
```bash
python3 scripts/index.py read "<Feishu_URL>"
```

**清空文档**（删除所有内容，写入前需先确认再清空）：
```bash
python3 scripts/index.py clear "<Feishu_URL>"
```

**写入文档**（全量写入，第一个 H1 设为文档标题）：
```bash
python3 scripts/index.py write "<Feishu_URL>" "<content_file>"
```

**追加内容**（在文档末尾追加，不修改标题和已有内容）：
```bash
python3 scripts/index.py append "<Feishu_URL>" "<content_file>"
```

**定点插入**（按标题/文本定位，支持模糊匹配和正则）：
```bash
python3 scripts/index.py insert-targeted "<Feishu_URL>" "<content_file>" \
  --anchor-type heading --anchor "技术方案" --match fuzzy --position section_end
```

**删除章节**（先删子章节/内容，再删标题）：
```bash
python3 scripts/index.py delete-section "<Feishu_URL>" \
  --anchor "技术方案" --match fuzzy
```

> ⚠️ `insert-targeted` 和 `delete-section` 默认都需要确认：会先输出预览，输入 `yes` 才执行。
> 非交互场景请显式传 `--yes`。

## URL Format

支持格式：
- `https://xxx.feishu.cn/wiki/TOKEN`
- `https://xxx.feishu.cn/docx/TOKEN`

示例：`https://nio.feishu.cn/wiki/XPK4w1xGDi4ntbkdc2Mcu1FVnac`

## 认证配置

支持两种认证方式，优先使用个人授权。

### 方式1：个人授权（推荐）

使用 `user_access_token`，能访问你个人有权限的所有文档，无需逐个授权。

**前置步骤**：
1. 在 https://open.feishu.cn/app 创建**企业自建应用**
2. 申请权限：`docx:document:readonly`、`docx:document`
3. 安全设置 → 重定向 URL → 添加 `http://127.0.0.1:9999/callback`
4. 发布应用

**首次登录**：
```bash
python3 scripts/login.py
```
浏览器会自动打开飞书授权页，点击授权后 token 自动保存。

- access_token 有效期 2 小时，脚本自动用 refresh_token 刷新
- refresh_token 有效期 30 天，过期后重新运行 `login.py`

**退出登录**：
```bash
python3 scripts/login.py logout
```

**重新登录**（token 过期或切换账号）：
```bash
python3 scripts/login.py logout && python3 scripts/login.py
```

### 方式2：应用授权

使用 `tenant_access_token`，需要将文档/知识库分享给应用。

**前置步骤**：同方式1的步骤1-2，然后在飞书中将文档「分享」给该应用。

### .feishu 格式

```
app_id=cli_xxxx
app_secret=xxxx
```

## 输出结构

**读取（read）**：
```json
{
  "docUrl": "https://...",
  "title": "文档标题",
  "blockCount": 42,
  "markdown": "# 文档标题\n\n文档内容...",
  "rawContent": "纯文本内容..."
}
```

**清空（clear）**：
```json
{
  "docUrl": "https://...",
  "action": "clear",
  "blocksDeleted": 42,
  "status": "success"
}
```

**写入（write）**：
```json
{
  "docUrl": "https://...",
  "action": "write",
  "blocksAdded": 5,
  "status": "success"
}
```

**定点插入（insert-targeted）**：
```json
{
  "docUrl": "https://...",
  "action": "insert-targeted",
  "anchor": "技术方案",
  "position": "section_end",
  "blocksAdded": 3,
  "status": "success"
}
```

**删除章节（delete-section）**：
```json
{
  "docUrl": "https://...",
  "action": "delete-section",
  "anchor": "技术方案",
  "blocksDeleted": 8,
  "status": "success"
}
```

## 写入支持的格式

- 标题（H1~H9，第一个 H1 自动设为文档标题）
- 代码块（自动识别 Java、SQL、JSON、Python、Go、Shell、mermaid 等语言）
- 无序列表、有序列表
- 待办事项（`- [ ]` / `- [x]`）
- 引用（渲染为飞书 Callout 容器）
- 表格（自动拆分为飞书原生表格，每个子表最多 8 行数据 + 1 行表头，大表格无缝支持）
- 分割线
- 行内样式：**加粗**、`行内代码`、~~删除线~~、[超链接](url)

## 前置条件

- 需 python3（macOS/Linux 自带，Windows 需安装）
- 飞书开放平台应用需有目标文档的访问权限
