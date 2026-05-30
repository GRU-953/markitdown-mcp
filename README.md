<div align="center">

# MarkItDown Attachments

**A token-free MCP server that converts your Claude chat & project attachments to Markdown — entirely on your machine.**

[![CI](https://github.com/GRU-953/markitdown-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/GRU-953/markitdown-mcp/actions/workflows/ci.yml)
&nbsp;![Python 3.10–3.13](https://img.shields.io/badge/python-3.10–3.13-blue)
&nbsp;![Platform](https://img.shields.io/badge/platform-macOS%20·%20Linux%20·%20Windows-lightgrey)
&nbsp;![License: MIT](https://img.shields.io/badge/license-MIT-green)
&nbsp;![Apple Silicon optimized](https://img.shields.io/badge/Apple%20Silicon-optimized-black?logo=apple)

</div>

---

PDF, Word, Excel, PowerPoint, images, audio, HTML and more → clean Markdown **written to disk**, with only compact metadata returned to the model. Converting a whole folder of attachments costs **effectively zero context tokens**.

> **Measured:** a real 76-file / 94 MB folder converts in one call returning **~25 KB** of metadata while writing **1.75 M characters** of Markdown to disk — **≈70× more content than tokens returned.**

## Why it's different

Everything runs **locally and privately** — no cloud APIs, no keys, no tokens:

| | |
|---|---|
| 🪙 **Token-free** | Conversions write `.md` files to disk; tools return only paths/sizes/status. |
| 🔤 **OCR, 163 languages** | Images & scanned PDFs via Tesseract, automatic orientation, per-page **hybrid** mode, and OCR of images **embedded inside** Office files. |
| 👁️ **Local vision LLM** | Images OCR can't read are *described* by a local model (Ollama), started on demand and idle-stopped. |
| 🎙️ **Local transcription** | Audio transcribed on-device with **Whisper** (no cloud). |
| 📊 **Clean tables** | Digital-PDF tables reconstructed as real Markdown tables via pdfplumber. |
| 🍎 **Apple-Silicon tuned** | Performance-core parallelism, **GPU-accelerated Whisper (MLX)**, unified-memory-aware concurrency. |
| ♻️ **Always current** | The underlying MarkItDown engine auto-updates from upstream. |
| 🧱 **Robust** | Collision-safe names, structure-preserving output, honest `failed`/`empty` reporting — a bad file never crashes a batch. |

## Apple M-series optimization

On Apple Silicon the server tunes itself to the hardware:

- **CPU** — parallel conversion across a process pool sized to the **performance cores** (not all cores), with each worker's native libraries pinned to a single thread to avoid oversubscription. *On an M4: 6 workers match 10 workers' throughput while using ~600 MB less memory; 3.3× faster than sequential.*
- **GPU** — Whisper transcription runs on the Apple GPU/Neural Engine via **MLX** (`mlx-whisper`), with a CPU `faster-whisper` fallback. Vision (Ollama) uses Metal.
- **Memory** — worker count is capped against unified memory, and the vision model is started on demand and stopped after idle.

## Tools

| Tool | Purpose |
|------|---------|
| `convert_attachments_to_markdown` | Batch-convert files / directories / globs (parallel) |
| `list_convertible_attachments` | Enumerate convertible files (paths/sizes only) |
| `convert_one` | Convert a single file |
| `peek_markdown` | Opt-in small preview of a generated `.md` |
| `ocr_capabilities` | Report local OCR / vision / transcription + hardware tuning |

## Install

```bash
git clone https://github.com/GRU-953/markitdown-mcp.git
cd markitdown-mcp
./install.sh                 # Python venv + dependencies (Python 3.10–3.13; 3.12 recommended)

# Optional local engines (recommended):
brew install tesseract tesseract-lang ffmpeg        # OCR (163 languages) + audio
brew install ollama && ollama pull moondream        # local vision model
```

### Claude Code
Add to your project's `.mcp.json` (absolute paths), then reload and approve:

```json
{ "mcpServers": { "markitdown-attachments": {
  "command": "/abs/path/markitdown-mcp/.venv/bin/python",
  "args": ["/abs/path/markitdown-mcp/server/markitdown_attachments_server.py"]
}}}
```

### Claude Desktop
Install `dist/markitdown-attachments.mcpb` via **Settings → Extensions → Install from file…**, run `install.sh` in the installed folder, then restart.

## Usage

> *"Convert every attachment in `~/Documents/contracts` to markdown."*

Claude calls `convert_attachments_to_markdown(input_dir="~/Documents/contracts")`, gets back the list of generated `.md` paths — no document text — and reads only the ones it needs.

## Configuration

| Setting / env | Meaning |
|---|---|
| `ocr` · `MARKITDOWN_OCR` | `auto` (default) · `off` · `force` · `hybrid` |
| `ocr_lang` · `MARKITDOWN_OCR_LANG` | e.g. `eng` or `eng+ben` |
| `vision` · `MARKITDOWN_VISION` | `auto` (default) · `off` · `force` (local Ollama) |
| `pdf_tables` · `MARKITDOWN_PDF_TABLES` | `auto` (default) · `off` · `force` |
| `transcribe` · `MARKITDOWN_TRANSCRIBE` | `auto` (default) · `off`; `whisper_model` (default `base`) |
| `workers` · `MARKITDOWN_WORKERS` | blank = auto (performance-core + memory tuned) |
| `MARKITDOWN_AUTO_UPDATE` | keep MarkItDown current from upstream (default on) |
| `detail` | `full` (default) · `summary` (compact result on huge sweeps) |

## Testing

```bash
.venv/bin/python tests/test_harness.py full /path/to/your/files
```

Drives the server over the real MCP protocol: every tool, OCR, vision, transcription, PDF tables, collision-safety, idempotency, accuracy spot-checks, and the token-free guarantee. Continuous integration runs the self-test on every push.

## Built with

Open-source tooling: [MarkItDown](https://github.com/microsoft/markitdown) (conversion engine), [Tesseract](https://github.com/tesseract-ocr/tesseract) (OCR), [Ollama](https://ollama.com) (local vision), [Whisper](https://github.com/openai/whisper) / [MLX](https://github.com/ml-explore/mlx) (transcription), and [pdfplumber](https://github.com/jsvine/pdfplumber) (PDF tables).

## Author

**Aninda Sundar Howlader** — [@GRU-953](https://github.com/GRU-953)

MIT-licensed. See [CHANGELOG.md](CHANGELOG.md).
