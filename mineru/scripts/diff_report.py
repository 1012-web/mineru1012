"""交付前必跑：逐字符比对源文本与成品，列出所有实质改动。

用法:
    python diff_report.py <mineru输出目录> <成品.md>

剔除标点、空格、Markdown 语法后仍存在的差异 = 实质改动。
补标点不会出现在结果里；动了字一定会。

改动报告必须由这个脚本生成，不能靠回忆——凭印象列清单一定会漏，
而漏掉的往往正是最该报告的那条。
"""
import argparse
import difflib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (CIRC, CIRC_RE, block_text, body_blocks, die, footnote_blocks,
                     join_lines, line_texts, load_layout, significant, strip_tags, write_report)

META_PREFIX = ('收稿日期', '作者简介', '基金项目', '通信作者', '引用格式', '作者单位', 'DOI')
CIRC_SET = set(CIRC)


def md_sections(text):
    """把成品拆成 (正文, 注释)，并去掉不属于原文的部分。"""
    text = re.sub(r'^---\n.*?\n---\n', '', text, flags=re.S)            # frontmatter
    parts = re.split(r'\n#+\s*注释\s*\n', text, maxsplit=1)
    body, notes = parts[0], (parts[1] if len(parts) > 1 else '')
    # AI 校注 callout 是我们加的，不参与比对
    body = re.sub(r'>\s*\[!\w+\][^\n]*AI\s*校注[^\n]*\n(?:>[^\n]*\n?)*', '', body)
    notes = re.sub(r'>\s*\[!\w+\][^\n]*AI\s*校注[^\n]*\n(?:>[^\n]*\n?)*', '', notes)

    def clean(s):
        s = re.sub(r'<!--.*?-->', '', s, flags=re.S)                     # 页码锚点等注释
        s = re.sub(r'\[\^\d+\]:?', '', s)                                # 脚注引用/定义标签
        s = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', s)                       # 图片
        s = re.sub(r'>\s*\[!\w+\]\s*', '', s)                            # callout 标记（保留标题文字）
        s = re.sub(r'^\s*[>#]+\s*', '', s, flags=re.M)                   # 引用/标题前缀
        return s.replace('**', '').replace('---', '')

    return clean(body), clean(notes)


def source_sections(layout):
    body = ''.join(strip_tags(block_text(b)) for pg in layout for b in body_blocks(pg))
    notes = []
    for pg in layout:
        for b in footnote_blocks(pg):
            t = strip_tags(b.get('text') or join_lines(line_texts(b))).strip()
            if not t or t.startswith(META_PREFIX):
                continue
            notes.append(CIRC_RE.sub('', t, count=1) if CIRC_RE.match(t) else t)
    return body, ''.join(notes)


def compare(name, src, out, R):
    a, b = significant(src), significant(out)
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        s, t = a[i1:i2], b[j1:j2]
        # 圈码被换成 [^n]（标签已在 clean 里去掉），属于预期转换，不算改动
        if tag == 'delete' and s and set(s) <= CIRC_SET:
            continue
        rows.append((tag, s, t, a[max(0, i1 - 22):i1], a[i2:i2 + 22]))

    R.append('\n## %s\n' % name)
    R.append('源 %d 实义字符 / 成品 %d，差异 %d 处\n' % (len(a), len(b), len(rows)))
    if not rows:
        R.append('-> 零改动。\n')
        return 0
    for tag, s, t, L, Rr in rows:
        label = {'replace': '改', 'insert': '增', 'delete': '删'}.get(tag, tag)
        R.append('\n[%s] 源「%s」-> 成品「%s」\n' % (label, s, t))
        R.append('     …%s▶%s◀%s…\n' % (L, s, Rr))
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dir')
    ap.add_argument('md')
    a = ap.parse_args()
    if not os.path.isdir(a.dir):
        die('目录不存在: ' + a.dir)

    layout = load_layout(a.dir)
    src_body, src_notes = source_sections(layout)
    md_body, md_notes = md_sections(open(a.md, encoding='utf-8').read())

    R = ['# 改动报告\n']
    R.append('源: %s\n成品: %s\n' % (os.path.abspath(a.dir), os.path.abspath(a.md)))
    R.append('\n比对口径：剔除标点、空格、Markdown 语法后逐字符比对。\n')
    R.append('补标点不会出现在下面；动了字一定会出现。\n')

    n1 = compare('正文', src_body, md_body, R)
    n2 = compare('脚注', src_notes, md_notes, R)

    R.append('\n## 怎么用这份报告\n')
    R.append('  逐条判断每处差异属于哪类，然后写进给用户的报告，按重要度排序：\n')
    R.append('    1 内容改字   —— 逐条写明位置、原文、改后、依据。最需要用户知道的。\n')
    R.append('    2 内容补入   —— 脚注、元数据等从 JSON 捡回来的。\n')
    R.append('    3 结构判定   —— 标题降级、引用块认定。\n')
    R.append('    4 标点格式   —— 概括一句即可。\n')
    R.append('    5 位置迁移   —— 如刊号移入 frontmatter，字没变只是搬家。\n')
    R.append('  「删」类差异多半是第 5 类（搬进了 frontmatter），别误报成删除内容。\n')
    R.append('  凡是动了字的，都要进文末的 AI 校注 callout；需要用户拍板的加 ⚠️。\n')

    write_report(os.path.join(os.path.dirname(os.path.abspath(a.md)), '_diff_report.txt'), ''.join(R))
    print('body_diffs=%d note_diffs=%d' % (n1, n2))


if __name__ == '__main__':
    main()
