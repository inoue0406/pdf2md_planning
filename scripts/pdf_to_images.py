"""PDFをページ単位のPNG画像に変換する。

NDLOCR-Liteは画像入力のみ対応のため、OCR前にPDFを画像化する必要がある。
PyMuPDF (fitz) を使い、指定DPIでレンダリング。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import fitz  # PyMuPDF
from tqdm import tqdm


def pdf_to_images(
    pdf_path: Path,
    out_dir: Path,
    dpi: int = 300,
    first_page: int | None = None,
    last_page: int | None = None,
    prefix: str | None = None,
) -> list[Path]:
    """PDFをページPNGに変換し、生成された画像のパス一覧を返す。

    画像ファイル名は ``{prefix}_p{0001}.png`` 形式（ゼロ埋め4桁）。
    prefix未指定ならPDFファイル名のstem（拡張子なし）を使用。
    """
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if prefix is None:
        prefix = pdf_path.stem

    # NDLOCR-Liteは日本語パス・空白に弱いとされる。サブディレクトリは英数字のみで作る。
    # ファイル名側のprefixが日本語を含む場合に備えてサニタイズ。
    safe_prefix = _safe_name(prefix)

    zoom = dpi / 72.0  # PDFのデフォルトは72dpi
    matrix = fitz.Matrix(zoom, zoom)

    written: list[Path] = []
    with fitz.open(pdf_path) as doc:
        n_pages = doc.page_count
        start = (first_page or 1) - 1
        end = min(last_page or n_pages, n_pages)

        for i in tqdm(range(start, end), desc=f"rendering {pdf_path.name}", unit="page"):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out = out_dir / f"{safe_prefix}_p{i + 1:04d}.png"
            pix.save(out)
            written.append(out)

    return written


def _safe_name(name: str) -> str:
    """日本語・空白・括弧などをアンダーバーに置換した安全なファイル名を返す。"""
    safe_chars = []
    for ch in name:
        if ch.isascii() and (ch.isalnum() or ch in ("-", "_", ".")):
            safe_chars.append(ch)
        else:
            safe_chars.append("_")
    out = "".join(safe_chars).strip("_")
    return out or "page"


def main() -> None:
    ap = argparse.ArgumentParser(description="PDF -> per-page PNG images via PyMuPDF")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="output image directory")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--first-page", type=int, default=None)
    ap.add_argument("--last-page", type=int, default=None)
    ap.add_argument("--prefix", type=str, default=None)
    args = ap.parse_args()

    paths = pdf_to_images(
        args.pdf,
        args.out,
        dpi=args.dpi,
        first_page=args.first_page,
        last_page=args.last_page,
        prefix=args.prefix,
    )
    print(f"wrote {len(paths)} images to {args.out}")


if __name__ == "__main__":
    main()
