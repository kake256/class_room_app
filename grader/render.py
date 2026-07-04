"""PDF → ページPNG画像(PyMuPDF)。"""
from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass

import fitz  # PyMuPDF

log = logging.getLogger(__name__)


@dataclass
class RenderResult:
    pages: list[pathlib.Path]
    total_pages: int
    truncated: bool


def render_pdf(
    pdf_path: str | pathlib.Path,
    out_dir: str | pathlib.Path,
    dpi: int = 150,
    max_pages: int = 8,
    force: bool = False,
) -> RenderResult:
    """PDFの各ページをPNGにレンダリングする。max_pages超過分は切り捨て。

    出力済みでPDFより新しければスキップ(冪等)。
    """
    pdf_path = pathlib.Path(pdf_path)
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with fitz.open(pdf_path) as doc:
        total = doc.page_count
        n = min(total, max_pages)
        pages: list[pathlib.Path] = []
        for i in range(n):
            png = out_dir / f"p{i + 1}.png"
            if force or not png.exists() or png.stat().st_mtime < pdf_path.stat().st_mtime:
                pix = doc[i].get_pixmap(dpi=dpi)
                pix.save(png)
            pages.append(png)

    truncated = total > max_pages
    if truncated:
        log.warning("%s: %d pages, truncated to %d", pdf_path.name, total, max_pages)
    return RenderResult(pages=pages, total_pages=total, truncated=truncated)
