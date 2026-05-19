"""NDLOCR-LiteのXML出力を読み取り、Markdownに変換する。

NDLOCR-LiteのXMLは次のような構造（要点）::

    <OCRDATASET>
      <PAGE IMAGENAME="..." WIDTH="..." HEIGHT="...">
        <TEXTBLOCK CONF="0.974">
          <LINE TYPE="タイトル本文" X Y WIDTH HEIGHT ORDER STRING="…" />
          <LINE TYPE="本文" .../>
          <SHAPE><POLYGON POINTS="…"/></SHAPE>
        </TEXTBLOCK>
        <BLOCK TYPE="図版" X Y WIDTH HEIGHT />
        <BLOCK TYPE="表組" .../>
      </PAGE>
    </OCRDATASET>

行のTYPE値:
    タイトル本文 / 本文 / キャプション / ノンブル / 柱 / 表組 / 図版 / 広告文字
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from lxml import etree

from table_structure import TableConfig, render_table_block

# Markdown化の際に無視するLINE TYPE（ページ番号・柱書きなど読み物のノイズ）
SKIP_TYPES_DEFAULT = {"ノンブル", "柱", "広告文字"}

# 見出し相当として扱うLINE TYPE
HEADING_TYPES = {"タイトル本文"}

# キャプション
CAPTION_TYPES = {"キャプション"}


@dataclass
class Line:
    type_: str
    x: int
    y: int
    w: int
    h: int
    order: int
    text: str

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


@dataclass
class Block:
    type_: str  # TEXTBLOCK | 図版 | 表組 | etc
    lines: list[Line]
    x: int
    y: int
    w: int
    h: int

    @property
    def min_order(self) -> int:
        return min((ln.order for ln in self.lines), default=10**9)


# ページ上下端の何%以内にあるキャプション/本文を「柱書き(running header/footer)」扱いするか
RUNNING_HEADER_TOP_RATIO = 0.06
RUNNING_HEADER_BOTTOM_RATIO = 0.04


def _int(el, attr: str, default: int = 0) -> int:
    v = el.get(attr)
    if v is None:
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


def _extract_lines(el) -> list[Line]:
    """要素配下の LINE 要素から STRING を持つものを取り出す。"""
    lines: list[Line] = []
    for ln in el.iter():
        if etree.QName(ln).localname != "LINE":
            continue
        text = (ln.get("STRING") or "").strip()
        if not text:
            continue
        lines.append(
            Line(
                type_=(ln.get("TYPE") or "").strip(),
                x=_int(ln, "X"),
                y=_int(ln, "Y"),
                w=_int(ln, "WIDTH"),
                h=_int(ln, "HEIGHT"),
                order=_int(ln, "ORDER", default=10**9),
                text=text,
            )
        )
    return lines


def parse_page(page_el) -> tuple[list[Block], int, int]:
    """1ページ分のXML要素からブロック列・ページ幅・ページ高さを返す。"""
    page_w = _int(page_el, "WIDTH", 0)
    page_h = _int(page_el, "HEIGHT", 0)

    blocks: list[Block] = []

    for child in page_el:
        tag = etree.QName(child).localname
        if tag == "TEXTBLOCK":
            lines = _extract_lines(child)
            if not lines:
                continue
            x0 = min(ln.x for ln in lines)
            y0 = min(ln.y for ln in lines)
            x1 = max(ln.x + ln.w for ln in lines)
            y1 = max(ln.y + ln.h for ln in lines)
            blocks.append(Block("TEXTBLOCK", lines, x0, y0, x1 - x0, y1 - y0))

        elif tag == "BLOCK":
            t = (child.get("TYPE") or "").strip()
            inner_lines = _extract_lines(child)  # 表組 BLOCK は内側に LINE を持つ
            blocks.append(
                Block(
                    type_=t,
                    lines=inner_lines,
                    x=_int(child, "X"),
                    y=_int(child, "Y"),
                    w=_int(child, "WIDTH"),
                    h=_int(child, "HEIGHT"),
                )
            )

    # 読み順に並べる。テキストブロックは内部LINEのORDER最小値、非テキストブロックは y で。
    def sort_key(b: Block):
        if b.lines:
            return (b.min_order, b.y, b.x)
        return (10**9, b.y, b.x)

    blocks.sort(key=sort_key)
    return blocks, page_w, page_h


def _join_lines(lines: list[Line]) -> str:
    """ブロック内の行を連結する。

    日本語は通常スペースなしで連結し、行末が英数字・記号で終わる場合のみスペースを入れる、
    というシンプルな規則。改行は段落区切りとして扱わない（同一ブロック）。
    """
    out: list[str] = []
    for i, ln in enumerate(lines):
        t = ln.text
        if not t:
            continue
        if i == 0:
            out.append(t)
            continue
        prev = out[-1]
        # 行末ハイフン+英字なら結合
        if re.search(r"[A-Za-z]-$", prev) and re.match(r"^[A-Za-z]", t):
            out[-1] = prev[:-1] + t
            continue
        # 直前末尾が英数字/記号で、次の先頭も英数字なら半角スペースを挟む
        if re.search(r"[A-Za-z0-9\)\]]$", prev) and re.match(r"[A-Za-z0-9\(\[]", t):
            out.append(" " + t)
        else:
            out.append(t)
    return "".join(out)


def _heading_level(line: Line, page_w: int) -> int:
    """LINE.h（行高）からおおよその見出しレベル(2..4)を推定。

    値が大きいほど大見出し。ページ幅で正規化して安定化。
    """
    if page_w <= 0:
        return 3
    ratio = line.h / page_w
    if ratio >= 0.030:
        return 2
    if ratio >= 0.022:
        return 3
    return 4


def _looks_like_running_header(block: Block, page_w: int, page_h: int) -> bool:
    """ページ上端/下端の細い帯にある短いブロックを柱書き(running header/footer)と見做す。

    NDLOCR-Liteの「柱」分類は完全ではない。実運用では:
      - "キャプション"と誤分類された章節タイトル（ページ上端の帯）
      - "本文"と誤分類された単行のページヘッダ
    が混在する。位置+ブロック高さで救う。
    """
    if not block.lines or page_h <= 0:
        return False
    y_top = block.y
    y_bot = block.y + block.h
    in_top = y_top <= page_h * RUNNING_HEADER_TOP_RATIO
    in_bot = y_bot >= page_h * (1 - RUNNING_HEADER_BOTTOM_RATIO)
    if not (in_top or in_bot):
        return False

    # TYPEで明示的にキャプション/柱の場合
    if all(ln.type_ in {"キャプション", "柱"} for ln in block.lines):
        return True

    # ページ上下端にある "単一行" のブロックは柱書きとして扱う
    # （正規の見出しは通常もう少し下にある）
    if len(block.lines) == 1:
        return True

    return False


def block_to_markdown(
    block: Block,
    page_w: int,
    page_h: int,
    skip_types: set[str],
    include_figure_placeholder: bool,
    keep_running_header: bool,
    figures_map: dict[tuple[int, int, int, int], str] | None = None,
    tables_map: dict[tuple[int, int, int, int], str] | None = None,
    table_cfg: TableConfig | None = None,
) -> str | None:
    """1ブロックをMarkdown断片に変換。スキップ対象は None を返す。

    Args:
        figures_map: ``(x, y, w, h) -> 図版PNGの相対パス`` の辞書。
            ``図版`` BLOCK のキーがヒットすればコメントの代わりに ``![図版](path)`` を出力。
        tables_map: ``(x, y, w, h) -> 表組PNGの相対パス`` の辞書。
            ``--table-mode`` が ``grid+image`` / ``image`` / ``auto`` (fallback時) の
            場合に、表組PNGを埋め込むために使う。
        table_cfg: 表組の構造化変換の設定。``None`` で既定値。
    """
    if block.type_ in skip_types:
        return None

    if block.type_ == "図版":
        # 切り出し済み画像があれば埋め込む
        if figures_map:
            img_rel = figures_map.get((block.x, block.y, block.w, block.h))
            if img_rel:
                return f"![図版]({img_rel})"
        if include_figure_placeholder:
            return f"<!-- 図版 (x={block.x}, y={block.y}, w={block.w}, h={block.h}) -->"
        return None

    if block.type_ == "表組":
        # 表組PNGがあれば相対パスを取得
        img_rel: str | None = None
        if tables_map:
            img_rel = tables_map.get((block.x, block.y, block.w, block.h))
        return render_table_block(block, table_cfg or TableConfig(), image_rel=img_rel)

    # 柱書き（running header/footer）
    if not keep_running_header and _looks_like_running_header(block, page_w, page_h):
        return None

    # TEXTBLOCK
    lines = [ln for ln in block.lines if ln.text and ln.type_ not in skip_types]
    if not lines:
        return None

    # 先頭がタイトル本文なら見出しに昇格
    first = lines[0]
    rest = lines[1:]

    parts: list[str] = []
    if first.type_ in HEADING_TYPES:
        level = _heading_level(first, page_w)
        parts.append(("#" * level) + " " + first.text)
        if rest:
            if all(ln.type_ in CAPTION_TYPES for ln in rest):
                parts.append("*" + _join_lines(rest) + "*")
            else:
                parts.append(_join_lines(rest))
    elif all(ln.type_ in CAPTION_TYPES for ln in lines):
        parts.append("*" + _join_lines(lines) + "*")
    else:
        # 本文ブロック中に「タイトル本文」が混ざる場合は途中で見出しを差し込む
        if any(ln.type_ in HEADING_TYPES for ln in lines):
            buf: list[Line] = []
            chunks: list[str] = []
            for ln in lines:
                if ln.type_ in HEADING_TYPES:
                    if buf:
                        chunks.append(_join_lines(buf))
                        buf = []
                    level = _heading_level(ln, page_w)
                    chunks.append(("#" * level) + " " + ln.text)
                else:
                    buf.append(ln)
            if buf:
                chunks.append(_join_lines(buf))
            parts.extend(chunks)
        else:
            parts.append(_join_lines(lines))

    return "\n\n".join(parts)


def xml_to_markdown(
    xml_path: Path,
    skip_types: Iterable[str] = SKIP_TYPES_DEFAULT,
    include_figure_placeholder: bool = True,
    keep_running_header: bool = False,
    figures_map: dict[tuple[int, int, int, int], str] | None = None,
    tables_map: dict[tuple[int, int, int, int], str] | None = None,
    table_cfg: TableConfig | None = None,
) -> str:
    skip = set(skip_types)
    tree = etree.parse(str(xml_path))
    root = tree.getroot()

    page_el = root.find(".//PAGE") if root.tag != "PAGE" else root
    if page_el is None:
        return ""

    blocks, page_w, page_h = parse_page(page_el)

    md_chunks: list[str] = []
    for b in blocks:
        chunk = block_to_markdown(
            b, page_w, page_h, skip, include_figure_placeholder,
            keep_running_header, figures_map, tables_map, table_cfg,
        )
        if chunk:
            md_chunks.append(chunk)
    return "\n\n".join(md_chunks).rstrip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert NDLOCR-Lite XML to Markdown")
    ap.add_argument("xml", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--skip-types",
        nargs="*",
        default=sorted(SKIP_TYPES_DEFAULT),
        help="無視するLINE TYPE一覧",
    )
    ap.add_argument("--no-figure-placeholder", action="store_true")
    ap.add_argument("--keep-running-header", action="store_true", help="柱書き相当を残す")
    args = ap.parse_args()

    md = xml_to_markdown(
        args.xml,
        skip_types=set(args.skip_types),
        include_figure_placeholder=not args.no_figure_placeholder,
        keep_running_header=args.keep_running_header,
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
    else:
        print(md)


if __name__ == "__main__":
    main()
