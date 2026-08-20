"""从 layout.json 重建正文骨架。

用法:
    python build.py <mineru输出目录> -o _skeleton.md [--skip-marker 12,30] [--quote-indent 10]

骨架里的正文仍是 MinerU 的原始用字（标点可能缺失），需要人工还原标点。
但脚注、页码锚点、跨页合并、图片、编号这些机械活已经做完了。

产出:
    -o 指定的 markdown 骨架
    <目录>/_build_report.txt  疑似引文块清单等需要复核的项
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (ANCHOR_RE, CIRC, CIRC_RE, FLOAT_PREFIX, block_text, body_blocks, die, find_files,
                     footnote_blocks, image_paths, is_break, join_lines, line_texts,
                     load_blocklist, load_layout, page_number_map, strip_tags,
                     sub_blocks_by_type, write_report)

META_PREFIX = ('收稿日期', '作者简介', '基金项目', '通信作者', '引用格式', '作者单位', 'DOI')
CJK = r'㐀-䶿一-鿿'


def btext(b):
    return strip_tags(b.get('text') or block_text(b)).strip()


def tidy_footnote(t):
    """脚注只做「清理」不做「补标点」：还原标签、修断行、规范半全角。

    保留原有空格——它们是被吞掉的标点的位置标记，删掉反而更难读。
    """
    t = strip_tags(t).replace('－', '-')
    t = re.sub(r'\s*:\s*(?=[' + CJK + r'《（])', '：', t)
    t = re.sub(r'(?<=[' + CJK + r'》）])\s*:\s*', '：', t)
    t = re.sub(r'(?<=[' + CJK + r'》）])\s*;\s*', '；', t)
    t = re.sub(r'\s*·\s*', '·', t)
    # 括号只在内容含中日韩文字时转全角，避免动到英文文献
    t = re.sub(r'\(\s*([^()]*?)\s*\)',
               lambda m: ('（%s）' % m.group(1)) if re.search('[' + CJK + ']', m.group(1)) else m.group(0), t)
    t = re.sub(r'\s+([，。、；：》】」』？！])', r'\1', t)
    t = re.sub(r'([《【「『])\s+', r'\1', t)
    return re.sub(r'\s{2,}', ' ', t).strip()


LEAD_CIRC_RE = re.compile('^[' + CIRC + ']+')


def parse_add_footnote(vals):
    """--add-footnote 页序号:圈码序号:正文 -> {页序号: [(圈码序号-1, 正文), ...]}"""
    add = {}
    for v in vals:
        parts = v.split(':', 2)
        if len(parts) != 3 or not parts[0].strip().isdigit() or not parts[1].strip().isdigit():
            die('--add-footnote 格式应为 页序号:圈码序号:正文，收到 %r' % v)
        add.setdefault(int(parts[0]), []).append((int(parts[1]) - 1, parts[2].strip()))
    return add


def collect_footnotes(layout, pnum, add=None):
    """按页序取出脚注。开头没有圈码的条目照位置保留——那多半是圈码识别丢了。

    两个坑，中文书里都很常见：

    一、**一条脚注可能领起多个圈码**——「②③⑥⑦⑧《史记·商君列传》。」是一条块，
    却对应正文里五个引用。只当成一条，其后所有脚注编号会整体错位。要拆成五条。

    二、拆完还得**按圈码值在页内重排**。上例那页的块序是 ①／②③⑥⑦⑧／④／⑤，
    照块序输出就成了 1,2,3,6,7,8,4,5，与正文的 ①②③④⑤⑥⑦⑧ 对不上。
    只在该页每条都认出了圈码时才排序；有认不出的就保持块序，宁可不动也别乱排。

    add 是 --add-footnote 回补的条目：MinerU 偶尔会整条漏掉一个脚注块，
    或把它误判成正文。漏一条，其后所有编号就错一位，必须在这里补回来。
    """
    notes, meta = [], []
    split, added = [], []
    add = add or {}
    for pi, pg in enumerate(layout):
        page_notes = []
        for b in footnote_blocks(pg):
            raw = b.get('text') or join_lines(line_texts(b))
            t = strip_tags(raw).strip()
            if not t:
                continue
            if t.startswith(META_PREFIX):
                meta.append((pnum[pi], tidy_footnote(t)))
                continue
            m = LEAD_CIRC_RE.match(t)
            if not m:
                page_notes.append((None, tidy_footnote(t)))
                continue
            body = tidy_footnote(t[m.end():])
            if len(m.group(0)) > 1:
                split.append((pnum[pi], m.group(0), body))
            for c in m.group(0):
                page_notes.append((CIRC.index(c), body))
        for k, t in add.get(pi, []):
            page_notes.append((k, t))
            added.append((pnum[pi], CIRC[k] if k < len(CIRC) else '?', t))
        if page_notes and all(k is not None for k, _ in page_notes):
            page_notes.sort(key=lambda kv: kv[0])
        notes += [(pnum[pi], t) for _, t in page_notes]
    return notes, meta, split, added


# 标题编号形态由粗到细。MinerU 常把所有标题都标成 level=2（实测 120–154 个全一样），
# 那种情况下源里没有任何层级信息，只能从编号形态重建。
HEAD_PATTERNS = [
    ('部/编/篇/卷', re.compile(r'^第[一二三四五六七八九十百零〇\d]+\s*[部编篇卷]')),
    ('章', re.compile(r'^第[一二三四五六七八九十百零〇\d]+\s*章')),
    ('节', re.compile(r'^第[一二三四五六七八九十百零〇\d]+\s*节')),
    ('一、', re.compile(r'^[一二三四五六七八九十]+\s*[、．.]')),
    ('（一）', re.compile(r'^[（(]\s*[一二三四五六七八九十]+\s*[)）]')),
    ('1.', re.compile(r'^\d+\s*[、．.]')),
    ('（1）', re.compile(r'^[（(]\s*\d+\s*[)）]')),
]


def make_heading_level(layout):
    """按文档里实际出现的编号形态，从 H2 起依次分级（H1 留给文档标题）。

    只在源里的 level 完全扁平时启用；源有真层级就别乱动它。
    """
    titles = [strip_tags(block_text(b)).strip()
              for pg in layout for b in body_blocks(pg) if b.get('type') == 'title']
    lv = {b.get('level') for pg in layout for b in body_blocks(pg) if b.get('type') == 'title'}
    if len(lv) > 1 or len(titles) <= 5:
        return (lambda _t: None), []
    present = [(name, rx) for name, rx in HEAD_PATTERNS if any(rx.match(t) for t in titles)]
    if not present:
        return (lambda _t: None), []
    mapping = {name: min(6, i + 2) for i, (name, _) in enumerate(present)}

    def level_of(t):
        for name, rx in present:
            if rx.match(t):
                return mapping[name]
        return None
    return level_of, [(n, mapping[n]) for n, _ in present]


def join_page_breaks(blocks):
    """接回 mergeConnections 漏掉的跨页断段。

    MinerU 只在自己记录了合并关系时才接段；实测一本 200 页的书还会剩 15—35 处
    没接上的。这些地方极易被误当成「缺标点」而补一个句号——那是用标点掩盖数据丢失。

    两个必须处理的细节（都是踩出来的）：
      1. 判断句末前要先剥掉尾部的 [^n]，否则「…近代化。[^11]」会被当成断句；
      2. 图和图注可能正夹在断点中间，要跨过去，接好后把图放到该段之后。
    """
    out, i, joined = [], 0, []
    while i < len(blocks):
        m = ANCHOR_RE.match(blocks[i].strip())
        if m and out and int(m.group(1)) > 0:
            j = i + 1
            floats = []
            while j < len(blocks) and blocks[j].strip().startswith(FLOAT_PREFIX):
                floats.append(blocks[j])
                j += 1
            if j < len(blocks):
                prev, nxt = out[-1].strip(), blocks[j].strip()
                if is_break(prev, nxt):
                    joined.append((int(m.group(1)), prev[-26:], nxt[:26], len(floats)))
                    out[-1] = prev + blocks[i].strip() + nxt
                    out += floats
                    i = j + 1
                    continue
        out.append(blocks[i])
        i += 1
    return out, joined


def body_margins(page):
    """一页正文的左右边界：取多行文本块 x0 / x1 的众数。

    只看多行块，因为单行段落带首行缩进，x0 会假性偏大。
    """
    bs = [b for b in body_blocks(page)
          if b.get('type') in ('text', 'ref_text') and len(b.get('lines') or []) >= 2]
    if not bs:
        bs = [b for b in body_blocks(page) if b.get('type') in ('text', 'ref_text')]
    if not bs:
        return (None, None)
    xs = [b['bbox'][0] for b in bs]
    x1s = [b['bbox'][2] for b in bs]
    return (min(xs, key=lambda v: (-xs.count(v), v)),
            max(x1s, key=lambda v: (x1s.count(v), v)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dir')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--skip-marker', default='',
                    help='非脚注的圈码全局序号，逗号分隔（从 _markers.tsv 认定）')
    ap.add_argument('--add-footnote', action='append', default=[], metavar='页序号:圈码序号:正文',
                    help='回补 MinerU 漏掉的脚注，可重复。页序号是 layout.json 的 page_idx')
    ap.add_argument('--quote-indent', type=float, default=10.0,
                    help='相对左边界缩进超过此值（pt）视为疑似引文块')
    ap.add_argument('--no-join', action='store_true',
                    help='不接合 mergeConnections 漏掉的跨页断段（默认接合，清单见构建报告）')
    a = ap.parse_args()

    if not os.path.isdir(a.dir):
        die('目录不存在: ' + a.dir)
    skip = {int(x) for x in a.skip_marker.replace('，', ',').split(',') if x.strip()}

    layout = load_layout(a.dir)
    _, merge = load_blocklist(a.dir)
    pnum = page_number_map(layout)
    has_images = bool(find_files(a.dir)['images'])

    merged_into = set(merge.values())
    margins = {pi: body_margins(pg) for pi, pg in enumerate(layout)}
    heading_level, head_map = make_heading_level(layout)

    quote_candidates = []
    state = {'seq': 0, 'used': 0}

    def sub_markers(text, pi):
        """圈码 -> [^n]。跳过被认定为非脚注的那些。"""
        def rep(m):
            state['seq'] += 1
            if state['seq'] in skip:
                return m.group(0)
            state['used'] += 1
            return '[^%d]' % state['used']
        return CIRC_RE.sub(rep, text)

    def render(pi, b, seam=None):
        """把一个块渲染成 markdown。seam 用于跨页合并时把页码锚点插在断页处。"""
        typ = b.get('type')
        if typ in ('image', 'table'):
            # 图和表统一处理。要点：图注只能出一次——block_text(b) 是递归的，
            # 会把子块里的图注一起取出来，再单独输出一遍就重复了。
            caps = [strip_tags(block_text(c)).strip()
                    for c in sub_blocks_by_type(b, ('image_caption', 'table_caption'))]
            foots = [strip_tags(block_text(c)).strip()
                     for c in sub_blocks_by_type(b, ('image_footnote', 'table_footnote'))]
            html = (b.get('html') or b.get('table_body') or '').strip()
            for sub in sub_blocks_by_type(b, ('table_body', 'image_body')):
                html = html or (sub.get('html') or sub.get('table_body') or '').strip()
            out = ['*%s*' % sub_markers(c, pi) for c in caps if c]
            if html:
                out.append(html)
            # 以图片承载的表格，html 是空的，图必须照样输出，否则内容直接丢失
            for p in image_paths(b):
                out.append('![](images/%s)' % os.path.basename(p) if has_images else '![](%s)' % p)
            if not html and not image_paths(b):
                inner = ' '.join(strip_tags(block_text(s)).strip()
                                 for s in sub_blocks_by_type(b, ('table_body', 'image_body')))
                if inner.strip():
                    out.append(sub_markers(inner.strip(), pi))
            out += ['<small>%s</small>' % sub_markers(c, pi) for c in foots if c]
            return '\n\n'.join(x for x in out if x)

        txt = strip_tags(block_text(b)).strip()
        if seam:
            txt = seam(txt)
        if not txt:
            return ''
        txt = sub_markers(txt, pi)

        if typ == 'title':
            plain = re.sub(r'^#+\s*', '', txt)
            lvl = heading_level(plain) or max(1, min(6, int(b.get('level') or 2)))
            return '#' * lvl + ' ' + plain

        # 单行块的 x0 等于首行缩进后的位置，跟缩排引文无法区分——只判定多行块。
        # 实测在一本 200 页的书上，不加这个限制会产生 53 处误报、0 处真引文。
        x0, x1 = margins.get(pi, (None, None))
        if x0 is not None and len(b.get('lines') or []) >= 2 and b['bbox'][0] - x0 > a.quote_indent:
            li = b['bbox'][0] - x0
            ri = x1 - b['bbox'][2]
            # 居中块（题名、作者、机构、居中小标题）两侧留白接近，跟缩排引文不是一回事
            centered = abs(li - ri) < 12 and len(b.get('lines') or []) <= 2
            kind = 'center' if centered else 'quote'
            quote_candidates.append((pnum[pi], kind, round(li), round(ri), txt[:46]))
            return '<!--?%s--> %s' % (kind, txt)
        return txt

    lines = []
    seen_pages = set()
    for pi, pg in enumerate(layout):
        for b in body_blocks(pg):
            key = (pi, b.get('index'))
            if key in merged_into:
                continue
            # 跨页合并：沿着链条把后续页的文字接上，锚点插在接缝处
            pieces = [render(pi, b)]
            k, cur_pi = key, pi
            while k in merge:
                nk = merge[k]
                npi = nk[0]
                nb = next((x for x in body_blocks(layout[npi]) if x.get('index') == nk[1]), None)
                if nb is None:
                    break
                seg = render(npi, nb)
                if seg:
                    pieces.append('<!--p.%s-->%s' % (pnum[npi], seg))
                    seen_pages.add(npi)
                k, cur_pi = nk, npi
            body = ''.join(pieces).strip()
            if not body:
                continue
            if pi not in seen_pages:
                seen_pages.add(pi)
                if pi > 0:
                    lines.append('<!--p.%s-->' % pnum[pi])
            lines.append(body)

    joined = []
    if not a.no_join:
        lines, joined = join_page_breaks(lines)

    notes, meta, split, added = collect_footnotes(layout, pnum, parse_add_footnote(a.add_footnote))

    head = ['---',
            '# 期刊/图书元数据——从 _probe.txt 的页眉与期刊元信息补全后删掉本行注释',
            'title: ',
            'author: ',
            'tags: []   # tags/aliases/cssclasses 是 Obsidian 内置属性，必须保持英文键名',
            '---',
            '',
            '<!--p.%s-->' % pnum[0]]

    out = '\n'.join(head) + '\n\n' + '\n\n'.join(lines) + '\n\n---\n\n## 注释\n\n'
    for i, (pn, t) in enumerate(notes, 1):
        out += '[^%d]: <!--p.%s--> %s\n\n' % (i, pn, t)

    with open(a.out, 'w', encoding='utf-8') as fh:
        fh.write(out)

    R = ['# 骨架构建报告\n\n']
    R.append('输出: %s\n' % os.path.abspath(a.out))
    R.append('页数 %d / 正文块 %d / 脚注 %d\n' % (len(layout), len(lines), len(notes)))
    R.append('圈码总数 %d，其中按 --skip-marker 跳过 %d，转为脚注引用 %d\n'
             % (state['seq'], len(skip), state['used']))
    if state['used'] != len(notes):
        R.append('\n** 引用数 %d != 脚注定义数 %d。先回到 probe.py 的审计结果查清原因，不要直接交付。 **\n'
                 % (state['used'], len(notes)))
    else:
        R.append('-> 引用与定义数量一致。\n')

    if added:
        R.append('\n## 按 --add-footnote 回补的脚注，共 %d 条\n' % len(added))
        R.append('  这些是 MinerU 整条漏掉或误判成正文的注，已核对原刊后补回。**必须写进交付报告。**\n')
        for pn, c, t in added:
            R.append('  p.%-5s %s %s\n' % (pn, c, t[:70]))

    if split:
        R.append('\n## 领起多个圈码、已拆成多条的脚注，共 %d 处\n' % len(split))
        R.append('  原刊一条注管几个引用（「②③④《史记·商君列传》。」），拆开后每个圈码各得一条同文的注。\n')
        R.append('  拆出来的条目按圈码值在页内重排过，因为原书这类块的排列并不是 ①②③④。\n')
        for pn, marks, t in split:
            R.append('  p.%-5s %-8s %s\n' % (pn, marks, t[:60]))

    if joined:
        R.append('\n## 已接回的跨页断段，共 %d 处\n' % len(joined))
        R.append('  这些是 mergeConnections 没记录、但上页结尾无句末标点、下页开头不像新段的地方。\n')
        R.append('  已按判据接合并在接缝处保留页码锚点。抽查几条确认没有误接。\n')
        R.append('  「跨图 N」表示断点中间夹着 N 个图/图注块，已跨过并把图放到该段之后。\n')
        for pn, l, r, nf in joined:
            R.append('  p.%-5s …%s  ▷接▷  %s…%s\n' % (pn, l, r, ('  跨图 %d' % nf) if nf else ''))

    if meta:
        R.append('\n## 应移入 frontmatter 的期刊元信息\n')
        for pn, t in meta:
            R.append('  p.%s  %s\n' % (pn, t))

    if quote_candidates:
        R.append('\n## 缩排/居中块（骨架已标 <!--?quote--> / <!--?center-->），共 %d 处\n'
                 % len(quote_candidates))
        R.append('  依据是 bbox 缩进量，只是线索不是结论。中文段落首行缩进 2 字，\n')
        R.append('  单行段落会被误判为缩排；居中的题名/作者两侧留白接近，标为 center。\n')
        R.append('  逐条对着文字确认：确是引文改成 > 引用块；题名/作者按其本来身份处理；都不是就删掉标记。\n')
        R.append('  印刷页  类型    左缩进  右缩进  文字\n')
        for pn, kind, li, ri, t in quote_candidates:
            R.append('  %-7s %-7s %-7s %-7s %s\n' % (pn, kind, li, ri, t))

    R.append('\n## 下一步\n')
    R.append('  1. 复核 <!--?quote--> / <!--?center--> 标记，删除或改为 >\n')
    R.append('  2. 补全 frontmatter\n')
    R.append('  3. 还原正文标点（脚注不补）\n')
    R.append('  4. verify.py 校验 -> diff_report.py 出改动报告\n')
    write_report(os.path.join(a.dir, '_build_report.txt'), ''.join(R))
    print('blocks=%d footnote_refs=%d footnote_defs=%d quote_candidates=%d joined=%d'
          % (len(lines), state['used'], len(notes), len(quote_candidates), len(joined)))


if __name__ == '__main__':
    main()
