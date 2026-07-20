#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create the PDFToolkit operation manual video from real application screens."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tkinter import ttk


ROOT = Path(__file__).resolve().parent
MANUAL_DIR = ROOT / "manual_video"
SCRIPT_PATH = MANUAL_DIR / "script.md"
STORYBOARD_PATH = MANUAL_DIR / "storyboard.md"
OUTPUT_DIR = MANUAL_DIR / "output"
OUTPUT_VIDEO = OUTPUT_DIR / "manual.mp4"
DEPS_DIR = ROOT / ".manual_video_deps"
SAMPLE_DIR = ROOT / "範例"
FRAMES_DIR = MANUAL_DIR / "_frames"
AUDIO_DIR = MANUAL_DIR / "_audio"
WIDTH = 1920
HEIGHT = 1080
SUBTITLE_H = 132
FPS = 30
VOICE = "zh-TW-HsiaoChenNeural"


SCENE_PLAN = [
    ("01", "opening", 5.5),
    ("02", "viewer_open", 7.0),
    ("03", "viewer_tools", 7.0),
    ("04", "viewer_search", 8.0),
    ("05", "splitter_setup", 6.5),
    ("06", "splitter_detect", 7.0),
    ("07", "splitter_preview", 7.0),
    ("08", "replace_setup", 7.0),
    ("09", "replace_scan", 7.0),
    ("10", "replace_compare", 8.0),
    ("11", "replace_finish", 6.0),
    ("12", "signature_setup", 7.0),
    ("13", "signature_rule", 7.0),
    ("14", "signature_preview", 8.0),
]


def add_local_deps() -> None:
    if DEPS_DIR.exists():
        sys.path.insert(0, str(DEPS_DIR))


def require_modules():
    add_local_deps()
    missing = []
    try:
        import fitz  # type: ignore
    except Exception:
        missing.append("PyMuPDF")
        fitz = None
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except Exception:
        missing.append("Pillow")
        Image = ImageDraw = ImageFont = None
    if missing:
        raise RuntimeError(
            "缺少影片製作套件："
            + ", ".join(missing)
            + "\n請先執行：python -m pip install --target .manual_video_deps PyMuPDF Pillow edge-tts"
        )
    return fitz, Image, ImageDraw, ImageFont


def find_ffmpeg() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    ffprobe = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
    if not Path(ffmpeg).exists():
        raise RuntimeError("找不到 ffmpeg，請確認 C:\\ffmpeg\\bin\\ffmpeg.exe 是否存在。")
    if not Path(ffprobe).exists():
        raise RuntimeError("找不到 ffprobe，請確認 C:\\ffmpeg\\bin\\ffprobe.exe 是否存在。")
    return ffmpeg, ffprobe


def read_script() -> dict[str, str]:
    if not SCRIPT_PATH.exists():
        raise RuntimeError(f"找不到旁白稿：{SCRIPT_PATH}")
    sections: dict[str, list[str]] = {}
    current = None
    for raw in SCRIPT_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            current = line[3:].split()[0]
            sections[current] = []
        elif current and line:
            sections[current].append(line)
    return {key: "\n".join(value) for key, value in sections.items()}


def create_samples(fitz) -> dict[str, Path]:
    SAMPLE_DIR.mkdir(exist_ok=True)
    replace_dir = SAMPLE_DIR / "replace_text_samples"
    replace_dir.mkdir(exist_ok=True)
    signature_dir = SAMPLE_DIR / "digital_signature_samples"
    signature_dir.mkdir(exist_ok=True)

    viewer_pdf = SAMPLE_DIR / "demo_pdf_viewer.pdf"
    split_pdf = SAMPLE_DIR / "demo_pdf_split.pdf"

    doc = fitz.open()
    pages = [
        [
            "PDF Toolkit Viewer Demo",
            "Purpose: demonstrate opening, paging, zooming, and text search.",
            "Search keyword: invoice",
            "This sample has selectable text, not scanned images.",
        ],
        [
            "Search Practice",
            "The word invoice appears on this page for Ctrl+F search practice.",
            "You can jump from result to result in the search panel.",
        ],
        [
            "Invoice Review",
            "invoice number: INV-2026-001",
            "invoice total: 1280",
            "This page repeats invoice for search results.",
        ],
        [
            "Viewer End Page",
            "Use fit width, fit page, and multiple columns to review pages quickly.",
        ],
    ]
    for lines in pages:
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 90), lines[0], fontsize=22, fontname="helv", color=(0.05, 0.2, 0.32))
        y = 140
        for text in lines[1:]:
            page.insert_text((72, y), text, fontsize=12, fontname="helv")
            y += 24
        page.insert_text((72, 800), f"Page {len(doc)}", fontsize=9, fontname="helv", color=(0.45, 0.45, 0.45))
    doc.save(viewer_pdf)
    doc.close()

    doc = fitz.open()
    for i in range(1, 7):
        page = doc.new_page(width=595, height=842)
        if i in (1, 3, 5):
            title = f"CASE START A-{(i + 1) // 2:03d}"
            desc = "This page starts a new case group."
        else:
            title = f"CASE DETAIL PAGE {i}"
            desc = "This page should stay grouped with the previous CASE START page."
        page.insert_text((72, 90), title, fontsize=18, fontname="helv")
        page.insert_text((72, 130), desc, fontsize=12, fontname="helv")
        page.insert_text((72, 160), "CASE START is the split keyword used in this tutorial.", fontsize=12, fontname="helv")
        page.insert_text((72, 800), f"Page {i}", fontsize=9, fontname="helv", color=(0.45, 0.45, 0.45))
    doc.save(split_pdf)
    doc.close()

    for suffix in ("A", "B", "C"):
        out = replace_dir / f"demo_replace_{suffix}.pdf"
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 90), f"Replacement Sample {suffix}", fontsize=18, fontname="helv")
        page.insert_text((72, 135), "Customer: OLD COMPANY NAME", fontsize=13, fontname="helv")
        page.insert_text((72, 165), "This document is safe sample data for batch replacement.", fontsize=12, fontname="helv")
        page.insert_text((72, 800), "Page 1", fontsize=9, fontname="helv", color=(0.45, 0.45, 0.45))
        doc.save(out)
        doc.close()

    from PIL import Image, ImageDraw, ImageFont  # type: ignore

    stamp_png = signature_dir / "demo_stamp.png"
    stamp = Image.new("RGBA", (360, 150), (255, 255, 255, 0))
    draw = ImageDraw.Draw(stamp)
    draw.rounded_rectangle((8, 8, 352, 142), radius=18, outline=(170, 20, 20, 255), width=8)
    try:
        font_big = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 42)
        font_small = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 22)
    except Exception:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()
    draw.text((62, 42), "APPROVED", fill=(170, 20, 20, 255), font=font_big)
    draw.text((112, 96), "DEMO STAMP", fill=(170, 20, 20, 255), font=font_small)
    stamp.save(stamp_png)

    signature_pdf = signature_dir / "demo_signature.pdf"
    doc = fitz.open()
    for i in range(1, 4):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 90), f"Digital Signature Sample {i}", fontsize=18, fontname="helv")
        page.insert_text((72, 140), "Approved by", fontsize=14, fontname="helv")
        page.insert_text((72, 180), "This safe sample demonstrates placing an electronic stamp after searched text.", fontsize=12, fontname="helv")
        page.insert_text((72, 800), f"Page {i}", fontsize=9, fontname="helv", color=(0.45, 0.45, 0.45))
    doc.save(signature_pdf)
    doc.close()

    return {
        "viewer": viewer_pdf,
        "splitter": split_pdf,
        "replace_dir": replace_dir,
        "signature_dir": signature_dir,
        "stamp": stamp_png,
    }


def pump(root, seconds: float = 0.4) -> None:
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.03)


def patch_feature_factories() -> None:
    import tkinter as tk
    import features.splitter as splitter
    import features.replace_text as replace_text
    import features.digital_signature as digital_signature

    def splitter_frame(parent, presets_dir=None):
        frame = ttk.Frame(parent)
        frame.app = splitter.App(frame, presets_dir=presets_dir)
        return frame

    def replace_frame(parent, presets_dir=None, open_in_viewer=None, open_bytes_in_viewer=None):
        frame = ttk.Frame(parent)
        frame.app = replace_text.App(
            frame,
            presets_dir=presets_dir,
            open_in_viewer=open_in_viewer,
            open_bytes_in_viewer=open_bytes_in_viewer,
        )
        return frame

    def signature_frame(parent, presets_dir=None, open_in_viewer=None, open_bytes_in_viewer=None):
        frame = ttk.Frame(parent)
        frame.app = digital_signature.App(
            frame,
            presets_dir=presets_dir,
            open_in_viewer=open_in_viewer,
            open_bytes_in_viewer=open_bytes_in_viewer,
        )
        return frame

    splitter.create_frame = splitter_frame
    replace_text.create_frame = replace_frame
    digital_signature.create_frame = signature_frame
    _ = tk


def launch_app():
    add_local_deps()
    patch_feature_factories()
    import tkinter as tk
    import pdftools

    root = tk.Tk()
    root.geometry(f"{WIDTH}x{HEIGHT}+0+0")
    root.update()
    try:
        root.state("zoomed")
    except tk.TclError:
        pass
    app = pdftools.App(root, presets_dir=SAMPLE_DIR / "manual_video_settings")
    pump(root, 0.8)
    return root, app


def show_feature(root, app, feature_id: str):
    feat = next(f for f in app.__class__.__module__ and __import__("pdftools").FEATURES if f["id"] == feature_id)
    for iid, item in app._iid_to_feat.items():
        if item["id"] == feature_id:
            app.tree.selection_set(iid)
            app.tree.focus(iid)
            break
    app._show_feature(feat)
    pump(root, 0.7)
    return app._feature_frames[feature_id].app


def configure_scene(root, app, samples: dict[str, Path], scene_name: str):
    import fitz  # type: ignore

    if scene_name == "opening":
        viewer = show_feature(root, app, "viewer")
        viewer.open_path(str(samples["viewer"]))
        pump(root, 0.8)
        return

    if scene_name == "viewer_open":
        viewer = show_feature(root, app, "viewer")
        viewer.open_path(str(samples["viewer"]))
        viewer.fit_page()
        viewer.show_page(0)
        pump(root, 0.8)
        return

    if scene_name == "viewer_tools":
        viewer = show_feature(root, app, "viewer")
        if viewer.doc is None:
            viewer.open_path(str(samples["viewer"]))
        viewer.next_page()
        viewer.zoom_in()
        viewer.fit_width()
        viewer.var_cols.set("2")
        viewer._on_cols_changed()
        pump(root, 0.8)
        return

    if scene_name == "viewer_search":
        viewer = show_feature(root, app, "viewer")
        if viewer.doc is None:
            viewer.open_path(str(samples["viewer"]))
        viewer.var_cols.set("2")
        viewer._on_cols_changed()
        if not viewer.search_visible:
            viewer._toggle_search()
        viewer.var_search.set("invoice")
        viewer._do_search()
        if viewer._matches:
            viewer._goto_match(viewer._matches[0])
        pump(root, 0.8)
        return

    if scene_name in {"splitter_setup", "splitter_detect", "splitter_preview"}:
        splitter = show_feature(root, app, "splitter")
        if splitter.doc is None:
            splitter.fitz = fitz
            splitter.doc = fitz.open(str(samples["splitter"]))
            splitter.total_pages = len(splitter.doc)
            splitter.pdf_path = str(samples["splitter"])
            splitter.var_pdf.set(str(samples["splitter"]))
            splitter.lbl_pages.config(text=f"共 {splitter.total_pages} 頁")
        splitter.var_mode.set("keyword")
        splitter._switch_mode()
        splitter.var_kw.set("CASE START")
        splitter.var_kw_extract.set("line")
        splitter._toggle_kw_search()
        if scene_name in {"splitter_detect", "splitter_preview"}:
            splitter._run_detect()
            children = splitter.tree.get_children()
            if children:
                splitter.tree.selection_set(children[0])
                splitter.tree.focus(children[0])
        if scene_name == "splitter_preview":
            splitter.notebook.select(splitter.tab_view)
            splitter._on_tab_changed()
        else:
            splitter.notebook.select(splitter.tab_main)
        pump(root, 1.0)
        return

    if scene_name in {"replace_setup", "replace_scan", "replace_compare", "replace_finish"}:
        rep = show_feature(root, app, "replace_text")
        rep.var_folder.set(str(samples["replace_dir"]))
        rep.var_outfolder.set(str(ROOT / "範例" / "replace_text_output"))
        rep.var_search.set("OLD COMPANY NAME")
        rep.var_replace.set("NEW COMPANY NAME")
        rep._on_folder_changed()
        if scene_name in {"replace_scan", "replace_compare", "replace_finish"}:
            rep._scan()
            children = rep.tree.get_children()
            if children:
                rep.tree.selection_set(children[0])
                rep.tree.focus(children[0])
        if scene_name == "replace_compare":
            rep.notebook.select(rep.tab_view)
            rep._on_tab_changed()
        else:
            rep.notebook.select(rep.tab_main)
        pump(root, 1.0)
        return

    if scene_name in {"signature_setup", "signature_rule", "signature_preview"}:
        sig = show_feature(root, app, "digital_signature")
        sig.var_source.set(str(samples["signature_dir"]))
        sig.var_filter.set("")
        sig.var_outfolder.set(str(ROOT / "範例" / "digital_signature_output"))
        sig.var_stamp.set(str(samples["stamp"]))
        sig.var_placement.set("search")
        sig.var_search.set("Approved by")
        sig.var_offset_x.set("10")
        sig.var_offset_y.set("-16")
        sig.var_width.set("96")
        sig.var_height.set("42")
        sig.var_scope.set("每頁")
        sig._on_source_changed()
        if scene_name in {"signature_rule", "signature_preview"}:
            sig.rules = []
            sig._add_rule()
            children = sig.tv_rules.get_children()
            if children:
                sig.tv_rules.selection_set(children[0])
                sig.tv_rules.focus(children[0])
        if scene_name == "signature_preview":
            sig._preview()
            viewer = app._feature_frames.get("viewer")
            viewer_app = getattr(viewer, "app", None)
            if viewer_app is not None:
                viewer_app.var_search.set("")
                viewer_app._clear_search()
                if viewer_app.search_visible:
                    viewer_app._toggle_search()
        pump(root, 1.0)
        return

    raise ValueError(f"unknown scene: {scene_name}")


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", ctypes.c_long),
        ("bmWidth", ctypes.c_long),
        ("bmHeight", ctypes.c_long),
        ("bmWidthBytes", ctypes.c_long),
        ("bmPlanes", ctypes.c_ushort),
        ("bmBitsPixel", ctypes.c_ushort),
        ("bmBits", ctypes.c_void_p),
    ]


def capture_window(root, Image):
    hwnd = root.winfo_id()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    hdc_window = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    ok = user32.PrintWindow(hwnd, hdc_mem, 2)
    if not ok:
        ok = user32.PrintWindow(hwnd, hdc_mem, 0)
    bmp = BITMAP()
    gdi32.GetObjectW(hbmp, ctypes.sizeof(BITMAP), ctypes.byref(bmp))
    data = ctypes.create_string_buffer(bmp.bmWidthBytes * bmp.bmHeight)
    gdi32.GetBitmapBits(hbmp, len(data), data)
    image = Image.frombuffer("RGB", (bmp.bmWidth, bmp.bmHeight), data, "raw", "BGRX", 0, 1).copy()
    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_window)
    return image


def load_font(ImageFont, size: int, bold: bool = False):
    candidates = [
        r"C:\Windows\Fonts\msjhbd.ttc" if bold else r"C:\Windows\Fonts\msjh.ttc",
        r"C:\Windows\Fonts\mingliu.ttc",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    out: list[str] = []
    for para in text.splitlines():
        line = ""
        for char in para:
            test = line + char
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] <= max_width or not line:
                line = test
            else:
                out.append(line)
                line = char
        if line:
            out.append(line)
    return out[:3]


def compose_frame(source, subtitle: str, Image, ImageDraw, ImageFont):
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    area_h = HEIGHT - SUBTITLE_H
    src = source.convert("RGB")
    scale = min(WIDTH / src.width, area_h / src.height)
    new_size = (max(1, int(src.width * scale)), max(1, int(src.height * scale)))
    src = src.resize(new_size, Image.LANCZOS)
    canvas.paste(src, ((WIDTH - new_size[0]) // 2, (area_h - new_size[1]) // 2))

    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, area_h, WIDTH, HEIGHT), fill=(2, 18, 32))
    font = load_font(ImageFont, 32)
    lines = wrap_text(draw, subtitle, font, WIDTH - 160)
    line_h = 42
    total_h = len(lines) * line_h
    y = area_h + (SUBTITLE_H - total_h) // 2 - 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), line, fill=(255, 255, 255), font=font)
        y += line_h
    return canvas


def generate_frames(script: dict[str, str], samples: dict[str, Path]):
    fitz, Image, ImageDraw, ImageFont = require_modules()
    _ = fitz
    if FRAMES_DIR.exists():
        shutil.rmtree(FRAMES_DIR)
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    root, app = launch_app()
    frames: list[Path] = []
    try:
        for idx, (scene_id, scene_name, _duration) in enumerate(SCENE_PLAN):
            configure_scene(root, app, samples, scene_name)
            raw = capture_window(root, Image)
            frame = compose_frame(raw, script[scene_id], Image, ImageDraw, ImageFont)
            out = FRAMES_DIR / f"scene_{idx:02d}_{scene_id}.png"
            frame.save(out)
            frames.append(out)
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return frames


def media_duration(ffprobe: str, path: Path) -> float:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return float(result.stdout.strip())


async def tts_one(text: str, out: Path) -> None:
    add_local_deps()
    import edge_tts  # type: ignore

    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(str(out))


def build_voice(ffmpeg: str, ffprobe: str, script: dict[str, str], durations: list[float]) -> tuple[Path | None, list[float]]:
    try:
        add_local_deps()
        import edge_tts  # noqa: F401
    except Exception:
        return None, durations

    if AUDIO_DIR.exists():
        shutil.rmtree(AUDIO_DIR)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    adjusted = list(durations)
    wavs: list[Path] = []
    for idx, (scene_id, _scene_name, _duration) in enumerate(SCENE_PLAN):
        mp3 = AUDIO_DIR / f"tts_{idx:02d}.mp3"
        wav = AUDIO_DIR / f"seg_{idx:02d}.wav"
        asyncio.run(tts_one(script[scene_id], mp3))
        natural = media_duration(ffprobe, mp3)
        adjusted[idx] = max(adjusted[idx], natural + 0.6)
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(mp3),
                "-af",
                f"apad,atrim=0:{adjusted[idx]:.3f}",
                "-ar",
                "48000",
                "-ac",
                "2",
                str(wav),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wavs.append(wav)

    concat = AUDIO_DIR / "concat_audio.txt"
    concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in wavs), encoding="utf-8")
    narration = AUDIO_DIR / "narration.wav"
    subprocess.run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(narration)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return narration, adjusted


def build_video(ffmpeg: str, frames: list[Path], durations: list[float], audio: Path | None):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    concat = MANUAL_DIR / "_frames.txt"
    lines: list[str] = []
    for frame, duration in zip(frames, durations):
        lines.append(f"file '{frame.as_posix()}'")
        lines.append(f"duration {duration:.3f}")
    lines.append(f"file '{frames[-1].as_posix()}'")
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")

    silent = MANUAL_DIR / "_silent.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-vf",
            f"fps={FPS},format=yuv420p",
            "-movflags",
            "+faststart",
            str(silent),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if audio is None:
        shutil.move(str(silent), OUTPUT_VIDEO)
        return
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(silent),
            "-i",
            str(audio),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(OUTPUT_VIDEO),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate PDFToolkit operation manual video.")
    parser.add_argument("--silent", action="store_true", help="只產生字幕影片，不產生旁白。")
    parser.add_argument("--keep-temp", action="store_true", help="保留暫存影格與音訊。")
    args = parser.parse_args()

    fitz, _Image, _ImageDraw, _ImageFont = require_modules()
    ffmpeg, ffprobe = find_ffmpeg()
    script = read_script()
    missing = [scene_id for scene_id, _name, _duration in SCENE_PLAN if scene_id not in script]
    if missing:
        raise RuntimeError("script.md 缺少段落：" + ", ".join(missing))
    if not STORYBOARD_PATH.exists():
        raise RuntimeError(f"找不到分鏡稿：{STORYBOARD_PATH}")

    samples = create_samples(fitz)
    frames = generate_frames(script, samples)
    durations = [duration for _scene_id, _scene_name, duration in SCENE_PLAN]
    audio = None
    if not args.silent:
        audio, durations = build_voice(ffmpeg, ffprobe, script, durations)
    build_video(ffmpeg, frames, durations, audio)

    duration = media_duration(ffprobe, OUTPUT_VIDEO)
    print(f"output={OUTPUT_VIDEO}")
    print(f"duration_seconds={duration:.3f}")
    print(f"sha256={sha256(OUTPUT_VIDEO)}")
    print(f"type={'voice' if audio else 'silent'}")

    if not args.keep_temp:
        for path in (FRAMES_DIR, AUDIO_DIR, MANUAL_DIR / "_frames.txt", MANUAL_DIR / "_silent.mp4"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
