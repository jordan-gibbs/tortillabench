#!/usr/bin/env python3
"""TortillaBench helper. Stdlib only.

Each run happens in its own workspace outside the repo, so a model can't see
other models' results. Afterwards, collect it into results/ for the gallery
and for grading.

    python tortilla.py new <model>                      make an isolated workspace
    python tortilla.py collect <workspace> [--model M] [--harness H] [--final IMG]
    python tortilla.py gallery                          rebuild the README gallery

Workspaces go in ~/tortillabench-runs (override with TORTILLABENCH_RUNS).
Set BLENDER=/path/to/blender if Blender isn't found (used for preview images).
"""

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
README = ROOT / "README.md"
RUNS = Path(os.environ.get("TORTILLABENCH_RUNS", Path.home() / "tortillabench-runs"))
IMAGE_EXTS = {".png", ".exr", ".jpg", ".jpeg", ".tif", ".tiff"}
SKIP_DIRS = {"__pycache__", ".git", ".venv", ".idea"}
SKIP_EXTS = {".blend1", ".blend2", ".pyc"}
PREVIEW = "preview.jpg"
GALLERY_START = "<!-- gallery:start -->"
GALLERY_END = "<!-- gallery:end -->"


def die(msg, code=1):
    print(f"tortilla: {msg}", file=sys.stderr)
    sys.exit(code)


def slugify(s):
    return re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-")


def unique_dir(base):
    path, n = base, 2
    while path.exists():
        path = base.with_name(f"{base.name}-{n}")
        n += 1
    return path


def find_blender():
    env = os.environ.get("BLENDER")
    if env:
        return env
    on_path = shutil.which("blender")
    if on_path:
        return on_path
    if sys.platform == "win32":
        candidates = glob.glob(r"C:\Program Files\Blender Foundation\Blender *\blender.exe")
    elif sys.platform == "darwin":
        candidates = glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender")
    else:
        candidates = glob.glob("/opt/blender*/blender") + glob.glob("/snap/bin/blender")

    def version_key(p):
        m = re.search(r"(\d+)\.(\d+)", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0)

    return max(candidates, key=version_key) if candidates else None


def make_preview(src, dest, width=1280):
    """Downscaled JPG for the README, rendered by Blender since stdlib has no imaging."""
    blender = find_blender()
    if not blender:
        print("tortilla: Blender not found, skipping preview")
        return False
    expr = (
        "import bpy\n"
        f"img = bpy.data.images.load({str(src)!r})\n"
        "w, h = img.size\n"
        f"img.scale({width}, max(1, round(h * {width} / w)))\n"
        f"img.filepath_raw = {str(dest)!r}\n"
        "img.file_format = 'JPEG'\n"
        "img.save()\n"
    )
    subprocess.run([blender, "--background", "--factory-startup", "--python-expr", expr],
                   capture_output=True, timeout=300)
    return dest.exists()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- commands --------------------------------------------------------------

def cmd_new(args):
    ws = unique_dir(RUNS / f"{slugify(args.model)}-{dt.date.today().isoformat()}")
    ws.mkdir(parents=True)
    shutil.copy2(ROOT / "TASK.md", ws / "TASK.md")
    print(ws)
    print("\nStart your agent in that folder and give it the prompt from README.md.")
    print(f"When it's done:  python tortilla.py collect \"{ws}\" --model \"{args.model}\"")


def cmd_collect(args):
    ws = Path(args.workspace).resolve()
    if not ws.is_dir():
        die(f"{ws} is not a folder")
    if RESULTS in ws.parents or ws == ROOT:
        die("workspace must be outside the repo")

    files = [p for p in ws.rglob("*") if p.is_file()
             and not SKIP_DIRS.intersection(p.relative_to(ws).parts)
             and p.suffix.lower() not in SKIP_EXTS
             and p.name != "TASK.md"]

    # Scripts often save a .blend per iteration; keep only the newest.
    blends = sorted((p for p in files if p.suffix.lower() == ".blend"), key=lambda p: p.stat().st_mtime)
    files = [p for p in files if p.suffix.lower() != ".blend"] + blends[-1:]

    images = [p for p in files if p.suffix.lower() in IMAGE_EXTS]
    if args.final:
        final = (ws / args.final).resolve()
        if not final.exists():
            die(f"{final} not found")
    elif images:
        # Guess: the newest full-size image.
        big = [p for p in images if p.stat().st_size > 1_000_000] or images
        final = max(big, key=lambda p: p.stat().st_mtime)
    else:
        final = None

    model = args.model or re.sub(r"-\d{4}-\d{2}-\d{2}(-\d+)?$", "", ws.name)
    date = dt.date.fromtimestamp(max(p.stat().st_mtime for p in files)).isoformat() if files else dt.date.today().isoformat()
    dest = unique_dir(RESULTS / slugify(model) / date)
    dest.mkdir(parents=True)

    # Copy, skipping byte-identical duplicates (e.g. the same render saved twice).
    seen, total, copied = {}, 0, []
    for p in sorted(files, key=lambda p: (p != final, len(p.parts), str(p))):
        digest = sha256(p)
        if digest in seen:
            continue
        rel = p.relative_to(ws)
        seen[digest] = rel.as_posix()
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest / rel)
        total += p.stat().st_size
        copied.append(rel.as_posix())
        if p.stat().st_size > 50_000_000:
            print(f"tortilla: warning: {rel} is {p.stat().st_size // 1_000_000} MB (GitHub limit is 100 MB)")

    has_preview = bool(final) and make_preview(final, dest / PREVIEW)
    meta = {
        "model": model,
        "harness": args.harness,
        "date": date,
        "final_image": final.relative_to(ws).as_posix() if final else None,
        "preview": PREVIEW if has_preview else None,
        "files": len(copied),
        "notes": args.notes,
        "grade": None,
    }
    (dest / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    print(f"tortilla: collected {len(copied)} files ({total // 1_000_000} MB) -> {dest.relative_to(ROOT).as_posix()}")
    print(f"tortilla: final image: {meta['final_image']}  (override with --final)")
    if len(files) > len(copied):
        print(f"tortilla: skipped {len(files) - len(copied)} duplicate file(s)")


def cmd_gallery(args):
    rows = []
    for meta_path in sorted(RESULTS.glob("*/*/meta.json")):
        run_dir = meta_path.parent
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        rel = run_dir.relative_to(ROOT).as_posix()
        shown = meta.get("preview") or meta.get("final_image")
        img = f'<img src="{rel}/{shown}" width="360">' if shown else "_no image_"
        grade = meta.get("grade")
        score = grade.get("overall", "") if isinstance(grade, dict) else "not graded"
        rows.append(f"| {img} | **{meta['model']}**<br>{meta.get('harness') or ''} | "
                    f"{meta.get('date', '')} | {score} | [files]({rel}) |")
    table = ["| Final render | Model | Date | Grade | Run |", "|---|---|---|---|---|"]
    body = "\n".join(table + rows) if rows else "_No results yet — be the first tortilla._"

    text = README.read_text(encoding="utf-8")
    start, end = text.find(GALLERY_START), text.find(GALLERY_END)
    if start == -1 or end == -1:
        die("README.md is missing the gallery markers")
    text = text[:start + len(GALLERY_START)] + "\n" + body + "\n" + text[end:]
    README.write_text(text, encoding="utf-8")
    print(f"tortilla: gallery updated with {len(rows)} run(s)")


def main():
    p = argparse.ArgumentParser(description="TortillaBench helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("new", help="create an isolated workspace for a run")
    s.add_argument("model")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("collect", help="copy a finished workspace into results/")
    s.add_argument("workspace")
    s.add_argument("--model", help="model name (default: from the workspace folder name)")
    s.add_argument("--harness", default="", help="agent used, e.g. claude-code, codex")
    s.add_argument("--final", help="final image, relative to the workspace (default: newest render)")
    s.add_argument("--notes", default="")
    s.set_defaults(fn=cmd_collect)

    s = sub.add_parser("gallery", help="rebuild the README gallery from results/")
    s.set_defaults(fn=cmd_gallery)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
