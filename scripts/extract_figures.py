"""NDLOCR-Lite の XML に含まれる ``<BLOCK TYPE="図版">`` を、対応するページ
画像 (PNG) から切り取って保存する。

NDLOCR-Lite の座標系はOCRに渡した元画像の画素座標と同じ。
``<PAGE WIDTH HEIGHT/>`` と Pillow が読んだ画像の実サイズが一致するのが基本だが、
万一ずれていてもスケール補正して切り出す。

Returnは図版BLOCK毎の dict のリスト。``key`` フィールドは markdown 側で
``figures_map`` のキーに使う ``(x, y, w, h)`` のタプル文字列。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from lxml import etree
from PIL import Image


# 切り出し時、図版の境界に少し余白を持たせるためのパディング(px)
DEFAULT_PADDING = 4


@dataclass
class Figure:
    """切り取った1つの図版領域。"""
    block_type: str
    x: int
    y: int
    w: int
    h: int
    path: Path

    @property
    def key(self) -> tuple[int, int, int, int]:
        """markdown側で ``figures_map`` のキーに使う座標タプル。

        XMLの ``BLOCK`` 要素の X/Y/WIDTH/HEIGHT そのまま（パディング前）。
        """
        return (self.x, self.y, self.w, self.h)


def _i(el, attr: str, default: int = 0) -> int:
    v = el.get(attr)
    if v is None:
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


def extract_figures_from_page(
    image_path: Path,
    xml_path: Path,
    out_dir: Path,
    block_types: Iterable[str] = ("図版",),
    padding: int = DEFAULT_PADDING,
    name_prefix: str | None = None,
) -> list[Figure]:
    """1ページ分のXMLを読み、対応PNGから図版BLOCKを切り出して保存する。

    Args:
        image_path: ページPNG
        xml_path:   NDLOCR-Lite が生成したXML
        out_dir:    保存先ディレクトリ (なければ作成)
        block_types: 切り出す ``BLOCK/TYPE`` の集合 (既定: ``図版`` のみ)
        padding:    切り出し領域の上下左右に加える余白 (px)
        name_prefix: 出力ファイル名のprefix。省略時は ``image_path.stem`` を使う。

    Returns:
        切り出した図版の ``Figure`` リスト
    """
    image_path = Path(image_path)
    xml_path = Path(xml_path)
    out_dir = Path(out_dir)

    block_types = set(block_types)
    tree = etree.parse(str(xml_path))
    root = tree.getroot()
    page_el = root.find(".//PAGE") if root.tag != "PAGE" else root
    if page_el is None:
        return []

    page_w = _i(page_el, "WIDTH", 0)
    page_h = _i(page_el, "HEIGHT", 0)

    # 対象BLOCKを収集
    blocks: list[dict] = []
    for b in page_el.findall("BLOCK"):
        t = (b.get("TYPE") or "").strip()
        if t not in block_types:
            continue
        x = _i(b, "X")
        y = _i(b, "Y")
        w = _i(b, "WIDTH")
        h = _i(b, "HEIGHT")
        if w <= 0 or h <= 0:
            continue
        blocks.append({"type": t, "x": x, "y": y, "w": w, "h": h})

    if not blocks:
        return []

    # 並び順: 上→下、左→右 (markdown側で同じ順に並ぶ)
    blocks.sort(key=lambda b: (b["y"], b["x"]))

    # 画像読み込み (図版が無いページではここまで来ない)
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as im:
        # NDLOCR-Lite は入力画像をそのまま使うため通常 page_w/h == im.size。
        # 万一の不一致に備えてスケール係数を保険として計算。
        iw, ih = im.size
        sx = iw / page_w if page_w > 0 else 1.0
        sy = ih / page_h if page_h > 0 else 1.0

        stem = name_prefix or image_path.stem
        figures: list[Figure] = []
        for i, b in enumerate(blocks, start=1):
            x0 = max(0, int(round(b["x"] * sx)) - padding)
            y0 = max(0, int(round(b["y"] * sy)) - padding)
            x1 = min(iw, int(round((b["x"] + b["w"]) * sx)) + padding)
            y1 = min(ih, int(round((b["y"] + b["h"]) * sy)) + padding)
            if x1 <= x0 or y1 <= y0:
                continue
            crop = im.crop((x0, y0, x1, y1))
            out_path = out_dir / f"{stem}_fig{i:02d}.png"
            crop.save(out_path)
            figures.append(
                Figure(
                    block_type=b["type"],
                    x=b["x"], y=b["y"], w=b["w"], h=b["h"],
                    path=out_path,
                )
            )

    return figures


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract 図版 regions from NDLOCR-Lite XML + page PNG",
    )
    ap.add_argument("xml", type=Path, help="NDLOCR-Lite が出力したXML")
    ap.add_argument(
        "--image",
        type=Path,
        required=True,
        help="ページの元画像PNG (XMLと同じページのもの)",
    )
    ap.add_argument("--out", type=Path, required=True, help="切り出し画像の保存先")
    ap.add_argument(
        "--types",
        nargs="*",
        default=["図版"],
        help="切り出すBLOCK/TYPE一覧 (例: 図版 表組)",
    )
    ap.add_argument("--padding", type=int, default=DEFAULT_PADDING)
    args = ap.parse_args()

    figs = extract_figures_from_page(
        image_path=args.image,
        xml_path=args.xml,
        out_dir=args.out,
        block_types=args.types,
        padding=args.padding,
    )
    for f in figs:
        print(f"{f.block_type}\t{f.x},{f.y},{f.w}x{f.h}\t{f.path}")
    print(f"# extracted {len(figs)} figure(s)")


if __name__ == "__main__":
    main()
