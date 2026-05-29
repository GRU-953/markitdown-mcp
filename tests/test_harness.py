#!/usr/bin/env python3
"""Comprehensive MCP test harness for the markitdown-attachments server.

Drives the server over the real MCP stdio protocol (exactly like Claude does)
and exercises every tool + edge cases + v2 features (OCR, drive-link pointers,
collision-safe names, empty tracking) against a real corpus of files.

Usage:
  ../.venv/bin/python tests/test_harness.py [full|quick] <CORPUS_DIR>

Paths to the venv/server are derived relative to this file, so it works from a
clone. Exit 0 = all assertions passed; non-zero = issues found.
"""
import asyncio
import json
import os
import sys
import time
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PLUG = Path(__file__).resolve().parent.parent          # repo root
VENV_PY = str(PLUG / ".venv" / "bin" / "python")
SERVER = str(PLUG / "server" / "markitdown_attachments_server.py")

MODE = sys.argv[1] if len(sys.argv) > 1 else "full"
if len(sys.argv) > 2:
    CORPUS = sys.argv[2]
else:
    print("usage: test_harness.py [full|quick] <CORPUS_DIR>")
    raise SystemExit(2)

CONVERTIBLE_EXTS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".xlsm",
    ".html", ".htm", ".csv", ".tsv", ".json", ".xml", ".rss", ".atom",
    ".epub", ".zip", ".msg", ".txt", ".rtf", ".ipynb",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".wav", ".mp3", ".m4a", ".flac",
}
POINTER_EXTS = {".gdoc", ".gslides", ".gsheet", ".gdrive"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"}

ISSUES, WARNINGS = [], []
def issue(m): ISSUES.append(m); print("   ✗ ISSUE:", m)
def warn(m):  WARNINGS.append(m); print("   ! warn:", m)
def ok(m):    print("   ✓", m)


def ground_truth(corpus):
    conv, ptr = [], []
    for dp, _dns, fns in os.walk(corpus):
        for fn in fns:
            if fn == ".DS_Store":
                continue
            p = Path(dp) / fn
            ext = p.suffix.lower()
            if ext in CONVERTIBLE_EXTS:
                conv.append(p)
            elif ext in POINTER_EXTS:
                ptr.append(p)
    return conv, ptr


async def call(session, name, args):
    res = await session.call_tool(name, args)
    raw = res.content[0].text if res.content else ""
    try:
        obj = json.loads(raw)
    except Exception:
        obj = None
    return obj, raw, bool(res.isError)


async def main():
    print(f"\n{'='*70}\nMARKITDOWN-ATTACHMENTS HARNESS (mode={MODE})\ncorpus={CORPUS}\n{'='*70}")
    conv, ptr = ground_truth(CORPUS)
    print(f"Ground truth: {len(conv)} convertible, {len(ptr)} pointer-stubs")
    OUT = Path(tempfile.mkdtemp(prefix="harness_out_"))
    params = StdioServerParameters(command=VENV_PY, args=[SERVER], env=os.environ.copy())
    results = []

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print(f"server: {init.serverInfo.name}")
            tools = {t.name for t in (await s.list_tools()).tools}
            for t in ["convert_attachments_to_markdown", "list_convertible_attachments",
                      "convert_one", "peek_markdown", "ocr_capabilities"]:
                (ok if t in tools else issue)(f"tool present: {t}")
            cap, _, _ = await call(s, "ocr_capabilities", {})
            print(f"   OCR available={cap.get('available')} langs={cap.get('languages')}")

            print("\n[1] list_convertible_attachments(recursive=True)")
            obj, raw, err = await call(s, "list_convertible_attachments", {"input_dir": CORPUS, "recursive": True})
            if err or obj is None:
                issue(f"list errored: {raw[:200]}")
            else:
                listed = {Path(f["path"]).resolve() for f in obj["files"]}
                gt = {p.resolve() for p in conv}
                ok(f"listed {len(listed)} convertible (gt {len(gt)}); pointers {obj.get('pointer_count')}")
                if listed - gt: issue(f"unexpected: {[p.name for p in list(listed-gt)[:5]]}")
                if gt - listed: issue(f"missed: {[p.name for p in list(gt-listed)[:5]]}")
                (ok if obj.get("pointer_count") == len(ptr) else issue)("pointer count matches")

            print(f"\n[2] convert_one over {len(conv)} files")
            files = conv if MODE == "full" else conv[:8]
            for i, p in enumerate(sorted(files), 1):
                rel = p.relative_to(CORPUS)
                outp = OUT / "perfile" / (str(rel).replace("/", "__") + ".md")
                t = time.time()
                obj, raw, err = await call(s, "convert_one", {"source": str(p), "output_path": str(outp)})
                dt = time.time() - t
                rec = {"file": str(rel), "ext": p.suffix.lower(), "secs": dt,
                       "ok": bool(obj and obj.get("ok")), "chars": (obj or {}).get("chars", 0),
                       "ocr": (obj or {}).get("ocr", False), "err": (obj or {}).get("error"),
                       "raw_len": len(raw)}
                results.append(rec)
                tag = "OCR " if rec["ocr"] else "    "
                print(f"   [{i:2d}/{len(files)}] {'ok' if rec['ok'] else 'ERR'} {tag}{dt:5.1f}s {rec['chars']:8d}ch  {str(rel)[:54]}")
                if obj and rec["raw_len"] > 4000:
                    issue(f"convert_one result too large for {rel}")
            for r in [r for r in results if not r["ok"]]:
                issue(f"convert FAILED: {r['file']} -> {r['err']}")
            on_disk = list((OUT / "perfile").rglob("*.md"))
            ok(f"{len(on_disk)} .md written; OCR used on {len([r for r in results if r['ocr']])} files")

            print("\n[3] batch convert + collision-safe names + INDEX")
            obj, raw, err = await call(s, "convert_attachments_to_markdown",
                                       {"input_dir": CORPUS, "output_dir": str(OUT/"batch"),
                                        "recursive": True, "write_index": True})
            if err or obj is None:
                issue(f"batch errored: {raw[:200]}")
            else:
                outs = [c["markdown_file"] for c in obj["converted"]]
                ok(f"batch: {obj['summary']}")
                if obj.get("failed"): issue(f"batch failures: {obj['failed']}")
                (ok if len(set(outs)) == len(outs) else issue)("no colliding output paths")
                (ok if "totals" in obj else issue)("result carries totals")
                (ok if obj.get("index_file") and Path(obj["index_file"]).exists() else issue)("INDEX.md written")
                big = len(raw); content = obj["totals"]["total_chars"]
                ok(f"token-free: result {big}B vs {content} content chars ({content//max(big,1)}x)")

            print("\n[4] edge cases")
            obj, _, _ = await call(s, "convert_attachments_to_markdown", {"sources": ["/no/such.pdf"]})
            (ok if obj and obj.get("failed") else issue)("missing file -> failed")
            d2 = OUT / "ow"
            await call(s, "convert_attachments_to_markdown", {"sources": [f"{CORPUS}/*"], "output_dir": str(d2)})
            o2, _, _ = await call(s, "convert_attachments_to_markdown", {"sources": [f"{CORPUS}/*"], "output_dir": str(d2)})
            (ok if (o2 and o2.get("skipped") and not o2.get("converted")) else issue)("idempotent (2nd run skips)")
            if on_disk:
                obj, _, _ = await call(s, "peek_markdown", {"markdown_file": str(on_disk[0]), "max_chars": 120})
                (ok if (obj and obj.get("ok") and len(obj.get("preview","")) <= 120) else issue)("peek capped")

            print("\n[6] v2 features")
            imgs = [p for p in conv if p.suffix.lower() in IMAGE_EXTS]
            if imgs:
                obj, _, _ = await call(s, "convert_one", {"source": str(imgs[0]), "output_path": str(OUT/"i.md"), "ocr": "force"})
                print(f"   image OCR({imgs[0].name[:30]}) -> {obj.get('chars') if obj else '?'} chars ocr={obj.get('ocr') if obj else '?'}")
            if ptr:
                obj, raw, _ = await call(s, "convert_one", {"source": str(ptr[0]), "output_path": str(OUT/"p.md")})
                md = (OUT/"p.md").read_text() if (OUT/"p.md").exists() else ""
                (ok if obj and obj.get("method") == "drive-link" and "google.com" in md else issue)("pointer -> drive-link note")
            # hybrid OCR on a pdf
            pdfs = [p for p in conv if p.suffix.lower() == ".pdf"]
            if pdfs:
                o, _, _ = await call(s, "convert_one", {"source": str(pdfs[0]), "output_path": str(OUT/"hy.md"), "ocr": "hybrid"})
                (ok if (o and o.get("ok") and ("hybrid" in (o.get("method") or "") or o.get("method") == "pdf-text")) else issue)(
                    f"hybrid pdf -> method={o.get('method') if o else '?'}")
            # detail=summary returns a compact result (no big converted[] array)
            o, raw, _ = await call(s, "convert_attachments_to_markdown",
                                   {"input_dir": CORPUS, "output_dir": str(OUT/"sum"), "detail": "summary", "overwrite": True})
            (ok if (o and "converted" not in o and "totals" in o) else issue)(f"detail=summary compact ({len(raw)}B, converted[] omitted)")
            # markdown carry-through (only assert when corpus contains .md/.markdown)
            mds = [1 for dp, _, fns in os.walk(CORPUS) for fn in fns if fn.lower().endswith((".md", ".markdown"))]
            if mds:
                o, _, _ = await call(s, "convert_attachments_to_markdown",
                                     {"input_dir": CORPUS, "output_dir": str(OUT/"mdc"), "overwrite": True})
                (ok if (o and o["totals"].get("markdown_copied", 0) > 0) else issue)(
                    f"markdown carry-through ({o['totals'].get('markdown_copied') if o else '?'} copied of {len(mds)} .md)")

    print(f"\n{'='*70}\nREPORT  |  ISSUES: {len(ISSUES)}  WARNINGS: {len(WARNINGS)}")
    for m in ISSUES: print("  ✗", m)
    print(f"VERDICT: {'PASS ✅' if not ISSUES else 'FAIL ❌'}   (output: {OUT})")
    return 0 if not ISSUES else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
