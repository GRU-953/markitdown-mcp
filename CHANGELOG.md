# Changelog

All notable changes to **markitdown-attachments** — a token-free MCP server that
converts Claude chat/project attachments to Markdown **locally**, writing `.md`
files to disk and returning only metadata. Built on
[microsoft/markitdown](https://github.com/microsoft/markitdown).

## v2.7.0
- **Apple M-series optimization** — parallel conversion is sized to the *performance*
  cores with each worker's native libraries pinned to a single thread (on M4: 6 workers
  match 10-worker throughput at ~600 MB less memory; 3.3× faster than sequential);
  **Whisper transcription runs on the Apple GPU via MLX** (`mlx-whisper`, with a
  `faster-whisper` CPU fallback); worker count is capped against unified memory.
- **Auto-updating engine** — the underlying MarkItDown is kept current from upstream
  (microsoft/markitdown) automatically in the background (checked once/day, applied on
  next launch; opt out with `MARKITDOWN_AUTO_UPDATE=off`); `install.sh` also pulls latest.
- `ocr_capabilities` now reports hardware tuning, engine selection, and auto-update state.

## v2.6.2
- **XLSX header cleanup** — pandas' `Unnamed: N` placeholders for blank-header
  columns (e.g. when a title row is mis-detected as the header) are now stripped
  from spreadsheet output. A comprehensive fidelity audit (XLSX/DOCX/PPTX vs.
  openpyxl/python-docx/python-pptx ground truth) otherwise confirmed faithful
  conversion across all formats — no data loss, all sheets/slides/tables present.

## v2.6.1
- **Fixed RTF** — `.rtf` files previously passed raw `\rtf` control codes through as
  fake "content"; they're now extracted to plain text locally via `striprtf`.
  (Found by a maintenance audit of advertised vs. actually-supported formats:
  legacy `.doc`/`.ppt` correctly report unsupported; `.xls`, `.rss`/`.atom`/`.tsv`,
  and image variants `.bmp/.gif/.tiff/.webp` all confirmed working.)

## v2.6.0
- **Clean PDF tables** — digital PDFs are reconstructed via pdfplumber so tables
  render as proper markdown tables (non-table text preserved, no duplication);
  `pdf_tables` (`auto`/`off`/`force`). Equivalent text and comparable speed to before;
  scanned PDFs still fall back to OCR.

## v2.5.0
- **Local audio transcription** via open-source **Whisper** (`faster-whisper`,
  CPU int8) — replaces markitdown's *cloud* Google Web Speech path, so audio
  (`.wav/.mp3/.m4a/.flac`) is transcribed entirely on-device. `transcribe`
  (`auto`/`off`), `whisper_model` (default `base`). No-speech audio degrades to a
  clean note instead of failing.
- Format coverage verified clean: `.ipynb`, HTML tables, UTF-16/Latin-1 CSV, JSON, XML.

## v2.4.0
- **On-demand Ollama lifecycle**: the local vision model starts when a conversion
  needs it and a watchdog stops the instance it started after `OLLAMA_IDLE_TIMEOUT`
  (default 300 s) idle — nothing runs in the background between jobs. Only ever
  stops an instance it launched.

## v2.3.0
- **Parallel batch conversion** via a process pool (true multi-core; ~3.8× on a
  10-core machine). Collision-safe (targets assigned single-threaded first).
- **Local open-source vision LLM** (via Ollama, default `moondream`) describes
  images OCR can't read. `vision` (`auto`/`off`/`force`). Local, no tokens.

## v2.2.0
- OCR of images **embedded inside** Office files (DOCX/PPTX/XLSX) under
  `ocr=force`/`hybrid`.

## v2.1.1
- Honest **failed vs empty** taxonomy — files that genuinely error (corrupt,
  truncated, 0-byte, unreadable) are reported as `failed`, not `empty`. Hardened
  against corrupt/malformed/unreadable inputs (a bad file never crashes a batch).

## v2.1.0
- Existing `.md`/`.markdown` inputs are **carried through** into output sweeps.
- **Hybrid PDF OCR** (per-page: keep text pages, OCR only image pages).
- `detail="summary"` for compact results on large sweeps.

## v2.0.1
- Self-heal `PATH` so `ffmpeg`/`tesseract` resolve even under a sparse host environment.

## v2.0.0
- **OCR** (Tesseract, 163 languages, automatic orientation) for images and
  scanned/image-only PDFs.
- Google Drive pointer files (`.gdoc/.gslides/.gsheet/.gdrive`) → "Open in Drive"
  link notes (no raw JSON, no embedded email).
- Collision-safe output names; structure-preserving output; rich token-free summary.

## v1.0.0
- Initial token-free MCP server + Claude Desktop (`.mcpb`) extension: convert
  attachments to `.md` on disk, return only metadata (paths/sizes/status).
