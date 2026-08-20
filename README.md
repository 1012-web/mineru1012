# mineru

把 [MinerU](https://github.com/opendatalab/MinerU) 的 PDF 解析输出整理成高质量 Markdown（Obsidian 格式，也适合喂给 AI）的 Claude Code skill。

## 解决什么问题

MinerU 会输出一份 `full.md`，但它把页眉、页码、**脚注**标记为 `is_discarded` 后直接丢弃。实测：

| 文献 | full.md 丢失的脚注 |
|---|---|
| 一篇 12 页期刊论文 | 60 条 |
| 一本 200 页专著 | 305 条 |

对学术文献来说，脚注往往是最有价值的部分。这些内容完整保存在 `layout.json` 的 `discarded_blocks` 里，只是没被渲染出来。

除此之外还处理：跨页段落合并、页码锚点、圈码脚注的全局重编号、图表与图注、标题层级重建，以及某些 PDF 文字层编码损坏导致的**中文标点大面积丢失**。

## 用法

在 Claude Code 中，指向一个 MinerU 输出目录并说明需求即可，skill 会自动触发。手动跑脚本：

```bash
python scripts/probe.py <mineru输出目录>              # 探查 + 六项自动检查
python scripts/build.py <目录> -o _skeleton.md        # 重建骨架
python scripts/verify.py <成品.md>                    # 结构自检
python scripts/diff_report.py <目录> <成品.md>        # 交付前逐字符改动比对
python scripts/crop_pdf.py <origin.pdf> --page 2 --rect 65,545,300,559 -o crop.png
```

脚本把报告写成 UTF-8 文件、stdout 只打一行 ASCII——Windows 控制台是 GBK，中文直接 print 会乱码。

## 三条红线

1. **不给正文补字。** 学术文献以原刊为准；疑似脱字只标注，经用户许可才改。
2. **改动原文前先核 PDF 原图。** 分不清是原刊印错还是 OCR 认错，就没资格决定改不改。
3. **改动报告必须由 `diff_report.py` 生成，不能靠回忆。** 曾有一次标点规范化脚本的正则 bug 静默删掉约 85 处括号内容，全靠它揪出来。

## 结构

```
SKILL.md                      主流程
scripts/
  probe.py                    探查、圈码↔脚注对齐审计、六项自动检查
  build.py                    从 layout.json 重建骨架
  verify.py                   结构自检
  diff_report.py              交付前改动比对
  crop_pdf.py                 裁图核对 / 剥离文字层供重跑 OCR
references/
  mineru-outputs.md           六个产物文件各自有什么、缺什么
  templates.md                frontmatter / AI 校注 / 改动报告 模板
```

## 状态

在一篇期刊论文和一本 200 页专著上做过端到端验证（含无 skill 对照组）。**尚未覆盖**：表格密集型 PDF、双栏排版、竖排古籍、扫描件 OCR、英文文献、含公式的理工科文献。
