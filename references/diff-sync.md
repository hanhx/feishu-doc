# 差异更新第一版

`sync` 比较本地目标 Markdown 和原生块快照，只修改变化的内容。默认预览；`--apply` 才会写入。原 `write` 仍表示清空后整篇重建，语义不变。

## 使用

在本 Skill 根目录执行。文档地址和文件路径都由参数传入，不依赖任何工作区任务目录。

```bash
# 只读：保存原生块、block ID、富文本样式和文档版本；文件不能已存在
python3 scripts/index.py snapshot "<Feishu_URL>" baseline.json

# desired.md 第一行必须是文档标题：# 文档标题，后续是完整目标正文
# 默认只预览，也可显式加 --dry-run；不会写飞书或创建执行状态
python3 scripts/index.py sync "<Feishu_URL>" desired.md --baseline baseline.json

# 用户已授权具体更新后才执行；每次使用全新的状态文件
python3 scripts/index.py sync "<Feishu_URL>" desired.md \
  --baseline baseline.json --apply --state sync-run.json
```

`snapshot` 保存 JSON 基线，不是 Markdown 导出。`desired.md` 是用户希望文档最终包含的完整内容；不要把有损的纯文本读取结果直接当作保留所有格式的源稿。预览返回计划中的 PATCH/POST/DELETE、块数量和索引范围；不会把 Token 或整份正文打印进诊断日志。JSON 基线含文档原文，注意按文档权限保管，不提交到代码仓库。

Docx 地址直接使用 document ID。Wiki 地址先通过 Wiki API 解析 `obj_token`，需要对应 Wiki 读取权限；不能解析时明确失败，不把 Wiki token 猜成 document ID。可使用该文档实际的 `/docx/…` 地址。

## 支持范围

- 文本、标题、扁平列表、待办、分割线、代码块，支持普通块的新增和删除。`sync` 中的 Mermaid 围栏仍按普通代码块处理。
- 比较行内样式及链接、代码语言、待办勾选状态，避免把“文字一样但样式变了”当作无变化。文字更新保留原 block ID；块类型、代码语言或待办状态改变时可能重建该块，因此其原 block ID/评论锚点不保证保留。其他未变块保持原样。
- 原有、顺序不变且行列数相同的原生表格，仅修改变化的文本单元格。一个 Markdown 表默认对应一个原生表，空单元格、转义竖线和代码内竖线均保留。基线已含旧版拆开的长表时，`sync` 可按匹配的旧表结构更新单元格，原有表格数量保持不变。
- 不支持新增/删除/重排表格、表格增删行列、合并单元格、图片、文件、引用容器、嵌套列表、文本绘图小组件或其他复杂块。遇到即在任何远端写入前停止。可用 `insert-targeted` 在含小组件的文档中定点新增内容。
- 保留原段落对齐、字体等未被 Markdown 表达的块样式，不提供任意版式编辑。不能宣称所有飞书富文本都可无损往返 Markdown。

## 基线、失败和重入

1. 基线属于一个确定的 document ID；读取时检查版本稳定，分页完整且块结构合法。
2. 预览和执行都会复核远端完整原生快照。发生变化则停止，不能自动覆盖或自动合并他人的编辑。
3. 执行前记录当前步骤为 `inflight`，写请求携带已知文档版本且关闭传输自动重试；每步成功后保存返回版本与完成编号。后续步骤前再次检查版本。
4. 执行后独立回读，核对目标内容/样式及未变块；只有验证一致才返回 `status=verified`。
5. 明确拒绝记录 `status=stopped`；连接中断、5xx 或成功响应缺少可用版本等结果不明情形记录 `status=uncertain`，保留 `inflight`。两者都返回失败，不继续后续写入。

跨多次 API 调用不是事务。部分成功时，已修改的块可能已生效，脚本不自动回滚，也不自动续跑。已有状态文件会拒绝再次执行；核查远端实际结果后，再以新快照和新状态文件生成剩余差异，不能只换文件名盲目重发。

当前实现参考飞书官方 SDK 的 [版本参数](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/docx/v1/model/create_document_block_children_request.py) 与 [块更新请求](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/docx/v1/model/update_block_request.py)。离线测试不能代替真实飞书环境的契约验证；首次实测应使用获授权的专用测试文档。

## 本地验证

```bash
python3 -m unittest discover -s tests -v
```

测试用内存 API 边界模拟分页、版本、写入、部分成功与未知结果，不连接真实飞书、不读取登录缓存。原命令的测试一并回归。
