"""探查 MinerU 输出目录，并做圈码↔脚注的对齐审计。

用法:
    python probe.py <mineru输出目录>

产出:
    <目录>/_probe.txt    结构报告（用 Read 工具看）
    <目录>/_markers.tsv  正文里每个圈码的全局序号与上下文——必须逐行扫一遍
"""
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (CIRC, CIRC_RE, OPENER, TERMINAL, block_text, body_blocks, die,
                     discarded_of_type, find_files, footnote_blocks, load_blocklist,
                     load_layout, page_number_map, strip_tags, write_report)

META_PREFIX = ('收稿日期', '作者简介', '基金项目', '通信作者', '引用格式', '作者单位', 'DOI')


def btext(b):
    """block_list.json 的块有 text 字段，layout.json 的只有 lines——两边都要能取到。"""
    return strip_tags(b.get('text') or block_text(b)).strip()


def extra_checks(layout, merge, pnum, R, outdir):
    """把踩过的坑做成自动检查，而不是写成让模型每次自己想起来的说明文字。"""
    hits = 0

    # 1) 句内枚举圈码：「条件：①…；②…」这类不是脚注号，盲转会让后面整体错位
    enum = []
    for pi, pg in enumerate(layout):
        for b in body_blocks(pg):
            t = strip_tags(block_text(b))
            ms = list(CIRC_RE.finditer(t))
            runs, cur = [], []
            for a, c in zip(ms, ms[1:]):
                if CIRC.index(c.group(0)) == CIRC.index(a.group(0)) + 1 and c.start() - a.start() < 40:
                    cur = (cur or [a]) + [c]
                else:
                    if cur:
                        runs.append(cur)
                    cur = []
            if cur:
                runs.append(cur)
            for r in runs:
                s = r[0].start()
                # 两个挨得近的脚注号也会连成 run，光看长度会大量误报。
                # 真正的列举几乎都由冒号引出（「条件：①…；②…」），或长到 3 项以上。
                if len(r) >= 3 or '：' in t[max(0, s - 12):s] or ':' in t[max(0, s - 12):s]:
                    enum.append((pnum[pi], t[max(0, s - 24):s + 60]))
    if enum:
        hits += 1
        R.append('\n## [检查] 疑似句内枚举圈码，%d 处\n' % len(enum))
        R.append('  同一块里出现连续递增且间距很近的圈码，多半是「①②③」列举而非脚注号。\n')
        R.append('  确认后用 build.py --skip-marker 排除，否则其后所有脚注会整体错位。\n')
        for pn, t in enum[:12]:
            R.append('  p.%s  %s\n' % (pn, t))

    # 2) 被误判成脚注的编号列表项（参考书目常见：以 "2." 开头而非 "②"）
    mis = []
    for pi, pg in enumerate(layout):
        for b in footnote_blocks(pg):
            t = strip_tags(b.get('text') or block_text(b)).strip()
            if t and not CIRC_RE.match(t) and re.match(r'^\d+\s*[.．、]', t):
                mis.append((pnum[pi], t[:60]))
    if mis:
        hits += 1
        R.append('\n## [检查] 疑似被误判为脚注的编号列表项，%d 条\n' % len(mis))
        R.append('  它们以「2.」而非「②」开头，很可能是参考书目/正文列表被划进了脚注区。\n')
        R.append('  照脚注处理会让这些内容从正文里消失。核对原图后放回正文原位。\n')
        for pn, t in mis[:12]:
            R.append('  p.%s  %s\n' % (pn, t))

    # 3) MinerU 漏掉的跨页段落合并：上页末尾没有句末标点，下页开头不像新段
    src = set(merge.keys())
    breaks = []
    toc = re.compile(r'[…·．.]{3,}\s*[(（]?\d+[)）]?\s*$|^\S{0,20}[…·．.]{3,}')
    for pi in range(len(layout) - 1):
        # 目录页、版权页、扉页天然是短行堆叠，不适用「续接」判断
        if pnum[pi] <= 0 or pnum[pi + 1] <= 0:
            continue
        cur = [b for b in body_blocks(layout[pi]) if b.get('type') in ('text', 'ref_text')]
        nxt = [b for b in body_blocks(layout[pi + 1]) if b.get('type') in ('text', 'ref_text')]
        if not cur or not nxt:
            continue
        last, first = cur[-1], nxt[0]
        if (pi, last.get('index')) in src:
            continue
        lt = strip_tags(block_text(last)).rstrip()
        ft = strip_tags(block_text(first)).lstrip()
        if not lt or not ft or toc.search(lt) or toc.search(ft):
            continue
        if lt[-1] in TERMINAL or OPENER.match(ft):
            continue
        breaks.append((pnum[pi], lt[-26:], ft[:26]))
    if breaks:
        hits += 1
        R.append('\n## [检查] 疑似漏合并的跨页段落，%d 处\n' % len(breaks))
        R.append('  mergeConnections 没记录，但上页结尾无句末标点、下页开头不像新段起头。\n')
        R.append('  这类地方极易被误当成「缺标点」而补一个句号——那是用标点掩盖数据丢失。\n')
        R.append('  build.py 默认会按同一套判据接回去（--no-join 可关），全量清单见 _breaks.tsv，\n')
        R.append('  接合结果见构建报告。下面只是前 12 条预览。\n')
        for pn, a, b_ in breaks[:12]:
            R.append('  p.%s…%s  ▷接▷  %s…\n' % (pn, a, b_))
        write_report(os.path.join(outdir, '_breaks.tsv'),
                     '印刷页\t上页结尾\t下页开头\n'
                     + ''.join('%s\t%s\t%s\n' % (pn, a.replace('\t', ' '), b_.replace('\t', ' '))
                               for pn, a, b_ in breaks))

    # 3b) 同页 mergeConnection：合并关系本该跨页，同页出现多半是图注被误接进正文
    same = [(k, v) for k, v in merge.items() if k[0] == v[0]]
    if same:
        hits += 1
        R.append('\n## [检查] 同页 mergeConnection，%d 条\n' % len(same))
        R.append('  合并关系本该发生在换页处。同页的两块被接在一起，多半是图注（MinerU 误判为\n')
        R.append('  正文）被接到了正文段尾——正文会因此多出一句图注，还会凭空多一个页码锚点。\n')
        R.append('  逐条核对 layout.json 里该页的块序，确认真正的下文是哪一块。\n')
        for k, v in same:
            bl = {b.get('index'): b for b in body_blocks(layout[k[0]])}
            a = strip_tags(block_text(bl[k[1]]))[-26:] if k[1] in bl else '?'
            b_ = strip_tags(block_text(bl[v[1]]))[:34] if v[1] in bl else '?'
            R.append('  p.%s  idx%s…%s  ->  idx%s %s\n' % (pnum[k[0]], k[1], a, v[1], b_))

    # 4) 标题层级扁平
    lv = collections.Counter(b.get('level') for pg in layout for b in body_blocks(pg)
                             if b.get('type') == 'title')
    if lv and len(lv) == 1 and sum(lv.values()) > 5:
        hits += 1
        R.append('\n## [检查] 全部 %d 个标题都是 level=%s\n' % (sum(lv.values()), list(lv)[0]))
        R.append('  源里没有可用的层级，需要按「第X章/第X节/一、/1./（1）」的编号形态重建。\n')
        R.append('  build.py 在层级完全扁平时会自动重建，结果仍要复核。\n')
        R.append('  注意：层级「不扁平但自相矛盾」（第一章 H2、第二章 H1）不会触发这条检查，\n')
        R.append('  build.py 也会照抄源层级——分章前务必自己扫一遍 grep "^#" 的结果。\n')

    # 5) 篇幅截断：PDF 未必是整本书
    first_pn, last_pn = pnum[0], pnum[len(layout) - 1]
    tail = ''
    for b in reversed(body_blocks(layout[-1])):
        tail = strip_tags(block_text(b)).rstrip()
        if tail:
            break
    cut = tail and tail[-1] not in TERMINAL
    if first_pn > 1 or cut:
        hits += 1
        R.append('\n## [检查] 这份 PDF 可能不是完整的一本\n')
        R.append('  印刷页码 %s — %s%s\n' % (first_pn, last_pn,
                                          '，且正文结尾没有句末标点（疑似截断于句中）' if cut else ''))
        R.append('  结尾: …%s\n' % tail[-40:])
        R.append('  务必在 frontmatter 与交付说明里写清实际覆盖范围，别让读者以为是全书。\n')

    # 6) OCR 把普通字符读成了公式。只查 \% 会漏掉 \sim——数值区间 1500~1800 全被它吃掉
    alltxt = ''.join(strip_tags(block_text(b)) for pg in layout for b in body_blocks(pg))
    kinds = [('$…$ 行内公式', re.findall(r'\$[^$]{1,40}\$', alltxt)),
             (r'\%  百分号', re.findall(r'\\%', alltxt)),
             (r'\sim  波浪号（数值区间）', re.findall(r'\\sim', alltxt)),
             (r'\times / \approx / \pm 等', re.findall(r'\\(?:times|approx|pm|circ|cdot)', alltxt)),
             ('^{…} / _{…} 上下标', re.findall(r'[\^_]\{', alltxt))]
    kinds = [(n, len(v)) for n, v in kinds if v]
    if kinds:
        hits += 1
        R.append('\n## [检查] 疑似 OCR 误判的公式记法，%d 处\n' % sum(n for _, n in kinds))
        R.append('  抽查后统一还原为普通文本（还原成什么要看全书通例，如 \\sim -> ～）。\n')
        for n, c in kinds:
            R.append('  %-28s %d\n' % (n, c))

    if not hits:
        R.append('\n## [检查] 6 项自动检查全部未发现异常\n')
    return hits


def main():
    if len(sys.argv) < 2:
        die('用法: python probe.py <mineru输出目录>')
    d = sys.argv[1]
    if not os.path.isdir(d):
        die('目录不存在: ' + d)

    files = find_files(d)
    layout = load_layout(d)
    pages, merge = load_blocklist(d)
    pnum = page_number_map(layout)

    R = []
    R.append('# MinerU 产物探查\n')
    R.append('目录: %s\n' % os.path.abspath(d))

    R.append('\n## 文件\n')
    for k, v in files.items():
        if v:
            if os.path.isdir(v):
                R.append('  %-16s %s/  (%d 个文件)\n' % (k, os.path.basename(v), len(os.listdir(v))))
            else:
                R.append('  %-16s %s  (%.1f MB)\n' % (k, os.path.basename(v), os.path.getsize(v) / 1048576))
        else:
            R.append('  %-16s --- 缺失\n' % k)
    if not files['block_list']:
        R.append('\n  ! 没有 block_list.json：跨页段落的合并信息不可得，段落可能在换页处断开。\n')

    R.append('\n## 规模\n')
    R.append('  页数: %d\n' % len(layout))
    R.append('  印刷页码: %s — %s\n' % (pnum.get(0), pnum.get(len(layout) - 1)))

    # ---- 块类型
    R.append('\n## 块类型（layout.json）\n')
    body_c = collections.Counter(b.get('type') for pg in layout for b in body_blocks(pg))
    for t, n in body_c.most_common():
        R.append('  正文 %-16s %d\n' % (t, n))
    disc_c = collections.Counter(b.get('type') for pg in layout for b in pg.get('discarded_blocks', []))
    for t, n in disc_c.most_common():
        R.append('  丢弃 %-16s %d\n' % (t, n))

    nfn = disc_c.get('page_footnote', 0)
    if nfn:
        R.append('\n  -> full.md 丢掉了这 %d 条脚注。补回它们是本次整理的主要价值。\n' % nfn)

    # ---- 图 / 表 / 公式
    media = {t: n for t, n in body_c.items() if t in ('image', 'table', 'equation', 'interline_equation')}
    if media:
        R.append('\n## 图表公式\n')
        for t, n in media.items():
            R.append('  %-20s %d\n' % (t, n))
        if files['images']:
            R.append('  images/ 目录: %d 个文件\n' % len(os.listdir(files['images'])))
        R.append('  注意: 这类块在 layout.json 里是嵌套结构（用 blocks 而非 lines），\n')
        R.append('        图注在子块 image_caption / table_caption 里，别漏掉。\n')

    # ---- OCR 置信度
    scores = [s.get('score') for pg in layout for b in body_blocks(pg)
              for ln in (b.get('lines') or []) for s in ln.get('spans', []) if s.get('score') is not None]
    if scores:
        low = [s for s in scores if s < 0.9]
        R.append('\n## OCR 置信度\n')
        R.append('  spans: %d, 最低 %.3f, 低于 0.9 的: %d\n' % (len(scores), min(scores), len(low)))
        if not low:
            R.append('  -> 全部满分，说明是原生文字层 PDF（非扫描件），文字本身可信。\n')
        else:
            R.append('  -> 有低置信区域，交付时应列出待人工复核清单。\n')

    # ---- 标点完整度：决定这活是几分钟还是几小时
    alltxt = ''.join(strip_tags(block_text(b)) for pg in layout for b in body_blocks(pg))
    cjk = len(re.findall(r'[一-鿿]', alltxt))
    puncts = sum(alltxt.count(c) for c in '。，、；：？！')
    ratio = puncts / cjk if cjk else 0
    R.append('\n## 标点完整度\n')
    R.append('  汉字 %d，句读标点 %d，比值 %.3f\n' % (cjk, puncts, ratio))
    if ratio < 0.02:
        R.append('  -> ** 标点大面积丢失。** 多半是 PDF 文字层里标点字形的 Unicode 映射坏了，\n')
        R.append('     不是排版问题。先裁一块图确认（crop_pdf.py --rect），如果图里标点清清楚楚，\n')
        R.append('     最好的办法是 crop_pdf.py --strip-text-layer 后重跑 MinerU 走 OCR——\n')
        R.append('     OCR 读出来的是原文标点，远胜模型推断。把这个选择告诉用户。\n')
        R.append('     若坚持手工还原：这是本次工作的瓶颈，耗时随篇幅线性增长，长文档要分段做。\n')
    elif ratio < 0.05:
        R.append('  -> 标点偏少，抽查几段确认是否有局部丢失。\n')
    else:
        R.append('  -> 标点完好。骨架基本就是成品，主要工作是结构判定与元数据，篇幅长也不慢。\n')

    # ---- 对齐审计
    R.append('\n## 圈码 ↔ 脚注 对齐审计\n')
    total_marks = 0
    total_fn = 0
    per_page = []
    for pi, pg in enumerate(layout):
        txt = ''.join(strip_tags(block_text(b)) for b in body_blocks(pg))
        marks = CIRC_RE.findall(txt)
        fns = footnote_blocks(pg)
        numbered = [b for b in fns if not btext(b).startswith(META_PREFIX)]
        per_page.append((pi, pnum[pi], len(marks), len(fns), len(numbered)))
        total_marks += len(marks)
        total_fn += len(numbered)

    terse = len(layout) > 24
    rows = [r for r in per_page if r[2] != r[4]] if terse else per_page
    if terse:
        R.append('  （%d 页，只列本页数量不等的 %d 页；逐页不等是跨页合并的正常现象，看全局合计）\n'
                 % (len(layout), len(rows)))
    R.append('  页  印刷页  正文圈码  脚注块  其中带号\n')
    for pi, pn, m, f, nb in rows:
        flag = '' if m == nb else '   <-'
        R.append('  %-3d %-7s %-9d %-7d %-9d%s\n' % (pi, pn, m, f, nb, flag))

    R.append('\n  全局合计: 正文圈码 %d, 带号脚注 %d\n' % (total_marks, total_fn))
    if total_marks == total_fn:
        R.append('  -> 数量吻合。仍需扫 _markers.tsv 确认每个圈码都真是脚注引用。\n')
    else:
        R.append('  -> ** 数量不符，差 %d。停下来查清楚再继续。 **\n' % (total_marks - total_fn))
        R.append('     常见原因:\n')
        R.append('     1) 正文里有非脚注的圈码（如用 ⑧ 指代前文编号 (8) 的条目）——在 _markers.tsv 里找\n')
        R.append('     2) 某条脚注开头的圈码识别丢失（脚注文字还在，只是没有 ① 开头）\n')
        R.append('     3) 该刊脚注不用圈码，而用 [1] 或上标数字——需要改用别的正则\n')

    # ---- markers.tsv
    T = ['序号\t页\t印刷页\t圈码\t上下文\n']
    seq = 0
    for pi, pg in enumerate(layout):
        txt = ''.join(strip_tags(block_text(b)) for b in body_blocks(pg))
        for m in CIRC_RE.finditer(txt):
            seq += 1
            ctx = txt[max(0, m.start() - 18):m.start()] + '【' + m.group(0) + '】' + txt[m.end():m.end() + 14]
            T.append('%d\t%d\t%s\t%s\t%s\n' % (seq, pi, pnum[pi], m.group(0), ctx.replace('\t', ' ')))

    # ---- 无号脚注
    orphan = []
    for pi, pg in enumerate(layout):
        for b in footnote_blocks(pg):
            t = btext(b)
            if t and not CIRC_RE.match(t) and not t.startswith(META_PREFIX):
                orphan.append((pnum[pi], t[:60]))
    if orphan:
        R.append('\n## 开头没有圈码的脚注（%d 条）\n' % len(orphan))
        R.append('  这些多半是圈码识别丢失，需按页内位置补回编号：\n')
        for pn, t in orphan:
            R.append('  p.%s  %s\n' % (pn, t))

    meta = []
    for pi, pg in enumerate(layout):
        for b in footnote_blocks(pg):
            t = btext(b)
            if t.startswith(META_PREFIX):
                meta.append(t)
    if meta:
        R.append('\n## 期刊元信息（在脚注区，应移入 frontmatter）\n')
        for t in meta:
            R.append('  %s\n' % t)

    hdr = collections.Counter(btext(b) for pg in layout for b in discarded_of_type(pg, 'header'))
    if hdr:
        R.append('\n## 页眉（刊名/卷期常在这里，可入 frontmatter）\n')
        for t, n in hdr.most_common(6):
            R.append('  x%-3d %s\n' % (n, t))

    extra_checks(layout, merge, pnum, R, d)

    R.append('\n## 下一步\n')
    R.append('  1. 读 _markers.tsv，标出非脚注的圈码序号\n')
    R.append('  2. 逐条核对上面 [检查] 段报出的疑点\n')
    R.append('  3. python build.py %s -o _skeleton.md [--skip-marker 序号,序号]\n' % d)

    write_report(os.path.join(d, '_probe.txt'), ''.join(R))
    write_report(os.path.join(d, '_markers.tsv'), ''.join(T))
    print('pages=%d footnotes=%d markers=%d aligned=%s'
          % (len(layout), total_fn, total_marks, total_marks == total_fn))


if __name__ == '__main__':
    main()
