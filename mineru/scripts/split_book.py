"""把整书骨架切成分章文件：脚注按章重编号，每章自描述，另出一份索引。

用法:
    python split_book.py <整书.md> -o <输出目录> [--meta 书名=中国近现代史 --meta year=2009]
                         [--start-page 1] [--no-index] [--index-name 索引.md]

为什么要分章（而不是留一份全书）:
    整本几十万字喂给 AI 会撑爆检索窗口；再叠一份全书，同一段文字存两份，
    检索时同时命中、互相挤占名额，两份还会随编辑漂移。需要整本时 cat 一下即可。

为什么脚注要按章重编号:
    正文和它的注必须在同一个文件里——检索到某段时注能被一起召回。
    否则 [^47] 的定义在几万字之外，AI 看到的是空引用。

输入要求:
    - 可有可无的 YAML frontmatter（其中的字段会被每一章继承）
    - 正文用 `# ` 一级标题分章（build.py 重建层级后就是这个形态）
    - 文末的注释区形如 `\\n\\n---\\n\\n## 注释\\n\\n` 后跟 `[^n]: <!--p.N--> 正文`
"""
import argparse
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import die, write_report

SPLIT_RE = re.compile(r'\n+---\n+#+\s*注释\s*\n+')
NOTE_RE = re.compile(r'^\[\^(\d+)\]:\s*(?:<!--p\.(-?\d+)-->)?\s*(.*)$', re.S)
ANCHOR = re.compile(r'<!--p\.(-?\d+)-->')
FM_RE = re.compile(r'^---\n(.*?)\n---\n', re.S)
BAD = '\\/:*?"<>|'


def safe(name):
    for c in BAD:
        name = name.replace(c, '／' if c == '/' else '-')
    return name.strip()


def parse(path):
    text = io.open(path, encoding='utf-8').read()
    fm = ''
    m = FM_RE.match(text)
    if m:
        fm = m.group(1)
        text = text[m.end():]
    parts = SPLIT_RE.split(text, maxsplit=1)
    body = parts[0].strip()
    notes = {}
    if len(parts) > 1:
        for chunk in parts[1].strip().split('\n\n'):
            chunk = chunk.strip()
            if not chunk:
                continue
            mm = NOTE_RE.match(chunk)
            if not mm:
                die('脚注解析失败，这一条不是 `[^n]: 正文` 的形态:\n' + chunk[:80])
            notes[int(mm.group(1))] = (mm.group(2), mm.group(3).strip())
    return fm, body, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('md')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--meta', action='append', default=[],
                    help='附加到每章 frontmatter 的字段，形如 book=中国近现代史，可重复')
    ap.add_argument('--start-page', default=None,
                    help='第一章起始印刷页（首个页码锚点常在章标题之前，切分时已被留在卷首）')
    ap.add_argument('--index-name', default=None, help='索引文件名，默认取 --meta 里的 book')
    ap.add_argument('--no-index', action='store_true')
    a = ap.parse_args()

    if not os.path.isdir(a.out):
        os.makedirs(a.out)
    extra = []
    for kv in a.meta:
        if '=' not in kv:
            die('--meta 要写成 键=值: ' + kv)
        extra.append(tuple(kv.split('=', 1)))
    meta = dict(extra)

    fm, body, notes = parse(a.md)
    blocks = body.split('\n\n')
    if not blocks[0].startswith('# '):
        die('正文没有以一级标题开头。分章依据是 `# `，先确认 build.py 的层级重建结果。')

    chapters, cur = [], None
    page = a.start_page
    for b in blocks:
        if b.startswith('# '):
            cur = {'title': b[2:].strip(), 'blocks': [], 'start': page}
            chapters.append(cur)
        for m in ANCHOR.finditer(b):
            page = m.group(1)
        cur['blocks'].append(b)
        cur['end'] = page

    rows = []
    for i, c in enumerate(chapters, 1):
        used, remap = [], {}

        def sub(m):
            n = int(m.group(1))
            if n not in remap:
                used.append(n)
                remap[n] = len(used)
            return '[^%d]' % remap[n]

        text = re.sub(r'\[\^(\d+)\]', sub, '\n\n'.join(c['blocks']))
        missing = [n for n in used if n not in notes]
        if missing:
            die('第 %d 章引用了没有定义的脚注: %s' % (i, missing[:8]))

        pages = '-'.join(x for x in (c['start'], c['end']) if x) or ''
        head = ['---', 'title: %s' % c['title']]
        head += [ln for ln in fm.split('\n')
                 if ln.strip() and not re.match(r'^title\s*:', ln)]
        head += ['%s: %s' % kv for kv in extra]
        if pages:
            head.append('pages: %s' % pages)
        head += ['---', '']

        out = '\n'.join(head) + '\n' + text
        if used:
            out += '\n\n---\n\n## 注释\n\n' + '\n\n'.join(
                '[^%d]: %s%s' % (remap[n],
                                 ('<!--p.%s--> ' % notes[n][0]) if notes[n][0] else '',
                                 notes[n][1]) for n in used)
        fname = '%02d-%s.md' % (i, safe(c['title']))
        io.open(os.path.join(a.out, fname), 'w', encoding='utf-8').write(out.rstrip() + '\n')
        rows.append((i, c['title'], fname, pages, len(used)))

    used_all = sum(r[4] for r in rows)

    if not a.no_index:
        name = a.index_name or (safe(meta.get('book', '')) + '.md' if meta.get('book') else '索引.md')
        L = ['---']
        L += ['%s: %s' % kv for kv in extra]
        L += ['---', '', '# %s' % meta.get('book', os.path.basename(a.md)[:-3]), '',
              '| 篇章 | 文件 | 印刷页 | 脚注 |', '|---|---|---|---|']
        for i, title, fname, pages, n in rows:
            L.append('| %s | [[%s]] | %s | %d |' % (title, fname[:-3], pages or '—', n))
        L += ['', '共 %d 篇，%d 条脚注。' % (len(rows), used_all)]
        io.open(os.path.join(a.out, name), 'w', encoding='utf-8').write('\n'.join(L) + '\n')

    R = ['# 分章报告\n\n输出目录: %s\n\n' % os.path.abspath(a.out)]
    R.append('%-4s %-46s %-12s %s\n' % ('#', '标题', '印刷页', '脚注'))
    for i, title, fname, pages, n in rows:
        R.append('%-4d %-46s %-12s %d\n' % (i, title[:46], pages or '—', n))
    R.append('\n共 %d 篇；脚注 源 %d 条 -> 分配 %d 条\n' % (len(rows), len(notes), used_all))
    if used_all != len(notes):
        R.append('\n** 有 %d 条脚注没有被任何一章引用。回到 verify.py / diff_report.py 查清楚。 **\n'
                 % (len(notes) - used_all))
    R.append('\n下一步: 对每个成品跑 verify.py，再跑 diff_report.py 出改动报告。\n')
    write_report(os.path.join(a.out, '_split_report.txt'), ''.join(R))
    print('chapters=%d notes_src=%d notes_placed=%d' % (len(rows), len(notes), used_all))


if __name__ == '__main__':
    main()
