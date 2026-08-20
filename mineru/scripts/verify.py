"""成品自检：脚注编号、标签残留、页码锚点、frontmatter、图片链接。

用法:
    python verify.py <成品.md> [--images-dir <目录>]

只报结构性问题。内容层面的改动要靠 diff_report.py。
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import write_report

BUILTIN = ('tags', 'aliases', 'cssclasses')
BUILTIN_CN = {'标签': 'tags', '别名': 'aliases', '样式': 'cssclasses'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('md')
    ap.add_argument('--images-dir', default=None)
    a = ap.parse_args()

    text = open(a.md, encoding='utf-8').read()
    R = ['# 成品自检: %s\n\n' % os.path.basename(a.md)]
    problems = []

    # ---- frontmatter
    fm = ''
    m = re.match(r'^---\n(.*?)\n---\n', text, re.S)
    if not m:
        problems.append('没有 YAML frontmatter（如果本来就不需要，忽略这条）')
    else:
        fm = m.group(1)
        try:
            import yaml
            data = yaml.safe_load(fm)
            R.append('frontmatter: %d 个字段，YAML 解析通过\n' % (len(data) if isinstance(data, dict) else 0))
        except ImportError:
            bad = [ln for ln in fm.split('\n')
                   if ln.strip() and not ln.startswith((' ', '-', '#')) and ':' not in ln]
            R.append('frontmatter: %d 行（未装 pyyaml，只做粗查）\n' % len(fm.split('\n')))
            for ln in bad:
                problems.append('frontmatter 这行可能不是合法 YAML: %s' % ln.strip())
        except Exception as e:
            problems.append('frontmatter YAML 解析失败: %s' % e)
        for cn, en in BUILTIN_CN.items():
            if re.search(r'^%s\s*:' % cn, fm, re.M):
                problems.append('frontmatter 用了中文键「%s」——它是 Obsidian 内置属性，'
                                '必须写成英文 %s，否则会退化成普通文本字段' % (cn, en))

    # ---- 正文 / 注释 分区
    parts = re.split(r'\n#+\s*注释\s*\n', text, maxsplit=1)
    body = parts[0]
    notes = parts[1] if len(parts) > 1 else ''
    body = re.sub(r'>\s*\[!\w+\][^\n]*\n(?:>[^\n]*\n?)*', '', body)  # 去掉 callout（含 AI 校注）

    # ---- 脚注
    refs = [int(x) for x in re.findall(r'\[\^(\d+)\](?!:)', text)]
    defs = [int(x) for x in re.findall(r'^\[\^(\d+)\]:', text, re.M)]
    R.append('脚注: 引用 %d 个（去重 %d），定义 %d 个\n' % (len(refs), len(set(refs)), len(defs)))
    if sorted(set(refs)) != sorted(defs):
        miss = sorted(set(defs) - set(refs))
        extra = sorted(set(refs) - set(defs))
        if miss:
            problems.append('有定义但正文没引用的脚注: %s' % miss)
        if extra:
            problems.append('引用了但没有定义的脚注: %s' % extra)
    if defs and sorted(defs) != list(range(1, len(defs) + 1)):
        problems.append('脚注编号不连续（应为 1..%d）' % len(defs))
    if len(refs) != len(set(refs)):
        dup = sorted({r for r in refs if refs.count(r) > 1})
        problems.append('脚注被重复引用: %s（Obsidian 对重复锚点的渲染不稳定）' % dup)

    # ---- 标签残留
    # 先剥掉行内代码/代码块：AI 校注里用反引号提到 `<sub>` 时不该被当成残留标签
    scan = re.sub(r'```.*?```', '', text, flags=re.S)
    scan = re.sub(r'`[^`\n]*`', '', scan)
    tags = re.findall(r'</?su[bp]>', scan)
    R.append('MinerU 标签残留: %d\n' % len(tags))
    if tags:
        problems.append('还有 %d 处 <sub>/<sup> 没清干净' % len(tags))
    for mark in ('<!--?quote-->', '<!--?center-->'):
        n = text.count(mark)
        if n:
            problems.append('还有 %d 处 %s 待复核标记没处理' % (n, mark))

    # ---- 页码锚点
    anchors = re.findall(r'<!--p\.([^>]+?)-->', body)
    R.append('正文页码锚点: %d 个\n' % len(anchors))
    seen = {}
    for x in anchors:
        seen[x] = seen.get(x, 0) + 1
    dupes = {k: v for k, v in seen.items() if v > 1}
    if dupes:
        problems.append('同一页码出现多个锚点: %s（跨页合并处已插锚点时，别再补一个独立的）'
                        % ', '.join('p.%s x%d' % (k, v) for k, v in dupes.items()))
    nums = [int(x) for x in anchors if x.isdigit()]
    if nums and nums != sorted(nums):
        problems.append('页码锚点不是递增的，检查块顺序')

    # ---- 图片
    imgs = re.findall(r'!\[[^\]]*\]\(([^)]+)\)', text)
    if imgs:
        R.append('图片引用: %d 个\n' % len(imgs))
        base = a.images_dir or os.path.dirname(os.path.abspath(a.md))
        missing = [p for p in imgs if not p.startswith(('http:', 'https:'))
                   and not os.path.exists(os.path.join(base, p))]
        if missing:
            problems.append('这些图片文件找不到（记得把 images/ 目录一起拷到 md 旁边）: %s'
                            % ', '.join(missing[:5]))

    R.append('\n')
    if problems:
        R.append('## 待处理 %d 项\n' % len(problems))
        for p in problems:
            R.append('  - %s\n' % p)
    else:
        R.append('## 全部通过\n')
    R.append('\n结构自检只管形式。内容有没有被改动，跑 diff_report.py。\n')

    out = os.path.join(os.path.dirname(os.path.abspath(a.md)), '_verify.txt')
    write_report(out, ''.join(R))
    print('refs=%d defs=%d tags=%d anchors=%d problems=%d'
          % (len(refs), len(defs), len(tags), len(anchors), len(problems)))
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
