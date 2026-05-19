"""PDF → 画像 → NDLOCR-Lite → Markdown のEnd-to-Endパイプライン。

入力ディレクトリ内のすべてのPDFをバッチ処理し、出力ディレクトリに各PDFごとの
Markdown（全ページ結合）と中間生成物を残す。

ディレクトリ構成（例: --in data/disaster --out output/disaster）::

    output/disaster/
      <pdf_stem>/
        images/        # ページPNG (NDLOCR-Liteに食わせる)
        ocr/           # ndlocr-lite の出力 (.xml/.json/.txt)
        figures/       # 図版BLOCKを切り出したPNG
        pages/         # ページごとのMarkdown
        <pdf_stem>.md  # 全ページ結合済みMarkdown
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

# ローカルmodule
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf_to_images import pdf_to_images, _safe_name  # noqa: E402
from ndl_xml_to_markdown import xml_to_markdown, SKIP_TYPES_DEFAULT  # noqa: E402
from extract_figures import extract_figures_from_page  # noqa: E402


log = logging.getLogger("pdf2md")


def setup_logging(log_path: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def find_ndlocr_lite() -> str:
    """ndlocr-liteの実行ファイルを探す。"""
    for cand in (
        Path("/Users/tsuyoshi/.local/bin/ndlocr-lite"),
        Path.home() / ".local/bin/ndlocr-lite",
    ):
        if cand.exists():
            return str(cand)
    found = shutil.which("ndlocr-lite")
    if found:
        return found
    raise FileNotFoundError(
        "ndlocr-lite コマンドが見つかりません。 "
        "`uv tool install ./vendor/ndlocr-lite` を実行してください。"
    )


def run_ndlocr_lite(
    sourcedir: Path,
    outdir: Path,
    bin_path: str,
    enable_tcy: bool = False,
    extra_args: list[str] | None = None,
) -> None:
    """ndlocr-liteを実行。失敗するとCalledProcessErrorをそのままraise。"""
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        bin_path,
        "--sourcedir", str(sourcedir),
        "--output", str(outdir),
    ]
    if enable_tcy:
        cmd.append("--enable-tcy")
    if extra_args:
        cmd.extend(extra_args)
    log.info("$ %s", " ".join(cmd))
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("ndlocr-lite stdout:\n%s", proc.stdout)
        log.error("ndlocr-lite stderr:\n%s", proc.stderr)
        raise RuntimeError(f"ndlocr-lite failed (rc={proc.returncode})")
    if proc.stdout:
        log.debug("ndlocr-lite stdout:\n%s", proc.stdout)


_PAGE_RE = re.compile(r"_p(\d+)$")


def _page_no_from_stem(stem: str) -> int:
    m = _PAGE_RE.search(stem)
    return int(m.group(1)) if m else 10**9


def extract_figures_for_pdf(
    images_dir: Path,
    xml_dir: Path,
    figures_dir: Path,
    block_types: Iterable[str] = ("図版",),
) -> tuple[dict[str, dict[tuple[int, int, int, int], str]], int]:
    """全ページの図版BLOCKを切り出し、ページごとの ``figures_map`` を返す。

    Returns:
        (per_page_maps, total)
        per_page_maps: ``xml.stem -> {(x,y,w,h): "figures/xxx.png"}``。
            markdownから参照しやすいよう、画像パスは combined_md からの相対パス
            (``figures/<stem>_p####_fig##.png``) で格納する。
        total: 切り出した図版の総数
    """
    per_page: dict[str, dict[tuple[int, int, int, int], str]] = {}
    total = 0

    xmls = sorted(xml_dir.glob("*.xml"), key=lambda p: _page_no_from_stem(p.stem))
    for xml_path in xmls:
        # 同名のPNGを探す
        png_path = images_dir / f"{xml_path.stem}.png"
        if not png_path.exists():
            # 画像が削除済みの場合はスキップ（既存OCR結果のみ手元にあるケース）
            continue
        figs = extract_figures_from_page(
            image_path=png_path,
            xml_path=xml_path,
            out_dir=figures_dir,
            block_types=block_types,
        )
        if not figs:
            continue
        page_map: dict[tuple[int, int, int, int], str] = {}
        for f in figs:
            # combined_md / pages/<page>.md の両方から参照できるよう、
            # 出力ルート(figures_dirの1つ上)を基準にした相対パスにする。
            rel = f"figures/{f.path.name}"
            page_map[f.key] = rel
        per_page[xml_path.stem] = page_map
        total += len(figs)

    return per_page, total


def combine_pages_markdown(
    xml_dir: Path,
    pages_dir: Path,
    combined_md: Path,
    pdf_name: str,
    skip_types: set[str],
    keep_running_header: bool = False,
    figures_per_page: dict[str, dict[tuple[int, int, int, int], str]] | None = None,
) -> int:
    """OCR XML をページMarkdownに変換し、ページ番号順に結合する。

    戻り値: 結合に使ったページ数
    """
    pages_dir.mkdir(parents=True, exist_ok=True)

    xml_files = sorted(xml_dir.glob("*.xml"), key=lambda p: _page_no_from_stem(p.stem))
    if not xml_files:
        log.warning("XMLが見つかりません: %s", xml_dir)
        return 0

    combined_md.parent.mkdir(parents=True, exist_ok=True)
    figures_per_page = figures_per_page or {}
    with combined_md.open("w", encoding="utf-8") as fout:
        fout.write(f"# {pdf_name}\n\n")
        fout.write(f"<!-- 自動生成: NDLOCR-Lite + PyMuPDF -->\n\n")

        for xml_path in xml_files:
            page_no = _page_no_from_stem(xml_path.stem)
            page_figs = figures_per_page.get(xml_path.stem)
            # combined_mdから見た相対パスはそのまま使える(同じディレクトリ階層)。
            md_for_combined = xml_to_markdown(
                xml_path,
                skip_types=skip_types,
                keep_running_header=keep_running_header,
                figures_map=page_figs,
            )
            # ページ別.mdは1階層深いので "../figures/..." に書き換え
            md_for_page = xml_to_markdown(
                xml_path,
                skip_types=skip_types,
                keep_running_header=keep_running_header,
                figures_map=(
                    {k: f"../{v}" for k, v in page_figs.items()} if page_figs else None
                ),
            )
            page_md_path = pages_dir / f"{xml_path.stem}.md"
            page_md_path.write_text(md_for_page, encoding="utf-8")

            fout.write(f"\n\n---\n\n")
            fout.write(f"<!-- page {page_no} -->\n\n")
            fout.write(md_for_combined.rstrip())
            fout.write("\n")

    return len(xml_files)


def process_pdf(
    pdf_path: Path,
    out_root: Path,
    dpi: int,
    enable_tcy: bool,
    first_page: int | None,
    last_page: int | None,
    skip_types: set[str],
    ndlocr_bin: str,
    keep_images: bool,
    keep_running_header: bool,
    extract_figure_types: tuple[str, ...],
    extra_ocr_args: list[str] | None,
) -> None:
    """1つのPDFを処理する。"""
    t0 = time.time()
    safe_stem = _safe_name(pdf_path.stem)
    pdf_out_root = out_root / safe_stem
    images_dir = pdf_out_root / "images"
    ocr_dir = pdf_out_root / "ocr"
    figures_dir = pdf_out_root / "figures"
    pages_dir = pdf_out_root / "pages"
    combined_md = pdf_out_root / f"{safe_stem}.md"

    log.info("=== %s ===", pdf_path.name)
    log.info("output root: %s", pdf_out_root)

    # 1) PDF -> images
    if images_dir.exists() and any(images_dir.glob("*.png")):
        log.info("images already exist, skipping render: %s", images_dir)
    else:
        log.info("rendering PDF to PNG @%d dpi", dpi)
        pdf_to_images(
            pdf_path,
            images_dir,
            dpi=dpi,
            first_page=first_page,
            last_page=last_page,
            prefix=safe_stem,
        )

    # 2) OCR
    if ocr_dir.exists() and any(ocr_dir.glob("*.xml")):
        log.info("OCR results already exist, skipping OCR: %s", ocr_dir)
    else:
        log.info("running ndlocr-lite ...")
        run_ndlocr_lite(
            sourcedir=images_dir,
            outdir=ocr_dir,
            bin_path=ndlocr_bin,
            enable_tcy=enable_tcy,
            extra_args=extra_ocr_args,
        )

    # 3) 図版BLOCKを切り出してPNG保存 (画像cleanupより前に必ず実行する)
    figures_per_page: dict[str, dict[tuple[int, int, int, int], str]] = {}
    if extract_figure_types:
        if not (images_dir.exists() and any(images_dir.glob("*.png"))):
            # OCR結果はあるが画像が無い場合、必要なページだけ再レンダする。
            log.info("images missing — re-rendering for figure extraction")
            pdf_to_images(
                pdf_path,
                images_dir,
                dpi=dpi,
                first_page=first_page,
                last_page=last_page,
                prefix=safe_stem,
            )
        log.info("extracting figures (%s) ...", ",".join(extract_figure_types))
        figures_per_page, n_figs = extract_figures_for_pdf(
            images_dir=images_dir,
            xml_dir=ocr_dir,
            figures_dir=figures_dir,
            block_types=extract_figure_types,
        )
        log.info("extracted %d figure(s) into %s", n_figs, figures_dir)

    # 4) XML -> Markdown (per page + combined)
    n_pages = combine_pages_markdown(
        xml_dir=ocr_dir,
        pages_dir=pages_dir,
        combined_md=combined_md,
        pdf_name=pdf_path.name,
        skip_types=skip_types,
        keep_running_header=keep_running_header,
        figures_per_page=figures_per_page,
    )

    # 5) optional cleanup
    if not keep_images:
        try:
            shutil.rmtree(images_dir)
            log.info("cleaned up images dir: %s", images_dir)
        except Exception as e:
            log.warning("failed to clean up images: %s", e)

    log.info("done %s (%d pages) in %.1fs", pdf_path.name, n_pages, time.time() - t0)


def main() -> None:
    ap = argparse.ArgumentParser(description="Bulk PDF -> Markdown via NDLOCR-Lite")
    ap.add_argument("--in", dest="in_dir", type=Path, required=True, help="PDF入力ディレクトリ")
    ap.add_argument("--out", dest="out_dir", type=Path, required=True, help="出力ルート")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--enable-tcy", action="store_true", help="縦中横の読み取り改善")
    ap.add_argument("--first-page", type=int, default=None)
    ap.add_argument("--last-page", type=int, default=None)
    ap.add_argument("--keep-images", action="store_true", help="中間PNGを削除しない")
    ap.add_argument(
        "--keep-running-header",
        action="store_true",
        help="ページ上下端の柱書き相当(誤分類のキャプション含む)を残す",
    )
    ap.add_argument(
        "--extract-figures",
        nargs="*",
        default=["図版"],
        metavar="BLOCK_TYPE",
        help="ページ画像から切り出すBLOCK/TYPE一覧 (空指定で抽出オフ)。例: 図版 表組",
    )
    ap.add_argument(
        "--skip-types",
        nargs="*",
        default=sorted(SKIP_TYPES_DEFAULT),
        help="Markdown化時に無視するLINE TYPE一覧",
    )
    ap.add_argument(
        "--glob",
        default="*.pdf",
        help="入力ディレクトリ内のファイル選択パターン (default: *.pdf)",
    )
    ap.add_argument("--log", type=Path, default=Path("logs/pipeline.log"))
    ap.add_argument("--continue-on-error", action="store_true")
    args = ap.parse_args()

    setup_logging(args.log)
    bin_path = find_ndlocr_lite()
    log.info("ndlocr-lite: %s", bin_path)

    pdfs = sorted(args.in_dir.glob(args.glob))
    if not pdfs:
        log.error("PDFが見つかりません: %s (%s)", args.in_dir, args.glob)
        sys.exit(1)
    log.info("found %d PDFs", len(pdfs))

    skip = set(args.skip_types)

    failures: list[tuple[Path, Exception]] = []
    for pdf in pdfs:
        try:
            process_pdf(
                pdf_path=pdf,
                out_root=args.out_dir,
                dpi=args.dpi,
                enable_tcy=args.enable_tcy,
                first_page=args.first_page,
                last_page=args.last_page,
                skip_types=skip,
                ndlocr_bin=bin_path,
                keep_images=args.keep_images,
                keep_running_header=args.keep_running_header,
                extract_figure_types=tuple(args.extract_figures),
                extra_ocr_args=None,
            )
        except Exception as e:
            log.exception("failed for %s: %s", pdf, e)
            failures.append((pdf, e))
            if not args.continue_on_error:
                sys.exit(2)

    if failures:
        log.error("%d failures", len(failures))
        sys.exit(2)


if __name__ == "__main__":
    main()
