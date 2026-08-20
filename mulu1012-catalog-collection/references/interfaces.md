# 本地文件与 WordPress 接口

## 目录

1. Windows 路径
2. 本地状态文件
3. 审核包
4. JSONL 上传契约
5. WordPress Ability
6. UUID 与幂等
7. Ability 调用脚本

## 1. Windows 路径

使用 Windows 已知“文档”目录：

```powershell
[Environment]::GetFolderPath('MyDocuments')
```

默认根目录：

```text
<文档>\目录1012题录批次\<YYYYMMDD-HHMM-任务短名>\
```

所有返回给用户的路径必须是 `C:\...`、`D:\...` 等 Windows 原生绝对路径。

## 2. 本地状态文件

```text
batch.json
search-records.jsonl
candidates.jsonl
dedup-results.jsonl
field-fill.jsonl
evidence.jsonl
ai-review.jsonl
evidence\
```

### `batch.json`

```json
{
  "schema_version": "1.0",
  "batch_uuid": "UUID",
  "idempotency_key": "catalog-local:<batch_uuid>:batch:<batch_uuid>",
  "name": "任务名称",
  "created_at": "ISO-8601",
  "contract_version": "2026-07-12.1",
  "contract_hash": "0ca4bf5a608dcfdad7a7b25895cc70390963d63684701a3e1cf26fb6795bfe8c",
  "classification_contract_version": "2026-07-14.3",
  "classification_contract_hash": "70355b8bd3b4dba0fc3a8445dc4e598e636a71e2bd7552287cd6608adc0f5bf0",
  "research_brief": {
    "topic": "中国史数据库",
    "goal": "搜集100个确认独立的新候选",
    "target_count": 100,
    "languages": ["中文", "英文"],
    "resource_types": ["数据库"],
    "inclusion_rules": "……",
    "exclusion_rules": "……",
    "priority_sources": ["官网", "建设机构", "论文"],
    "evidence_requirements": "每个非空字段记录原文依据和来源"
  }
}
```

### `search-records.jsonl`

每行记录一次发现：

```json
{
  "search_uuid": "UUID",
  "candidate_uuid": "UUID",
  "query": "检索式",
  "index_source_name": "发现页面或论文名称",
  "index_source_url": "https://example.org/list",
  "discovered_at": "ISO-8601",
  "notes": ""
}
```

### `candidates.jsonl`

```json
{
  "candidate_uuid": "UUID",
  "version_uuid": "UUID",
  "idempotency_key": "catalog-local:batch:candidate",
  "source_kind": "web",
  "source_locator": "发现页面名称 + URL",
  "discovered_at": "ISO-8601",
  "fields": {
    "title": "候选名称",
    "aliases": [],
    "entrance_url": "https://example.org"
  }
}
```

字段可在搜索阶段部分填写，但键必须属于 37 项契约。

### `dedup-results.jsonl`

```json
{
  "dedup_uuid": "UUID",
  "candidate_uuid": "UUID",
  "formal_revision": "catalog-42",
  "algorithm_version": "3.0.0",
  "local_verdict": "distinct",
  "formal_verdict": "fillable",
  "final_verdict": "fillable",
  "score": 34,
  "matches": [],
  "ai_deep_review": null,
  "checked_at": "ISO-8601"
}
```

`final_verdict`只使用：

- `duplicate`
- `suspected_duplicate`
- `fillable`
- `blocked`

### `field-fill.jsonl`

```json
{
  "candidate_uuid": "UUID",
  "version_uuid": "UUID",
  "parent_database_confirmed": false,
  "fields": {
    "title": "中文标题",
    "aliases": ["Original Title"],
    "...": "其余字段"
  },
  "field_reviews": [
    {
      "field_key": "organizer",
      "confidence": 92,
      "value": ["机构全称"],
      "evidence_excerpt": "原文摘录",
      "source_name": "项目介绍",
      "source_url": "https://example.org/about",
      "method": "direct",
      "judgment": "判断说明",
      "empty_reason": ""
    }
  ],
  "proposed_terms": [
    {
      "suggestion_uuid": "UUID",
      "field_key": "research_topic",
      "taxonomy": "research_topic",
      "term_name": "德国犹太史",
      "parent_path": "",
      "rationale": "判断说明",
      "source_url": "https://example.org/about",
      "idempotency_key": "catalog-local:term:UUID"
    }
  ]
}
```

`method`只使用`direct`或`inference`。空字段的`empty_reason`必须非空。

`primary_category`必须使用正式主分类词。无法归类时字段填`待分类`，拟新增词写入
`proposed_terms`供人工批准。`featured_sources`使用正式特色史料词；非正式短语
必须有对应`proposed_terms`记录。`database_type`只能使用`综合库`、`专题库`、
`知识库`或留空，不能通过`proposed_terms`新增。

`official_introduction`和`body_introduction`是可包含多行 Markdown 的完整字符串：

- JSONL 使用标准 JSON 换行转义保存，不得压成单行摘要或截断。
- `official_introduction`同时建议原样保存到
  `evidence/<candidate_uuid>-official-introduction.md`，便于人工逐段核对。
- `body_introduction`可保存到
  `evidence/<candidate_uuid>-body-introduction.md`作为翻译核对副本。
- 原文快照的 SHA-256 写入对应`evidence.jsonl`记录的`snapshot_hash`。
- 证据记录的`excerpt`保持为代表性短摘录；它与官网介绍全文不是同一内容层级。
- 若单个 Excel 单元格达到格式限制，`field-fill.jsonl`、Markdown 快照、
  `题录总表.csv`和`wordpress-upload.jsonl`仍必须保留全文；XLSX 单元格写明完整
  Markdown 快照路径，不得静默截断。

`authorized_institutions`、`authorized_libraries`和`library_names`在本文件中
可保存结构化对象数组。`build-review-package.mjs`会把每个对象转换为一行
中文标注文本，再写入 XLSX、CSV 和 WordPress 上传字段；三者的显示值必须一致。

### `evidence.jsonl`

```json
{
  "evidence_uuid": "UUID",
  "candidate_uuid": "UUID",
  "idempotency_key": "catalog-local:evidence:UUID",
  "field_keys": ["organizer", "summary"],
  "source_type": "web",
  "url": "https://example.org/about",
  "page_title": "About",
  "excerpt": "原文摘录",
  "translation": "必要译文",
  "accessed_at": "YYYY-MM-DD",
  "support_type": "supports",
  "note": "字段与证据关系说明",
  "snapshot_hash": ""
}
```

### `ai-review.jsonl`

```json
{
  "review_uuid": "UUID",
  "candidate_uuid": "UUID",
  "idempotency_key": "catalog-local:review:UUID",
  "score": 91,
  "rationale": "独立审核理由",
  "independent": true,
  "blockers": [],
  "suggested_state": "green",
  "reviewed_at": "ISO-8601"
}
```

## 3. 审核包

输出：

```text
任务总结.md
题录审核.xlsx
题录总表.csv
wordpress-upload.jsonl
upload-manifest.json
upload-receipt.json
```

生成和验证 XLSX 前调用`load_workspace_dependencies`，把返回的 Node packages
绝对路径作为两个 MJS 脚本的`--node-modules`参数；不得猜测或硬编码运行库路径。

`题录审核.xlsx`固定两个工作表：

### `题录总表`

- 只使用固定 37 个表头，顺序与字段契约完全一致。
- 包含全部候选。
- 重复和待判重项保留已知字段；不为它们补齐 37 项。
- 多值用中文分号`；`连接，结构化授权每条一行。

### `查重明细`

固定列：

```text
序号
候选UUID
标题
最终结论
最高分
本地互重结论
正式库结论
正式库修订号
匹配题录
逐项依据
独立反证
AI深审评分
阻断项
索引来源
检查时间
```

`题录总表.csv`使用 UTF-8 BOM，表头和行数与 XLSX `题录总表`一致。

`任务总结.md`包括任务说明、搜索统计、查重统计、填充统计、阻断项、拟新增词项、输出文件和上传状态。上传后追加 WordPress 批次与回读结果，不覆盖原内容。

`upload-receipt.json`由上传脚本创建，记录每个传输分片的内容哈希、成功、复用、失败、复查和回读结果。它用于中断续传，不属于 WordPress 正式题录。

## 4. JSONL 上传契约

`wordpress-upload.jsonl`每行一条：

```json
{
  "candidate_uuid": "UUID",
  "version_uuid": "UUID",
  "idempotency_key": "catalog-local:batch:candidate",
  "source_kind": "local_collection",
  "source_locator": "索引来源名称 + URL",
  "parent_database_confirmed": false,
  "fields": {},
  "dedup": {
    "run_uuid": "UUID",
    "formal_revision": "catalog-42",
    "algorithm_version": "3.0.0",
    "verdict": "fillable",
    "matches": []
  },
  "evidence": [],
  "ai_review": {},
  "proposed_terms": [],
  "blockers": []
}
```

`upload-manifest.json`保存：

- 批次信息和研究说明。
- 批次 UUID 和批次幂等键。
- 候选总数及各结论数量。
- 正式库修订号。
- 字段契约版本和哈希。
- 填写规则契约版本和哈希。
- `wordpress-upload.jsonl`的 SHA-256。
- 生成时间和审核包文件名。

## 5. WordPress Ability

### `mulu1012-catalog/deduplicate-candidates`

输入：

```json
{
  "candidates": [
    {
      "candidate_uuid": "UUID",
      "fields": {}
    }
  ]
}
```

- 每次 1 至 500 条。
- 只读。
- 返回当前正式库修订号、算法版本，以及每条结论、分数、匹配题录和逐项依据。
- 不创建批次、候选或查重记录。

### `mulu1012-catalog/import-local-batch`

输入：

```json
{
  "batch": {
    "batch_uuid": "UUID",
    "name": "批次名称",
    "idempotency_key": "catalog-local:batch:UUID",
    "formal_revision": "catalog-42",
    "contract_version": "2026-07-12.1",
    "contract_hash": "0ca4bf5a608dcfdad7a7b25895cc70390963d63684701a3e1cf26fb6795bfe8c",
    "classification_contract_version": "2026-07-14.3",
    "classification_contract_hash": "70355b8bd3b4dba0fc3a8445dc4e598e636a71e2bd7552287cd6608adc0f5bf0",
    "research_brief": {}
  },
  "chunk_index": 1,
  "chunk_count": 3,
  "final_chunk": false,
  "candidates": []
}
```

- 每片最多 500 条。
- 同一逻辑批次的所有片段复用相同批次 UUID 和幂等键。
- 每条候选独立返回 `created`、`reused` 或 `failed`。
- 相同幂等键重试不重复创建。
- 修订号或查重算法变化时，只重新检查本片缓存过期候选。
- 不批准、不发布、不创建 `sites` 文章。

## 6. UUID 与幂等

UUID 是本地记录和 WordPress 记录之间的稳定身份：

- 一个对象在重试、分片和回读中始终使用同一个 UUID。
- 批次、候选、版本、查重、证据、审核和拟新增词分别使用 UUID。

幂等键用于防止重复写入：

- 同一个操作重试时复用原键。
- 不同对象或不同操作不得共用一个键。
- 推荐格式：`catalog-local:<batch_uuid>:<object_type>:<object_uuid>`。
- 键最长 191 个字符。

## 7. Ability 调用脚本

脚本：

```text
wp_ability.py
wp-ability-runner.php
wordpress-dedup.py
wordpress-upload.py
```

支持两种传输：

- `studio`：本地 WordPress Studio；传入`--site-path`和具有相应权限的`--wp-user`。
- `rest`：远程 WordPress；传入`--site-url`和`--wp-user`，只从环境变量`MULU1012_WP_APP_PASSWORD`读取 Application Password。远程站必须使用 HTTPS；只有`localhost`、`127.0.0.1`和`::1`测试地址允许 HTTP。

不得把 Application Password 写入命令参数、批次文件、任务总结或日志。

Studio 的`--wp-user`可使用用户 ID、登录名或邮箱；全数字登录名与用户 ID
冲突时使用`login:1012`或`id:1`明确指定。REST 的`--wp-user`填写实际登录名。

WordPress 对`readonly: true` Ability 的 REST 运行接口要求 GET。查重脚本只发送
查重算法实际使用的字段，并按编码后的 GET URL 长度自动分片；这只是传输分片，
不改变全任务查重范围。`--max-rest-url-length`默认 7000，Studio 通道仍可一次
提交最多 500 条。

正式库批量查重：

```powershell
python scripts\wordpress-dedup.py `
  --batch-dir "C:\...\批次目录" `
  --transport studio `
  --site-path "D:\projects\mulu1012\wordpress" `
  --wp-user "1012"
```

输出`formal-dedup-results.jsonl`。如果多个分片返回的正式库修订号不同，脚本自动重新执行整批查重，避免把不同版本的正式库结果混在一起。

上传前可只检查计划：

```powershell
python scripts\wordpress-upload.py `
  --batch-dir "C:\...\批次目录" `
  --dry-run
```

用户明确确认后上传：

```powershell
python scripts\wordpress-upload.py `
  --batch-dir "C:\...\批次目录" `
  --confirm-upload `
  --transport studio `
  --site-path "D:\projects\mulu1012\wordpress" `
  --wp-user "1012"
```

上传脚本默认每片 100 条，允许 1 至 500 条。它使用同一批次 UUID 和幂等键续传，完成后调用`get-batch`与`get-candidate`回读，并把结果写入`upload-receipt.json`和`任务总结.md`。
