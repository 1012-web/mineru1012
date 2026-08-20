"""从原始 PDF 裁图核对，或剥离文字层生成可 OCR 的 PDF。

用法:
    # 裁一块区域出来看（坐标用 layout.json 里的 bbox，单位 pt，原点左上）
    python crop_pdf.py <origin.pdf> --page 2 --rect 65,545,300,559 -o crop.png

    # 剥掉文字层，光栅化成纯图 PDF，喂回 MinerU 走 OCR
    python crop_pdf.py <origin.pdf> --strip-text-layer -o 无文字层.pdf --dpi 300

裁图之后用 Read 工具看那张图。改动原文之前先看原图，
否则分不清是原刊印错还是 MinerU 认错——这个区别决定了该不该改。

依赖 pymupdf。没装的话:
    python -m pip install pymupdf
或建个隔离环境避免污染系统 Python:
    python -m venv v && ./v/Scripts/python -m pip install pymupdf   # Windows
"""
import argparse
import os
import sys

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        print('ERROR: 需要 pymupdf。装法见本文件顶部的说明。')
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pdf')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--page', type=int, default=0, help='0 起的页序号，即 layout.json 的 page_idx')
    ap.add_argument('--rect', default=None, help='x0,y0,x1,y1（pt），留空则整页')
    ap.add_argument('--dpi', type=int, default=450)
    ap.add_argument('--strip-text-layer', action='store_true',
                    help='把每页光栅化成图片重新封装，产出没有文字层的 PDF')
    a = ap.parse_args()

    doc = pymupdf.open(a.pdf)

    if a.strip_text_layer:
        out = pymupdf.open()
        for pg in doc:
            pix = pg.get_pixmap(dpi=a.dpi)
            np = out.new_page(width=pg.rect.width, height=pg.rect.height)
            np.insert_image(pg.rect, pixmap=pix)
        out.save(a.out, deflate=True, garbage=4)
        out.close()
        chk = pymupdf.open(a.out)
        left = sum(len(p.get_text()) for p in chk)
        print('pages=%d dpi=%d size=%.1fMB residual_text_chars=%d'
              % (doc.page_count, a.dpi, os.path.getsize(a.out) / 1048576, left))
        if left:
            print('WARNING: 还残留文字层，MinerU 可能仍走文字提取而非 OCR')
        chk.close()
        return

    pg = doc[a.page]
    clip = None
    if a.rect:
        x0, y0, x1, y1 = [float(v) for v in a.rect.replace('，', ',').split(',')]
        clip = pymupdf.Rect(x0, y0, x1, y1)
    pg.get_pixmap(dpi=a.dpi, clip=clip).save(a.out)
    print('saved %s (page_idx=%d, dpi=%d) -- 用 Read 工具看它' % (a.out, a.page, a.dpi))


if __name__ == '__main__':
    main()
