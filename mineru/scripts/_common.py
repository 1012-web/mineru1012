"""MinerU 产物读取的共用工具。

Windows 说明：这些脚本一律把详细报告写成 UTF-8 文件，stdout 只打印 ASCII 状态行。
原因是 Windows 控制台默认 GBK，中文报告直接 print 会变成乱码，反而看不了。
详细内容请用 Read 工具读生成的文件。
"""
import json
import os
import re
import sys
import glob

CIRC = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
CIRC_RE = re.compile('[' + CIRC + ']')

# 实义字符：比对时只看这些，标点/空格/Markdown 语法一律忽略
KEEP_RE = re.compile(r'[㐀-䶿一-鿿A-Za-z0-9' + CIRC + r'□〈〉〔〕]')

TEXTLIKE = ('text', 'ref_text', 'title', 'list', 'index')


def die(msg):
    print('ERROR: ' + msg)
    sys.exit(1)


def find_files(d):
    """定位 MinerU 输出目录里的各个产物。"""

    def one(pattern):
        hits = sorted(glob.glob(os.path.join(d, pattern)))
        return hits[0] if hits else None

    return {
        'layout': one('layout.json'),
        'block_list': one('block_list.json'),
        'full_md': one('full.md') or one('MinerU_markdown_*.md'),
        'content_list': one('*_content_list.json'),
        'content_list_v2': one('*_content_list_v2.json'),
        'model': one('*_model.json'),
        'origin_pdf': one('*_origin.pdf'),
        'images': os.path.join(d, 'images') if os.path.isdir(os.path.join(d, 'images')) else None,
    }


def load_layout(d):
    f = find_files(d)['layout']
    if not f:
        die('找不到 layout.json——它是正文的唯一可靠来源，没有它无法继续。')
    return json.load(open(f, encoding='utf-8'))['pdf_info']


def load_blocklist(d):
    """返回 (pages, merge_map)。merge_map: (page_idx, index) -> (page_idx, index)"""
    f = find_files(d)['block_list']
    if not f:
        return None, {}
    raw = json.load(open(f, encoding='utf-8'))
    pages = raw.get('pdfData', [])
    pos2key = {}
    for pi, pg in enumerate(pages):
        for b in pg:
            if 'block_position' in b:
                pos2key[b['block_position']] = (pi, b.get('index'))
    merge = {}
    for c in raw.get('mergeConnections', []):
        blocks = c.get('blocks', [])
        for a, b in zip(blocks, blocks[1:]):
            if a in pos2key and b in pos2key:
                merge[pos2key[a]] = pos2key[b]
    return pages, merge


def strip_tags(t):
    """MinerU 用 <sub>/<sup> 包裹部分标点和上下标，还原为普通字符。"""
    t = re.sub(r'<sub>(.*?)</sub>', r'\1', t)
    t = re.sub(r'<sup>(.*?)</sup>', r'\1', t)
    return t


def join_lines(lines):
    """按行拼接。只有两侧都是 ASCII 字母数字时才补空格。

    中文换行处不能加空格（会多出空格），但英文/数字换行处必须加，
    否则 '2020' + '2022' 会粘成 '20202022'——这正是 block_list.json 的通病。
    """
    out = ''
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if out and re.search(r'[0-9A-Za-z]$', out) and re.match(r'[0-9A-Za-z]', ln):
            out += ' '
        out += ln
    return out


def line_texts(block):
    """取出一个 layout 块的每行文字。

    注意 image/table 类块是嵌套的：它们有 `blocks`（image_body / image_caption）
    而不是直接的 `lines`。套用正文逻辑会静默漏掉图注。
    """
    out = []
    for ln in block.get('lines', []) or []:
        out.append(''.join(s.get('content') or '' for s in ln.get('spans', [])))
    for sub in block.get('blocks', []) or []:
        out.extend(line_texts(sub))
    return out


def block_text(block):
    return join_lines(line_texts(block))


def image_paths(block):
    """递归取出块里的图片路径。"""
    out = []
    for ln in block.get('lines', []) or []:
        for s in ln.get('spans', []):
            p = s.get('image_path') or s.get('img_path')
            if p:
                out.append(p.lstrip('/'))
    for sub in block.get('blocks', []) or []:
        out.extend(image_paths(sub))
    return out


def sub_blocks_by_type(block, wanted):
    out = []
    for sub in block.get('blocks', []) or []:
        if sub.get('type') in wanted:
            out.append(sub)
        out.extend(sub_blocks_by_type(sub, wanted))
    return out


def body_blocks(page):
    """一页里参与正文的块，按阅读顺序。"""
    return sorted(page.get('preproc_blocks', []), key=lambda b: b.get('index', 0))


def footnote_blocks(page):
    return [b for b in page.get('discarded_blocks', []) if b.get('type') == 'page_footnote']


def discarded_of_type(page, t):
    return [b for b in page.get('discarded_blocks', []) if b.get('type') == t]


def page_number_map(layout):
    """从 page_number 块推断每页的印刷页码。

    很多首页没有页码块，所以用「印刷页码 - page_idx」的众数当偏移量补齐。
    """
    seen = {}
    for pi, pg in enumerate(layout):
        for b in discarded_of_type(pg, 'page_number'):
            # layout.json 的 discarded 块没有 text 字段，只有 lines——必须回退到行级拼接
            m = re.search(r'\d+', strip_tags(b.get('text') or block_text(b)))
            if m:
                seen[pi] = int(m.group(0))
                break
    if not seen:
        return {pi: pi + 1 for pi in range(len(layout))}
    offsets = {}
    for pi, n in seen.items():
        offsets[n - pi] = offsets.get(n - pi, 0) + 1
    off = max(offsets, key=offsets.get)
    return {pi: seen.get(pi, pi + off) for pi in range(len(layout))}


def significant(s):
    """只保留实义字符，用于比对。"""
    return ''.join(KEEP_RE.findall(s))


def write_report(path, text):
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    print('report -> ' + os.path.basename(path))
