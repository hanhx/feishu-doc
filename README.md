# feishu-doc

让 AI 助手读取和更新飞书新版文档。支持将正文读成 Markdown、按章节插入内容、预览并应用差异、写入原生长表格和可编辑 Mermaid。也可以读取文档里的附件与画板，或将 draw.io 文件导入飞书画板。

正文读写使用飞书开放平台 API。资源读取和画板导入还会使用独立的 Chrome 登录会话。

## 快速开始

### 1. 安装

需要 Git 和 Python 3。使用 Codex 时，可以安装到个人 Skills 目录：

```bash
git clone https://github.com/hanhx/feishu-doc.git ~/.agents/skills/feishu-doc
cd ~/.agents/skills/feishu-doc
```

| 工具 | 个人 Skill 安装目录 |
| --- | --- |
| [Codex](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills) | `~/.agents/skills/feishu-doc` |
| [Claude Code](https://code.claude.com/docs/en/skills) | `~/.claude/skills/feishu-doc` |
| [OpenCode](https://opencode.ai/docs/skills) | `~/.config/opencode/skills/feishu-doc` |
| [Cursor](https://cursor.com/docs/skills) | `~/.cursor/skills/feishu-doc` |

按所用工具选择一个安装目录。已有本地检出时，也可以在支持软链接的工具中将安装目录指向它。手动安装时，保留整个目录，确保 `SKILL.md`、`scripts/` 和 `assets/` 同级。也可以把仓库地址发给 AI 助手，请它安装到当前工具的 Skills 目录。

### 2. 配置飞书应用并登录

在[飞书开放平台](https://open.feishu.cn/app)创建或选用已获授权的企业自建应用。

1. 按需申请文档读取权限 `docx:document:readonly` 和编辑权限 `docx:document`。使用 `/wiki/…` 链接时，还需要应用具备[获取知识空间节点信息](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/get_node)所需的权限。
2. 在应用的安全设置中添加重定向 URL `http://127.0.0.1:9999/callback`，发布包含这些设置的应用版本。
3. 在 Skill 根目录复制配置样例，在本机填写应用的 App ID 和 App Secret，然后完成浏览器授权。

```bash
cp -n assets/.feishu.example assets/.feishu.local
# 编辑 assets/.feishu.local，填入 app_id 和 app_secret
python3 scripts/login.py
```

也可以通过运行脚本的环境提供 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。环境变量优先于本地文件。脚本以登录用户身份访问文档，读取和编辑还取决于应用权限及该用户对目标文档的权限。凭据和登录缓存保存在本机。

### 3. 读取第一篇文档

在助手里提供链接并说明任务，例如“读取这篇飞书文档，整理结论和待办”。也可以直接运行：

```bash
python3 scripts/index.py read "https://example.feishu.cn/wiki/EXAMPLE_TOKEN"
```

支持 `/wiki/…` 和 `/docx/…` 链接。将示例替换为有访问权限的实际地址。返回的 JSON 包含标题、Markdown 正文和 `assets` 资源清单。如果命令提示登录完成，重新执行原命令。旧版 `/docs/doccn…` 文档需先转换为新版。

## 更新文档

以下命令在 Skill 根目录执行。写入前确认目标文档和修改范围。

| 目标 | 命令 | 行为 |
| --- | --- | --- |
| 在文末补充内容 | `append` | 追加 Markdown，保留已有正文和标题 |
| 按标题或文字定位 | `insert-targeted` | 预览插入位置，确认后写入 |
| 更新支持的正文或表格单元格 | `snapshot` / `sync` | 保存基线，先预览差异，再显式应用 |
| 覆盖整篇正文 | `write` | 清空正文并重新写入，将首个 H1 用作文档标题 |
| 删除一个章节或清空正文 | `delete-section` / `clear` | 按指定范围删除 |

例如，在「技术方案」章节末尾插入本地 Markdown：

```bash
python3 scripts/index.py insert-targeted "https://example.feishu.cn/wiki/EXAMPLE_TOKEN" update.md \
  --anchor-type heading --anchor "技术方案" --match fuzzy --position section_end
```

写入支持标题、列表、待办、引用、代码块和常用行内样式。每个 Markdown 表格写成一张飞书原生表格，超过 9 行时会在同一张表中追加行。通过 `write`、`append` 或 `insert-targeted` 写入的 `mermaid` 围栏会生成可编辑的飞书文本绘图，读取时可恢复成 Mermaid 源码。

### 预览差异

```bash
python3 scripts/index.py snapshot "https://example.feishu.cn/wiki/EXAMPLE_TOKEN" baseline.json
python3 scripts/index.py sync "https://example.feishu.cn/wiki/EXAMPLE_TOKEN" desired.md --baseline baseline.json
```

`desired.md` 包含完整目标正文，首行为 `# 文档标题`。支持常见正文块及行列结构相同的表格单元格。确认预览后，使用一个尚不存在的状态文件执行：

```bash
python3 scripts/index.py sync "https://example.feishu.cn/wiki/EXAMPLE_TOKEN" desired.md \
  --baseline baseline.json --apply --state sync-run.json
```

文本绘图小组件和表格结构变化的处理方式见[差异更新说明](references/diff-sync.md)。

## 读取附件和导入画板

在 macOS 或 Linux 上准备 Node.js 20+ 和 Google Chrome，然后在 Skill 根目录执行 `npm ci`。读取资源时 Chrome 默认在后台运行，检测到飞书登录页才打开窗口。登录后同一条命令继续。

需要同时读取正文和多个资源时，使用 `read --with-assets`。它在同一个 Chrome 页面中读取资源，并在 JSON 的 `assetResults` 中返回各块的结果和输出路径。可重复传入 `--block-id` 限定范围。

```bash
python3 scripts/index.py read "https://example.feishu.cn/wiki/EXAMPLE_TOKEN" --with-assets
```

单独读取资源时，从 `read` 返回的 `assets` 中找到目标 `blockId`。可保存图片、画板或内嵌表格的可见预览，下载 Excel 附件，或将多维表格当前视图的可见数据导出为 TSV。

```bash
python3 scripts/index.py read-asset "https://example.feishu.cn/wiki/EXAMPLE_TOKEN" \
  --block-id "EXAMPLE_BLOCK_ID" --output preview.png
```

下面的命令预览将 draw.io 文件导入「架构方案」标题后的操作。核对位置后按提示确认。使用 `--replace-block-id` 可以在原画板中替换图形，旧图形会被清除。

```bash
python3 scripts/index.py import-drawio "https://example.feishu.cn/wiki/EXAMPLE_TOKEN" diagram.drawio \
  --anchor "架构方案" --output board-preview.png
```

导入完成后打开截图核对节点、文字和连线。其他参数、适用范围及失败后的回读步骤见 [Skill 指南](SKILL.md)。

## 常见问题

- **提示权限不足**。核对应用权限和版本是否已发布，以及当前飞书账号能否直接访问目标文档。如果只有 `/wiki/…` 链接报错，检查知识库节点的读取权限。
- **个人授权过期或需要切换账号**。运行 `python3 scripts/login.py logout`，再运行 `python3 scripts/login.py`。附件和画板使用 Chrome 中的独立登录会话。
- **操作报错后不确定是否已写入**。先回读目标章节或画板，确认已经生效的内容，再处理剩余部分。
