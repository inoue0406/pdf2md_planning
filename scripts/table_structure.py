"""NDLOCR-Lite の ``<BLOCK TYPE="表組">`` の中身（座標つきLINE要素群）から、
Markdownの表 (``| a | b |\n|---|---|\n| c | d |``) を再構築する。

設計（Phase 1 + 2 + 3）:

- **Phase 1**: 座標から行・列を再構築 (cluster_rows / cluster_cols / assign_cells)。
- **Phase 2**: 出力モード切替 (``TableConfig.mode``):
  ``grid`` / ``grid+image`` / ``image`` / ``list`` / ``auto``。
  ``auto`` は推定OKなら ``grid``、ダメなら ``image+details`` にフォールバック。
- **Phase 3**: 縦書き巨大ラベル(``本編`` / ``第N部…``) を、そのY範囲内に位置する
  すべての行に **複製配置** することで rowspan 的な「セクション見出し」を表現する。

このモジュールは ``Block``/``Line`` 型を ``ndl_xml_to_markdown.py`` 側から
import せず、 ``.x .y .w .h .text`` 属性を持つオブジェクトとして duck-type で
扱う（循環import回避）。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Iterable


# 表組の出力モード
MODES = ("auto", "grid", "grid+image", "image", "list")


@dataclass
class TableConfig:
    """表組の構造化変換に関する閾値・モード・オプション。"""

    mode: str = "auto"
    """auto / grid / grid+image / image / list

    - ``auto``    : grid を試み、列数超過などで破綻したら image+details にfallback
    - ``grid``    : grid のみ。破綻時は list にfallback
    - ``grid+image``: 常に grid と image を併記。grid破綻時は image+details
    - ``image``   : 画像のみ + ``<details>`` に生テキスト
    - ``list``    : 常にフラット箇条書き (旧挙動)
    """

    max_cols: int = 12
    """これより列が多いと破綻とみなす。"""

    min_rows: int = 2
    """これより行が少ないと表とみなさない。"""

    row_thresh_ratio: float = 0.6
    """行クラスタしきい値 = max(8, 行高中央値 * これ)"""

    col_thresh_ratio: float = 0.8
    """列クラスタしきい値 = max(20, 行高中央値 * これ)"""

    spanning_h_ratio: float = 3.0
    """行高中央値のN倍以上の高さを持つLINEを spanning とみなす。0以下で機能オフ。"""

    spanning_y_tolerance: float = 0.5
    """spanning配置時、行中心が cell の y範囲 ±(行高 * この比) なら覆われたとみなす。"""

    newline_in_cell: str = "<br>"
    """セル内の改行(マルチライン本文)の表現。"""

    header_mode: str = "auto"
    """auto / first-row: 1行目をヘッダにする / none: ヘッダ罫線を出さない"""

    keep_fallback_text: bool = True
    """grid破綻時の出力に ``<details>`` で生テキストを併記するか。"""


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


def _line_ref_x(ln: Any) -> float:
    """列クラスタ用のXの基準値。横書きは左端、縦書きは中央。"""
    if _line_is_vertical(ln):
        return ln.x + ln.w / 2
    return float(ln.x)


# --- 行・列のクラスタリング ---------------------------------------------------


def cluster_rows(lines: list[Any], threshold: float) -> list[list[int]]:
    """LINEのY中央値で行クラスタを作る。返り値はY昇順の「行ごとのline_idxリスト」。"""
    if not lines:
        return []
    indexed = sorted(enumerate(lines), key=lambda p: p[1].y + p[1].h / 2)
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


def cluster_cols(
    lines: list[Any],
    use_indices: Iterable[int],
    threshold: float,
) -> list[int]:
    """LINEの基準X (横書き=x_left / 縦書き=cx) を1次元クラスタリングし、
    続いてX範囲(=x_left..x_right)が重なるクラスタを再結合して、各列のアンカーX を返す。

    ヘッダ行など x_left がデータ行とずれているケースを、X範囲の重なりで救う。
    """
    indices = list(use_indices)
    if not indices:
        return []
    pairs = sorted(((_line_ref_x(lines[i]), i) for i in indices), key=lambda p: p[0])
    clusters: list[list[int]] = [[pairs[0][1]]]
    last_ref = pairs[0][0]
    for ref_x, i in pairs[1:]:
        if ref_x - last_ref <= threshold:
            clusters[-1].append(i)
        else:
            clusters.append([i])
        last_ref = ref_x

    def cluster_range(cl: list[int]) -> tuple[int, int]:
        xs_left = [lines[i].x for i in cl]
        xs_right = [lines[i].x + lines[i].w for i in cl]
        return (min(xs_left), max(xs_right))

    merged: list[list[int]] = []
    ranges: list[tuple[int, int]] = []
    indexed_clusters = sorted(clusters, key=lambda cl: cluster_range(cl)[0])
    for cl in indexed_clusters:
        xl, xr = cluster_range(cl)
        if merged and xl < ranges[-1][1]:
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
    """各LINEを最寄りの (row, col) に割り当てる。"""
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


# --- Phase 3: spanning cell の複製配置 ----------------------------------------


def distribute_spanning_labels(
    lines: list[Any],
    row_groups: list[list[int]],
    grid: dict[tuple[int, int], list[int]],
    median_h: float,
    cfg: TableConfig,
) -> int:
    """縦書き巨大ラベル(本編 / 第N部…)を、そのY範囲に位置するすべての行に複製配置する。

    既に他のテキストがあるセルは触らない（rowspan的セクションラベルは「空セル」を
    埋める形で広がるのが自然）。

    Returns:
        複製配置した spanning ラベルの個数（複製ヶ所数）。
    """
    if cfg.spanning_h_ratio <= 0 or median_h <= 0:
        return 0

    threshold_h = median_h * cfg.spanning_h_ratio
    tol = median_h * cfg.spanning_y_tolerance

    # 行ごとの中央Y
    row_y_centers: list[float] = []
    for indices in row_groups:
        if indices:
            ys = [lines[i].y + lines[i].h / 2 for i in indices]
            row_y_centers.append(sum(ys) / len(ys))
        else:
            row_y_centers.append(0.0)

    placements = 0
    # gridに変更を加えるため、snapshot のキーで反復
    for (r, c), idxs in list(grid.items()):
        for i in list(idxs):
            ln = lines[i]
            if ln.h < threshold_h:
                continue
            # このLINEはspanning。Y範囲を取り、覆う他の行に複製
            y_top = ln.y - tol
            y_bot = ln.y + ln.h + tol
            for r2 in range(len(row_groups)):
                if r2 == r:
                    continue
                ycy = row_y_centers[r2]
                if y_top <= ycy <= y_bot:
                    cell_key = (r2, c)
                    if cell_key not in grid:
                        # 空セル → spanning ラベルを参照として置く
                        grid[cell_key] = [i]
                        placements += 1
    return placements


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
        out.append("| " + " | ".join([" "] * n_cols) + " |")
        out.append("|" + "|".join(["---"] * n_cols) + "|")
        start_row = 0
    for r in range(start_row, n_rows):
        row = [cell_text(r, c) or " " for c in range(n_cols)]
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


# --- 内部: grid生成・フラット箇条書き ----------------------------------------


def _attempt_grid(block: Any, cfg: TableConfig) -> tuple[str | None, int, int, int]:
    """gridを推定して Markdown表テキストを返す。

    Returns:
        (md_table or None, n_rows, n_cols, n_spanning_placements).
        破綻時は (None, 0, 0, 0)。
    """
    lines = [ln for ln in (block.lines or []) if (ln.text or "").strip()]
    if len(lines) < cfg.min_rows:
        return None, 0, 0, 0

    horiz_indices = [i for i, ln in enumerate(lines) if not _line_is_vertical(ln)]
    if not horiz_indices:
        horiz_indices = list(range(len(lines)))
    heights = [lines[i].h for i in horiz_indices if lines[i].h > 0]
    if not heights:
        return None, 0, 0, 0
    median_h = statistics.median(heights)

    row_thresh = max(8.0, median_h * cfg.row_thresh_ratio)
    col_thresh = max(20.0, median_h * cfg.col_thresh_ratio)

    rows = cluster_rows(lines, row_thresh)
    if len(rows) < cfg.min_rows:
        return None, 0, 0, 0

    col_anchors = cluster_cols(lines, range(len(lines)), col_thresh)
    if len(col_anchors) < 2 or len(col_anchors) > cfg.max_cols:
        return None, 0, 0, 0

    grid = assign_cells(lines, rows, col_anchors)
    n_span = distribute_spanning_labels(lines, rows, grid, median_h, cfg)

    md = render_markdown_table(
        grid,
        n_rows=len(rows),
        n_cols=len(col_anchors),
        lines=lines,
        newline=cfg.newline_in_cell,
        header_mode=cfg.header_mode,
    )
    return md, len(rows), len(col_anchors), n_span


def _flat_list_markdown(lines: list[Any]) -> str:
    cells = [ln.text for ln in lines if (ln.text or "").strip()]
    if not cells:
        return ""
    return "\n".join(f"- {c}" for c in cells)


# --- 公開API ------------------------------------------------------------------


def render_table_block(
    block: Any,
    cfg: TableConfig | None = None,
    image_rel: str | None = None,
) -> str:
    """``<BLOCK TYPE="表組">`` に相当する Block を Markdown断片に変換する。

    ``cfg.mode`` に応じて grid / grid+image / image / list / auto を出し分ける。
    呼び出し側は **常に Markdown 文字列** を受け取れる（None は返さない）。
    """
    cfg = cfg or TableConfig()
    mode = cfg.mode if cfg.mode in MODES else "auto"

    lines = [ln for ln in (block.lines or []) if (ln.text or "").strip()]
    block_pos = (
        f"x={block.x}, y={block.y}, w={block.w}, h={block.h}"
    )
    if not lines:
        return f"<!-- 表組 ({block_pos}) -->"

    flat_body = _flat_list_markdown(lines)

    def _flat_block(tag: str) -> str:
        head = f"<!-- 表組開始 ({block_pos}) {tag} -->"
        return f"{head}\n{flat_body}\n<!-- 表組終了 -->"

    def _details_text() -> str:
        if not cfg.keep_fallback_text or not flat_body:
            return ""
        return (
            "<details><summary>OCR生テキスト (構造未復元)</summary>\n\n"
            f"{flat_body}\n\n"
            "</details>"
        )

    def _image_md() -> str:
        if image_rel:
            return f"![表組]({image_rel})"
        return ""

    def _fallback_with_image() -> str:
        # image + details (or list if details disabled)
        parts: list[str] = []
        img = _image_md()
        if img:
            parts.append(img)
        det = _details_text()
        if det:
            parts.append(det)
        elif not img:
            # 画像も詳細も無いなら、最後の手段としてフラット箇条書き
            parts.append(flat_body)
        head = f"<!-- 表組開始 ({block_pos}) fallback=image+details -->"
        body = "\n\n".join(parts)
        return f"{head}\n\n{body}\n\n<!-- 表組終了 -->"

    # mode=list は無条件にフラット箇条書き
    if mode == "list":
        return _flat_block("mode=list")

    # mode=image は無条件に 画像+詳細
    if mode == "image":
        return _fallback_with_image()

    md_grid, n_rows, n_cols, n_span = _attempt_grid(block, cfg)
    grid_meta = (
        f"rows={n_rows} cols={n_cols}" + (f" spans={n_span}" if n_span else "")
    )

    if mode == "grid":
        if md_grid is None:
            return _flat_block("fallback=list")
        head = f"<!-- 表組開始 ({block_pos}) {grid_meta} -->"
        return f"{head}\n\n{md_grid}\n\n<!-- 表組終了 -->"

    if mode == "grid+image":
        if md_grid is None:
            return _fallback_with_image()
        head = f"<!-- 表組開始 ({block_pos}) {grid_meta} -->"
        body = md_grid
        img = _image_md()
        if img:
            body = f"{body}\n\n{img}"
        return f"{head}\n\n{body}\n\n<!-- 表組終了 -->"

    # mode == "auto"
    if md_grid is not None:
        head = f"<!-- 表組開始 ({block_pos}) {grid_meta} -->"
        return f"{head}\n\n{md_grid}\n\n<!-- 表組終了 -->"
    return _fallback_with_image()
