---
name: mulu1012-catalog-collection
description: 搜集、查重、填充、审核并批量导入“目录1012”历史学数据库题录。用于用户要求搜集中国史、世界史、数字人文、史料、档案、报刊、人物、地理信息等数据库或数字资源，生成题录审核 XLSX/CSV/JSONL，或把已确认的本地题录批次上传到 WordPress 题录工作台时。
---

# 目录1012题录搜集

严格按三个阶段执行。搜索、查重和填充期间，以本地批次文件为临时状态源；成功上传后，以 WordPress 工作台为正式权威状态源。

## 开始任务

1. 读取 [references/workflow.md](references/workflow.md) 确认目标数量、搜索边界、查重裁决和暂停点。
2. 读取 [references/field-rules.md](references/field-rules.md) 使用唯一的 37 项契约填充字段与分类法。
3. 读取 [references/taxonomy-rules.md](references/taxonomy-rules.md) 和 [references/site-catalog-vocabulary.md](references/site-catalog-vocabulary.md)；其中站点类型、主分类、特色史料和数据库类型规则由《总目.xlsx》填写指南与 WordPress 正式词项角色归纳，是这四项的唯一判定依据。
4. 读取 [references/interfaces.md](references/interfaces.md) 创建本地文件、审核包和 Ability 输入。
5. 使用 Windows 已知“文档”目录下的 `目录1012题录批次`；不得向用户返回 WSL、沙箱或其他 Windows 无法直接打开的路径。

## 强制进度报告

任务开始、阶段切换、每个 20 至 30 条检查点、完整查重结束、填充里程碑、审核包生成、上传分片完成和异常暂停时，主动向用户报告进度。每次必须包含：

```text
【题录进度】
当前阶段：1/3 搜索与查重
当前环节：第2个检查点·线上批量预查重
已完成：已发现52条；本地排除4条；正式库重复3条；确认独立45/100条
正在处理：提交本检查点的45条候选进行正式库查重
下一步：保存逐项匹配依据并继续补搜
```

- 使用真实数量和文件状态，不用模糊百分比代替。
- “已完成”只写已经落盘或已由 Ability 返回的结果。
- 阻断时增加“阻断原因”和“需要什么”，不得静默等待。
- 生成审核包后明确报告“已暂停，尚未上传”。
- 用户询问进度时，先报告当前状态，再继续执行未完成工作。

## 阶段一：搜索与查重

1. 先建立本地任务和稳定 UUID，不在 WordPress 创建批次。
2. 搜索官网、专门介绍页、机构目录、论文、新闻或其他实际介绍资源的来源；把发现条目的实际页面记录为“索引来源”，不要把搜索引擎记为索引来源。
3. 进入候选官网后，继续寻找 `About`、`Presentation`、`Project`、`关于`、`项目介绍`、`数据库说明`等专门页面。存在专门介绍页时，`introduction_url`不得停留在首页或检索入口。
4. 每发现 20 至 30 条执行一次本地候选互重和线上批量预查重。该数字只是内部检查点，不是用户指定批次。
5. 达到目标数量并多搜 10% 至 20% 后，执行一次全任务完整查重；独立候选不足时继续补搜。
6. 使用 `scripts/local-dedup.py` 做同任务候选互重；使用 `scripts/wordpress-dedup.py` 调用 `mulu1012-catalog/deduplicate-candidates` 与正式库查重。
7. 重复和待人工判重项不计入目标数量；独立但资料不足的阻断项计入目标数量。

## 阶段二：填充与审核文件

1. 只完整填充明确独立候选。重复和待人工判重项只保留发现来源与查重材料。
2. `官网介绍`必须保存专门介绍页的完整主要正文，不是摘要、搜索片段或一段短摘录；保留原文语言、段落、标题、强调、链接、列表等 Markdown 结构。详细规则见 [references/field-rules.md](references/field-rules.md)。
3. `正文介绍`必须基于完整官网介绍生成：中文官网保留完整中文正文，外文官网作完整忠实中文翻译；不得把短摘要扩写成正文。
4. 固定按`站点类型 → 主分类 → 特色史料 → 数据库类型`顺序判断四项分类。分别判断产品结构、主要内容/功能、独特史料亮点和建库范围；禁止按站点类型机械映射主分类，也禁止把`专题库`作为默认值。
5. 为每个非空字段记录填充值、原文依据、来源、直接提取或规则推断、判断和 `xx/100` 置信度。
6. 为每个空字段记录未填原因。不得为提高填充率而编造。
7. 让另一个独立 AI 审核字段结果，生成总评分、阻断项和建议状态；字段生成者不得给自己评分。审核必须检查介绍页是否找对、官网全文是否完整、Markdown 是否保真，并逐项复核四个分类字段没有混用。
8. 运行 `scripts/validate-batch.py`。调用 `load_workspace_dependencies`取得 Node packages 路径，把它作为`--node-modules`传给`scripts/build-review-package.mjs`和`scripts/verify-review-package.mjs`。
9. 生成 `任务总结.md`、`题录审核.xlsx`、`题录总表.csv`、`wordpress-upload.jsonl`、`upload-manifest.json`。
10. **生成审核文件后立即暂停。只有用户明确确认上传，才进入阶段三。**

## 阶段三：上传

1. 只有用户明确确认后，运行 `scripts/wordpress-upload.py --confirm-upload`调用`mulu1012-catalog/import-local-batch`，把一个本地任务作为一个 WordPress 工作台批次整批提交。
2. 可为传输稳定性分片，但所有片段必须复用同一个批次 UUID 和幂等键；不得创建额外工作台批次。
3. 上传全部候选。独立候选包含完整字段；重复、待判重和阻断项包含查重审计信息。
4. Ability 只创建批次、候选、不可变版本、证据、AI 审核和拟新增词，不得批准或发布。
5. 上传完成后调用 `mulu1012-catalog/get-batch` 和 `mulu1012-catalog/get-candidate` 回读，核对总数、状态、字段、证据、查重和审核。
6. 把 WordPress 批次 UUID、成功/失败数量和回读结果追加到 `任务总结.md`，保留上传前统计。

## 硬性边界

- 不直接写线上 MySQL；只通过 WordPress Ability 调用共享领域服务。
- 不在上传前创建 WordPress 批次或分类词项。
- 不批准、不发布、不修改现有 WordPress 分类词项。
- 不用重复或偏题条目凑足用户要求的独立候选数量。
- 不读取或沿用已废弃题录 Skill 的规则。
