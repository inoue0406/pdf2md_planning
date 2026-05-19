# pdf2md_planning — 行政計画文書PDFをNDLOCR-LiteでMarkdown化する

国立国会図書館の [NDLOCR-Lite](https://github.com/ndl-lab/ndlocr-lite) を使って、地域防災計画などの行政計画PDFを **Markdown** に一括変換するパイプラインです。CPUのみで動作し、外部APIに送信しません。

## できること

- 入力ディレクトリ内のすべてのPDFを順次処理
- ページごとにPNG (PyMuPDF) → NDLOCR-Lite (CLI) → Markdown化
- 章節タイトルを `## / ### / ####` に昇格、本文は段落として復元
- 表組は `<!-- 表組開始 -->` … `<!-- 表組終了 -->` で囲んだ箇条書きとして出力（後段でLLM等で表に再構築しやすい形）
- **図版はページ画像から切り出して個別PNG化**し、Markdownに `![図版](figures/…)` として埋め込む
- ページ番号・柱書き（章節名のランニングヘッダ）は自動で除去
- 中断・再開に対応（既存PNG・OCR結果があればスキップ）

## ディレクトリ構成

```
pdf2md_planning/
├── data/
│   └── disaster/                 # 入力PDF
├── output/                       # ここにMarkdownが出る
├── scripts/
│   ├── pdf_to_images.py          # PDF -> ページPNG (PyMuPDF)
│   ├── ndl_xml_to_markdown.py    # NDLOCR-Lite XML -> Markdown
│   └── run_pipeline.py           # End-to-Endランナー
├── vendor/
│   └── ndlocr-lite/              # cloneしたNDLOCR-Lite本体
├── logs/                         # 実行ログ
└── pyproject.toml                # uvプロジェクト定義
```

実行後の出力例:

```
output/disaster/<PDF stem>/
├── images/                       # ページPNG (一時、デフォルトでは実行後削除)
├── ocr/                          # NDLOCR-Liteの生出力 (.xml/.json/.txt)
├── figures/                      # 図版BLOCKを切り出したPNG
├── pages/                        # ページごとMarkdown
└── <PDF stem>.md                 # 結合済みMarkdown
```

## セットアップ

macOS / Linuxを想定。Apple Siliconで動作確認済み (macOS Sequoia, M1)。

```bash
# 1) uv (Pythonバージョン管理 + パッケージマネージャ)
brew install uv
uv python install 3.11

# 2) NDLOCR-Lite を clone してuvでツール化
git clone https://github.com/ndl-lab/ndlocr-lite vendor/ndlocr-lite
uv tool install --python 3.11 ./vendor/ndlocr-lite

# 3) PATH に通す（zshならログインスクリプトに）
export PATH="$HOME/.local/bin:$PATH"

# 4) パイプライン側の依存（pymupdf / lxml / tqdm）
uv sync
```

`ndlocr-lite --help` が表示されればOK。

> **注**: NDLOCR-Liteは Python 3.10+ が必要。`uv tool install` で隔離環境に入るため、システムのPythonには触れません。

## サンプルPDFの入手

このリポジトリには PDF を同梱していません。動作確認には、たとえば熊本市の地域防災計画ページから本編PDFを取得して `data/disaster/` に置いてください。

- [熊本市 地域防災計画](https://www.city.kumamoto.jp/hpkiji/pub/detail.aspx?c_id=5&id=1326)

任意の行政計画PDFで動作します（横組み主体の文書を想定）。

## 使い方

```bash
# data/disaster/*.pdf を全部処理
uv run python scripts/run_pipeline.py \
  --in data/disaster --out output/disaster

# 大きいPDFの一部だけ試す (1〜10ページ)
uv run python scripts/run_pipeline.py \
  --in data/disaster --out output/disaster \
  --first-page 1 --last-page 10

# 縦書き混在文書（縦中横）
uv run python scripts/run_pipeline.py \
  --in data/disaster --out output/disaster --enable-tcy

# 中間PNGを残す（OCR精度を検証したいとき）
uv run python scripts/run_pipeline.py \
  --in data/disaster --out output/disaster --keep-images

# 1つのPDFが落ちても続行
uv run python scripts/run_pipeline.py \
  --in data/disaster --out output/disaster --continue-on-error
```

### 主なオプション

| オプション | 既定値 | 説明 |
|---|---|---|
| `--in DIR` | 必須 | PDF入力ディレクトリ |
| `--out DIR` | 必須 | 出力ルート |
| `--dpi N` | 300 | PDF→画像のDPI。高くするとOCR精度↑、処理時間↑ |
| `--first-page N` / `--last-page N` | なし | ページ範囲 |
| `--enable-tcy` | off | 縦中横の認識を改善（NDLOCR-Liteのオプションをそのまま透過） |
| `--keep-images` | off | 中間PNGを削除しない |
| `--keep-running-header` | off | 章節ヘッダ等を除去せず残す |
| `--extract-figures TYPE ...` | `図版` | ページ画像から切り出すBLOCK/TYPE一覧。空指定で抽出オフ。例: `--extract-figures 図版 表組` |
| `--skip-types ...` | `ノンブル 柱 広告文字` | Markdown化時に無視するLINE TYPE |
| `--glob PATTERN` | `*.pdf` | 入力選択パターン |
| `--continue-on-error` | off | 失敗PDFを飛ばして続行 |
| `--log FILE` | `logs/pipeline.log` | ログ出力先 |

## パイプラインの中身

NDLOCR-Liteは画像入力しか受け付けないため、次のような3段構成:

```
[PDF]                           data/disaster/*.pdf
   │ PyMuPDF (fitz)             scripts/pdf_to_images.py
   ▼
[per-page PNG]                  300dpiレンダリング、ファイル名: <stem>_p0001.png
   │ NDLOCR-Lite CLI            ndlocr-lite --sourcedir ... --output ...
   ▼
[per-page XML/JSON/TXT]         <stem>_p0001.xml など
   │ scripts/extract_figures.py
   ▼
[per-figure PNG]                figures/<stem>_p####_fig##.png
   │ scripts/ndl_xml_to_markdown.py
   ▼
[per-page Markdown]             pages/<stem>_p0001.md   (![図版](../figures/...))
   │ run_pipeline.py
   ▼
[combined Markdown]             <stem>.md               (![図版](figures/...))
```

### XML→Markdown変換の方針

NDLOCR-Liteの出力XMLには、`LINE`要素ごとに `TYPE` (本文/タイトル本文/キャプション/ノンブル/柱/表組/図版/広告文字)、`ORDER` (読み順)、座標、`STRING` (テキスト) が入っている。本パイプラインでは:

1. `TEXTBLOCK` 内の `LINE.ORDER` 最小値でブロックを並べ替え
2. ブロック先頭の `LINE TYPE="タイトル本文"` を見出しとして検出し、行高で `##` `###` `####` を割り当て
3. 同一ブロック内の本文行はスペースなしで連結（英字末尾は半角スペース）
4. ページ上端6% / 下端4% の単一行ブロックは **柱書き（running header）として除外**（NDLOCRが「本文」「キャプション」と誤分類するケースを救うため位置で判定）
5. `BLOCK TYPE="表組"` は内部の `LINE` を箇条書きで出力し、`<!-- 表組開始/終了 -->` で囲む
6. `BLOCK TYPE="図版"` の領域は、対応するページPNGから **その矩形を切り出して個別PNGに保存**し、Markdownには `![図版](figures/…png)` を埋め込む。画像が無い場合のみコメントのplaceholderにフォールバック。
7. `ノンブル`（ページ番号）・`柱`・`広告文字` の LINE は既定で除外（`--skip-types` で変更可）

### スループットの目安

| 環境 | 1ページあたり |
|---|---|
| macOS Sequoia / Apple M1 / CPU | 約2〜3秒（300dpi、モデルロード込み）|

500ページ規模のPDFで15〜25分が目安です。CUDA GPU環境では `extra_args=["--device", "cuda"]` を `process_pdf` に渡せばGPUモードも使えます（onnxruntime-gpuが必要）。

### 図版抽出の挙動

- 既定では `BLOCK TYPE="図版"` の矩形をページPNGから切り出して `figures/` に保存します
- 切り出しには Pillow を使用し、矩形の外側に4pxのパディングを追加（線が切れないように）
- 同一ページに複数の図版がある場合、上から下／左から右の順に `_fig01`, `_fig02`, … と連番
- `--extract-figures` に複数のBLOCK TYPEを渡すと、たとえば `表組` も画像として保存できます: `--extract-figures 図版 表組`
- `--extract-figures` を引数なしで指定すると抽出オフ（昔の挙動: コメントplaceholderのみ）
- OCR結果は残っていて中間PNGが消されている場合、図版抽出のため対象ページだけ自動で再レンダします

## 既知の限界

- **表のレイアウト再構築は未実装**：列の対応を保てません。表組は箇条書きとして並べるだけです。本格的な構造化は別途LLMなどで後処理してください（`--extract-figures 図版 表組` で表組も画像として保存することは可能）。
- **縦書きの段組**：横組み主体の前提です。完全縦書きは `--enable-tcy` を試してください。
- **数式・特殊記号**：NDLOCR-LiteのOCR一般限界に従います（「°」「′」「″」などの単位記号がしばしば誤認識）。
- **見出しレベル**：行の高さから推定しています。文書ごとに `_heading_level()` を調整可能。

## ライセンス

- 本パイプライン: MIT
- NDLOCR-Lite: CC BY 4.0 (`vendor/ndlocr-lite/LICENCE` を参照)

## 参考

- [NDLOCR-Lite (GitHub)](https://github.com/ndl-lab/ndlocr-lite)
- [NDLOCR-Liteの使い方 / NDLラボ](https://lab.ndl.go.jp/data_set/ndlocrlite-usage/)
- [NDLOCR-Liteの公開について / NDLラボ News](https://lab.ndl.go.jp/news/2025/2026-02-24/)
