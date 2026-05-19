"""NDLOCR-Lite の ``<BLOCK TYPE="表組">`` の中身（座標つきLINE要素群）から、
Markdownの表 (``| a | b |\n|---|---|\n| c | d |``) を再構築する。

NDLOCR-LiteのXMLには行・列インデックスが含まれない（座標のみ）ので、
ここではY座標の1次元クラスタリングで行を、X左辺の1次元クラスタリングで列を
推定し、各LINEを最寄りの(行, 列)に割り当てる。

- 縦書きの大きなラベル(`第N部…` のような rowspan 的要素)は、列クラスタリングの
  ノイズになりやすいため、列アンカー計算には**横書きLINEのみ**を使う。
  そのうえで、縦書きLINEは「その x 中央が一番近い列」に配置する。
- 多くの行政文書の表は2列〜十数列。``cfg.max_cols`` を超える推定が出たら破綻と
  みなして ``None`` を返し、呼び出し側で旧表現（フラット箇条書きなど）に
  フォールバックする想定。
- 結合セル (rowspan/colspan) はMarkdownで表現不能なため、MVPでは「該当する1
  セルにだけテキストを書き、他の対応セルは空欄」というベストエフォート方針。

このモジュールは ``Block``/``Line`` 型を ``ndl_xml_to_markdown.py`` 側から
import せず、 ``.x .y .w .h .text`` 属性を持つオブジェクトとして duck-type で
扱う（循環import回避）。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class TableConfig:
    """表組の構造化変換に関する閾値とオプション。"""

    max_cols: int = 12
    """これより列が多いと破綻とみなして None を返す。"""

    min_rows: int = 2
    """これより行が少ないと表とみなさない。"""

    row_thresh_ratio: float = 0.6
    """行クラスタしきい値 = max(8, 行高中央値 * これ)"""

    col_thresh_ratio: float = 0.8
    """列クラスタしきい値 = max(20, 行高中央値 * これ)"""

    newline_in_cell: str = "<br>"
    """セル内の改行(マルチライン本文セルの結合)の表現。"""

    header_mode: str = "auto"
    """auto: 第1行をヘッダにして罫線を出す / first-row: 同上 / none: 罫線なし(GFM非対応)"""


# --- 内部ヘルパ ---------------------------------------------------------------


def _line_is_vertical(ln: Any) -> bool:
    """縦書きとみなすかどうか。アスペクト比から判定。"""
    return ln.w > 0 and ln.h >= ln.w * 2.0


def _greedy_cluster_sorted(values: list[float], threshold: float) -> list[list[float]]:
    """昇順済みの1次元値を、隣接間隔がthreshold以下のグループに切る。"""
    if not values:
        return []
    clusters: list[list[float]] = [[values[0]]]
    for v in values[1:]:
        if v - clusters[-1][-1] <= threshold:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return clusters


# --- 行・列のクラスタリング ---------------------------------------------------


def cluster_rows(lines: list[Any], threshold: float) -> list[list[int]]:
    """LINEのY中央値で行クラスタを作る。

    Returns:
        行ごとの「lines のインデックスのリスト」のリスト。Y昇順。
    """
    if not lines:
        return []
    indexed = sorted(
        enumerate(lines), key=lambda p: p[1].y + p[1].h / 2
    )
    rows: list[list[int]] = []
    cur: list[int] = []
    cur_last_cy: float | None = None
    for idx, ln in indexed:
        cy = ln.y + ln.h / 2
        if cur_last_cy is None or cy - cur_last_cy <= threshold:
            cur.append(idx)
            cur_last_cy = cy
        else:
            rows.append(cur)
            cur = [idx]
            cur_last_cy = cy
    if cur:
        rows.append(cur)
    return rows


def _line_ref_x(ln: Any) -> float:
    """列クラスタ用のXの基準値。横書きは左端、縦書きは中央。"""
    if _line_is_vertical(ln):
        return ln.x + ln.w / 2
    return float(ln.x)


def cluster_cols(
    lines: list[Any],
    use_indices: Iterable[int],
    threshold: float,
) -> list[int]:
    """LINEの基準X (横書き=x_left / 縦書き=cx) を1次元クラスタリングし、
    続いてX範囲(=x_left..x_right)が重なるクラスタを再結合して、各列のアンカーX を返す。

    - 横書きセル内に複数行(マルチライン本文)があるとき、それらは概ね同じ x_left を共有する
      ため左端基準が自然。
    - 縦書きラベルは細長く x_left が列の左寄りに張り出すので中央xの方が列代表値として妥当。
    - ヘッダ行など x_left がデータ行とずれているケースがあるため、クラスタ後に **X範囲が
      重なるもの同士を統合** する。これで「機関の名称」のような中央寄せヘッダが
      データ列に正しくマージされる。
    """
    indices = list(use_indices)
    if not indices:
        return []
    # (ref_x, line_idx) でソート
    pairs = sorted(((_line_ref_x(lines[i]), i) for i in indices), key=lambda p: p[0])
    # Phase 1: greedy 1D clustering on ref_x
    clusters: list[list[int]] = [[pairs[0][1]]]
    last_ref = pairs[0][0]
    for ref_x, i in pairs[1:]:
        if ref_x - last_ref <= threshold:
            clusters[-1].append(i)
        else:
            clusters.append([i])
        last_ref = ref_x

    # Phase 2: 重なる X 範囲を持つクラスタ同士をマージする
    def cluster_range(cl: list[int]) -> tuple[int, int]:
        xs_left = [lines[i].x for i in cl]
        xs_right = [lines[i].x + lines[i].w for i in cl]
        return (min(xs_left), max(xs_right))

    merged: list[list[int]] = []
    ranges: list[tuple[int, int]] = []
    # min_x_left の昇順で処理
    indexed_clusters = sorted(clusters, key=lambda cl: cluster_range(cl)[0])
    for cl in indexed_clusters:
        xl, xr = cluster_range(cl)
        if merged and xl < ranges[-1][1]:
            # 直前クラスタとX範囲が重なる → マージ
            merged[-1].extend(cl)
            prev_xl, prev_xr = ranges[-1]
            ranges[-1] = (min(prev_xl, xl), max(prev_xr, xr))
        else:
            merged.append(list(cl))
            ranges.append((xl, xr))

    anchors = [
        int(round(statistics.median(_line_ref_x(lines[i]) for i in cl)))
        for cl in merged
    ]
    anchors.sort()
    return anchors


def assign_cells(
    lines: list[Any],
    row_groups: list[list[int]],
    col_anchors: list[int],
) -> dict[tuple[int, int], list[int]]:
    """各LINEを最寄りの (row, col) に割り当てる。

    Returns:
        ``(row_idx, col_idx) -> [line_idx, ...]`` (各セルは Y, X 昇順)
    """
    grid: dict[tuple[int, int], list[int]] = {}
    for r, indices in enumerate(row_groups):
        for i in indices:
            ln = lines[i]
            ref_x = _line_ref_x(ln)
            best_c = min(
                range(len(col_anchors)),
                key=lambda c: abs(ref_x - col_anchors[c]),
            )
            grid.setdefault((r, best_c), []).append(i)
    for k in grid:
        grid[k].sort(key=lambda i: (lines[i].y, lines[i].x))
    return grid


# --- Markdown描画 -------------------------------------------------------------


def _sanitize_cell(text: str, newline: str) -> str:
    text = text.replace("\r", " ").replace("\t", " ")
    text = text.replace("\n", newline)
    text = text.replace("|", r"\|")
    return text.strip()


def render_markdown_table(
    grid: dict[tuple[int, int], list[int]],
    n_rows: int,
    n_cols: int,
    lines: list[Any],
    newline: str = "<br>",
    header_mode: str = "auto",
) -> str:
    """grid から Markdown 表テキストを組み立てる。"""

    def cell_text(r: int, c: int) -> str:
        idxs = grid.get((r, c), [])
        if not idxs:
            return ""
        return _sanitize_cell(newline.join(lines[i].text for i in idxs), newline)

    out: list[str] = []

    if header_mode in ("auto", "first-row"):
        header = [cell_text(0, c) or " " for c in range(n_cols)]
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "|".join(["---"] * n_cols) + "|")
        start_row = 1
    else:
        # ヘッダ罫線なし。標準MarkdownはヘッダなしテーブルをサポートしないのでGFM相当の
        # 軽量表現として、ダミー空ヘッダを出してから始める。
        out.append("| " + " | ".join([" "] * n_cols) + " |")
        out.append("|" + "|".join(["---"] * n_cols) + "|")
        start_row = 0

    for r in range(start_row, n_rows):
        row = [cell_text(r, c) or " " for c in range(n_cols)]
        out.append("| " + " | ".join(row) + " |")

    return "\n".join(out)


# --- 公開API ------------------------------------------------------------------


def render_table_block(block: Any, cfg: TableConfig | None = None) -> str | None:
    """``<BLOCK TYPE="表組">`` に相当する Block を Markdown表に変換する。

    Args:
        block: ``.lines``, ``.x``, ``.y``, ``.w``, ``.h`` を持つ Block。
            ``.lines`` の各要素は ``.x .y .w .h .text`` を持つ Line とする。
        cfg: 閾値とオプション。``None`` で既定値を使用。

    Returns:
        Markdown断片。表組としての構造化に失敗した場合は ``None`` を返し、
        呼び出し側で代替表現（旧フラット箇条書きや画像埋め込み）に
        フォールバックする想定。
    """
    cfg = cfg or TableConfig()

    lines = [ln for ln in (block.lines or []) if (ln.text or "").strip()]
    if len(lines) < cfg.min_rows:
        return None

    # 行高中央値: 列アンカーや閾値の基準にする。縦書きラベルは異常値になるので除外。
    horiz_indices = [i for i, ln in enumerate(lines) if not _line_is_vertical(ln)]
    if not horiz_indices:
        horiz_indices = list(range(len(lines)))
    heights = [lines[i].h for i in horiz_indices if lines[i].h > 0]
    if not heights:
        return None
    median_h = statistics.median(heights)

    row_thresh = max(8.0, median_h * cfg.row_thresh_ratio)
    col_thresh = max(20.0, median_h * cfg.col_thresh_ratio)

    rows = cluster_rows(lines, row_thresh)
    if len(rows) < cfg.min_rows:
        return None

    # 列アンカー計算には全LINEを使う（縦書きラベルは中央xを基準にすることで、
    # 横書きセルとは別の列として認識される）。
    col_anchors = cluster_cols(lines, range(len(lines)), col_thresh)
    if len(col_anchors) < 2 or len(col_anchors) > cfg.max_cols:
        return None

    grid = assign_cells(lines, rows, col_anchors)

    md_table = render_markdown_table(
        grid,
        n_rows=len(rows),
        n_cols=len(col_anchors),
        lines=lines,
        newline=cfg.newline_in_cell,
        header_mode=cfg.header_mode,
    )

    head = (
        f"<!-- 表組開始 (x={block.x}, y={block.y}, w={block.w}, h={block.h}) "
        f"rows={len(rows)} cols={len(col_anchors)} -->"
    )
    return f"{head}\n\n{md_table}\n\n<!-- 表組終了 -->"
