#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 電子章工具。

支援將圖片章依搜尋結果後方、或指定頁面座標蓋入 PDF。
"""

from __future__ import annotations

import io
import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk


FONT = ("Microsoft JhengHei UI", 12)
SETTINGS_FILE = "digital_signature_rules.json"
SCOPE_LABELS = ("只蓋一次", "每個搜尋結果", "每頁")
SCOPE_VALUES = {
    "只蓋一次": "once",
    "每個搜尋結果": "each_match",
    "每頁": "each_page",
}
SCOPE_NAMES = {v: k for k, v in SCOPE_VALUES.items()}
PLACEMENT_NAMES = {
    "search": "搜尋文字後方",
    "fixed": "指定位置",
}


def _import_fitz():
    """延遲匯入 PyMuPDF，沒裝時給清楚訊息。"""
    try:
        import fitz  # noqa: F401
        return fitz
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "找不到 PyMuPDF (fitz) 套件。\n\n"
            "獨立執行請先安裝：pip install PyMuPDF\n"
            "透過 MyTools Launcher 安裝時會自動依 requirements.txt 裝好。\n\n"
            f"原始錯誤：{e}"
        )


def _default_output_path(src: str) -> str:
    if not src:
        return ""
    p = Path(src)
    return str(p.with_name(f"{p.stem}-加電子章{p.suffix}"))


def _output_path(src, outfolder):
    if outfolder:
        return str(Path(outfolder) / Path(src).name)
    return str(src)


def _as_float(value, default=0.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_int(value, default=1):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _clamped_rect(fitz, page, x, y, width, height):
    """建立電子章矩形，盡量維持在頁面範圍內。座標單位為 PDF point。"""
    r = page.rect
    width = max(1.0, float(width))
    height = max(1.0, float(height))
    max_x = max(r.x0, r.x1 - width)
    max_y = max(r.y0, r.y1 - height)
    x = min(max(float(x), r.x0), max_x)
    y = min(max(float(y), r.y0), max_y)
    return fitz.Rect(x, y, x + width, y + height)


def _stamp_targets(fitz, doc, search_text, placement, scope,
                   fixed_page, fixed_x, fixed_y, offset_x, offset_y,
                   width, height):
    targets = []
    if placement == "fixed":
        if scope == "each_page":
            pages = range(len(doc))
        else:
            pages = [max(0, min(len(doc) - 1, fixed_page - 1))]
        for pno in pages:
            page = doc[pno]
            targets.append((pno, _clamped_rect(fitz, page, fixed_x, fixed_y,
                                               width, height)))
        return targets

    needle = (search_text or "").strip()
    if not needle:
        raise ValueError("請輸入搜尋文字，或改用指定位置。")

    for pno, page in enumerate(doc):
        rects = page.search_for(needle)
        if scope == "each_page" and rects:
            rects = rects[:1]
        for found in rects:
            rect = _clamped_rect(
                fitz, page,
                found.x1 + offset_x,
                found.y0 + offset_y,
                width,
                height,
            )
            targets.append((pno, rect))
            if scope == "once":
                return targets
    return targets


def stamp_pdf_file(src, dst, stamp_image, search_text="", placement="search",
                   scope="once", fixed_page=1, fixed_x=72, fixed_y=72,
                   offset_x=4, offset_y=0, width=72, height=36):
    """將圖片章蓋入 PDF，回傳實際蓋章數。"""
    rule = {
        "stamp_image": stamp_image,
        "search_text": search_text,
        "placement": placement,
        "scope": scope,
        "fixed_page": fixed_page,
        "fixed_x": fixed_x,
        "fixed_y": fixed_y,
        "offset_x": offset_x,
        "offset_y": offset_y,
        "width": width,
        "height": height,
    }
    return stamp_pdf_rules_file(src, dst, [rule])


def _apply_stamp_rules(doc, rules):
    total = 0
    for idx, rule in enumerate(rules, start=1):
        stamp_image = rule["stamp_image"]
        if not stamp_image or not os.path.exists(stamp_image):
            raise ValueError(f"第 {idx} 筆規則的電子章圖片不存在。")
        image_bytes = Path(stamp_image).read_bytes()
        targets = _stamp_targets(
            _import_fitz(), doc,
            rule.get("search_text", ""),
            rule.get("placement", "search"),
            rule.get("scope", "once"),
            rule.get("fixed_page", 1),
            rule.get("fixed_x", 72),
            rule.get("fixed_y", 72),
            rule.get("offset_x", 4),
            rule.get("offset_y", 0),
            rule.get("width", 72),
            rule.get("height", 36),
        )
        for pno, rect in targets:
            doc[pno].insert_image(rect, stream=image_bytes,
                                  keep_proportion=True)
        total += len(targets)
    return total


def stamp_pdf_rules_bytes(src, rules):
    """依多筆規則產生預覽 PDF bytes，不寫入檔案。"""
    fitz = _import_fitz()
    doc = fitz.open(stream=Path(src).read_bytes(), filetype="pdf")
    try:
        total = _apply_stamp_rules(doc, rules)
        if total <= 0:
            return b"", 0
        out = io.BytesIO()
        doc.save(out, garbage=4, deflate=True)
        return out.getvalue(), total
    finally:
        doc.close()


def stamp_pdf_rules_file(src, dst, rules):
    """依多筆規則將圖片章蓋入 PDF，回傳實際蓋章總數。"""
    fitz = _import_fitz()
    doc = fitz.open(stream=Path(src).read_bytes(), filetype="pdf")
    try:
        total = _apply_stamp_rules(doc, rules)
        if total <= 0:
            return 0
        out = io.BytesIO()
        doc.save(out, garbage=4, deflate=True)
        Path(dst).write_bytes(out.getvalue())
        return total
    finally:
        doc.close()


class App:
    def __init__(self, root, presets_dir=None, open_in_viewer=None,
                 open_bytes_in_viewer=None):
        self.root = root
        self.open_in_viewer = open_in_viewer
        self.open_bytes_in_viewer = open_bytes_in_viewer
        self.presets_dir = presets_dir
        self.settings_path = self._settings_path()
        self.rule_sets = {}
        self._load_settings()
        if isinstance(root, (tk.Tk, tk.Toplevel)):
            self.root.title("PDF 電子章")
            self.root.geometry("900x640")
            self.root.minsize(820, 560)
            self._setup_style()
        self._build_ui()

    def _settings_path(self):
        base = self.presets_dir
        if base is None:
            base = Path.home() / ".pdf2"
        return Path(base) / SETTINGS_FILE

    def _load_settings(self):
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        self.rule_sets = data.get("rule_sets") or {}

    def _save_settings(self):
        data = {
            "rule_sets": self.rule_sets,
        }
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            self.lbl_status.config(text=f"規則設定檔儲存失敗：{e}")

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.option_add("*Font", FONT)
        style.configure("TLabelframe", padding=8)
        style.configure("TLabelframe.Label",
                        font=("Microsoft JhengHei UI", 12, "bold"))
        style.configure("Accent.TButton", padding=[18, 6],
                        font=("Microsoft JhengHei UI", 12, "bold"))

    def _build_ui(self):
        self.var_source = tk.StringVar()
        self.var_filter = tk.StringVar()
        self.var_outfolder = tk.StringVar()
        self.var_stamp = tk.StringVar()
        self.var_search = tk.StringVar()
        self.var_placement = tk.StringVar(value="search")
        self.var_scope = tk.StringVar(value="只蓋一次")
        self.var_page = tk.StringVar(value="1")
        self.var_x = tk.StringVar(value="72")
        self.var_y = tk.StringVar(value="72")
        self.var_offset_x = tk.StringVar(value="4")
        self.var_offset_y = tk.StringVar(value="0")
        self.var_width = tk.StringVar(value="72")
        self.var_height = tk.StringVar(value="36")
        self.var_rule_set = tk.StringVar()
        self.rules = []

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=10, pady=10)

        files = ttk.LabelFrame(body, text="來源與輸出")
        files.pack(fill="x")
        files.columnconfigure(1, weight=1)
        ttk.Label(files, text="來源").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        ent_source = ttk.Entry(files, textvariable=self.var_source)
        ent_source.grid(row=0, column=1, sticky="ew", pady=4)
        ent_source.bind("<FocusOut>", lambda _e: self._on_source_changed())
        ent_source.bind("<Return>", lambda _e: self._on_source_changed())
        src_btns = ttk.Frame(files)
        src_btns.grid(row=0, column=2, sticky="w", padx=(6, 0), pady=4)
        ttk.Button(src_btns, text="選資料夾", command=self._pick_folder).pack(side="left")
        ttk.Button(src_btns, text="選檔案", command=self._pick_file).pack(side="left", padx=(4, 0))
        self.lbl_filter = ttk.Label(files, text="檔名篩選")
        self.lbl_filter.grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        self.ent_filter = ttk.Entry(files, textvariable=self.var_filter)
        self.ent_filter.grid(row=1, column=1, sticky="ew", pady=4)
        self.ent_filter.bind("<KeyRelease>", lambda _e: self._on_source_changed())
        self.lbl_filter_hint = ttk.Label(
            files, text="只處理檔名包含此文字的 PDF；留空＝資料夾內全部",
            foreground="#666")
        self.lbl_filter_hint.grid(row=2, column=1, sticky="w")
        ttk.Label(files, text="輸出資料夾").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(files, textvariable=self.var_outfolder).grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Button(files, text="瀏覽", command=self._pick_outfolder).grid(row=3, column=2, padx=(6, 0), pady=4)
        ttk.Label(files, text="留空＝直接覆寫原檔；有填則輸出同檔名到該資料夾",
                  foreground="#a60").grid(row=4, column=1, sticky="w")

        pdf_box = ttk.LabelFrame(body, text="會處理的 PDF")
        pdf_box.pack(fill="both", expand=True, pady=(10, 0))
        pdf_cols = ("name", "folder")
        self.tv_pdfs = ttk.Treeview(
            pdf_box, columns=pdf_cols, show="headings", height=4)
        self.tv_pdfs.heading("name", text="檔名")
        self.tv_pdfs.heading("folder", text="資料夾")
        self.tv_pdfs.column("name", width=260, anchor="w")
        self.tv_pdfs.column("folder", width=520, anchor="w")
        pdf_scroll = ttk.Scrollbar(pdf_box, orient="vertical",
                                   command=self.tv_pdfs.yview)
        self.tv_pdfs.configure(yscrollcommand=pdf_scroll.set)
        self.tv_pdfs.pack(side="left", fill="both", expand=True,
                          padx=(0, 6), pady=(2, 6))
        pdf_scroll.pack(side="left", fill="y", pady=(2, 6))
        self.tv_pdfs.bind("<Double-1>", self._preview_selected)

        stamp_box = ttk.LabelFrame(body, text="電子章")
        stamp_box.pack(fill="x", pady=(10, 0))
        stamp_box.columnconfigure(1, weight=1)
        ttk.Label(stamp_box, text="電子章").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(stamp_box, textvariable=self.var_stamp).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(stamp_box, text="選擇", command=self._pick_stamp).grid(row=0, column=2, padx=(6, 0), pady=4)

        pos = ttk.LabelFrame(body, text="蓋章位置")
        pos.pack(fill="x", pady=(10, 0))
        ttk.Radiobutton(
            pos, text="搜尋文字後方", value="search",
            variable=self.var_placement, command=self._refresh_state).grid(
                row=0, column=0, sticky="w", pady=4)
        ttk.Entry(pos, textvariable=self.var_search, width=36).grid(
            row=0, column=1, sticky="w", padx=(8, 16), pady=4)
        ttk.Label(pos, text="X 偏移").grid(row=0, column=2, sticky="e")
        ttk.Entry(pos, textvariable=self.var_offset_x, width=8).grid(row=0, column=3, padx=(4, 10))
        ttk.Label(pos, text="Y 偏移").grid(row=0, column=4, sticky="e")
        ttk.Entry(pos, textvariable=self.var_offset_y, width=8).grid(row=0, column=5, padx=(4, 0))

        ttk.Radiobutton(
            pos, text="指定位置", value="fixed",
            variable=self.var_placement, command=self._refresh_state).grid(
                row=1, column=0, sticky="w", pady=4)
        ttk.Label(pos, text="頁碼").grid(row=1, column=1, sticky="w", padx=(8, 0))
        ttk.Entry(pos, textvariable=self.var_page, width=8).grid(row=1, column=1, sticky="w", padx=(50, 16))
        ttk.Label(pos, text="X").grid(row=1, column=2, sticky="e")
        ttk.Entry(pos, textvariable=self.var_x, width=8).grid(row=1, column=3, padx=(4, 10))
        ttk.Label(pos, text="Y").grid(row=1, column=4, sticky="e")
        ttk.Entry(pos, textvariable=self.var_y, width=8).grid(row=1, column=5, padx=(4, 0))

        style = ttk.LabelFrame(body, text="套用方式與大小")
        style.pack(fill="x", pady=(10, 0))
        ttk.Label(style, text="套用").grid(row=0, column=0, sticky="w", pady=4)
        self.cmb_scope = ttk.Combobox(
            style, textvariable=self.var_scope, state="readonly", width=14,
            values=SCOPE_LABELS)
        self.cmb_scope.grid(row=0, column=1, sticky="w", padx=(8, 18), pady=4)
        self.cmb_scope.bind("<<ComboboxSelected>>", lambda _e: self._refresh_state())
        ttk.Label(style, text="搜尋模式的「每頁」會取每頁第一個搜尋結果；指定位置的「每頁」會蓋在每一頁同座標。").grid(
            row=0, column=2, columnspan=5, sticky="w")
        ttk.Label(style, text="寬").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(style, textvariable=self.var_width, width=8).grid(row=1, column=1, sticky="w", padx=(8, 18))
        ttk.Label(style, text="高").grid(row=1, column=2, sticky="e")
        ttk.Entry(style, textvariable=self.var_height, width=8).grid(row=1, column=3, sticky="w", padx=(4, 18))
        ttk.Label(style, text="單位為 PDF point；A4 約 595 x 842").grid(row=1, column=4, sticky="w")

        rules = ttk.LabelFrame(body, text="多筆電子章規則")
        rules.pack(fill="x", pady=(10, 0))
        preset_bar = ttk.Frame(rules)
        preset_bar.pack(fill="x", pady=(2, 6))
        ttk.Label(preset_bar, text="規則組").pack(side="left")
        self.cmb_rule_set = ttk.Combobox(
            preset_bar, textvariable=self.var_rule_set, state="readonly",
            width=24, values=sorted(self.rule_sets))
        self.cmb_rule_set.pack(side="left", padx=(6, 4))
        ttk.Button(preset_bar, text="載入規則組",
                   command=self._load_rule_set).pack(side="left", padx=(0, 4))
        ttk.Button(preset_bar, text="儲存規則組",
                   command=self._save_rule_set).pack(side="left", padx=(0, 4))
        ttk.Button(preset_bar, text="刪除規則組",
                   command=self._delete_rule_set).pack(side="left")
        cols = ("stamp", "placement", "target", "scope", "size")
        self.tv_rules = ttk.Treeview(rules, columns=cols, show="headings", height=3)
        heads = {
            "stamp": "電子章",
            "placement": "位置",
            "target": "搜尋/座標",
            "scope": "套用",
            "size": "大小",
        }
        widths = {
            "stamp": 180,
            "placement": 110,
            "target": 230,
            "scope": 110,
            "size": 90,
        }
        for c in cols:
            self.tv_rules.heading(c, text=heads[c])
            self.tv_rules.column(c, width=widths[c], anchor="w")
        rscroll = ttk.Scrollbar(rules, orient="vertical",
                                command=self.tv_rules.yview)
        self.tv_rules.configure(yscrollcommand=rscroll.set)
        self.tv_rules.pack(side="left", fill="both", expand=True, padx=(0, 6), pady=(2, 6))
        rscroll.pack(side="left", fill="y", pady=(2, 6))
        rbtns = ttk.Frame(rules)
        rbtns.pack(side="right", fill="y", pady=(2, 6))
        ttk.Button(rbtns, text="加入規則", command=self._add_rule).pack(fill="x", pady=(0, 4))
        ttk.Button(rbtns, text="更新選取", command=self._update_rule).pack(fill="x", pady=(0, 4))
        ttk.Button(rbtns, text="刪除選取", command=self._delete_rule).pack(fill="x", pady=(0, 4))
        ttk.Button(rbtns, text="清空規則", command=self._clear_rules).pack(fill="x")

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="預覽輸出", command=self._preview).pack(side="left")
        ttk.Button(actions, text="執行輸出", style="Accent.TButton",
                   command=self._run).pack(side="right")
        self.lbl_status = ttk.Label(body, text="", foreground="#555", anchor="w")
        self.lbl_status.pack(fill="x", pady=(10, 0))
        self._refresh_state()
        self._refresh_rules(save=False)
        self._refresh_pdf_list()

    def _pick_folder(self):
        d = filedialog.askdirectory(title="選擇要批次蓋章的資料夾")
        if d:
            self.var_source.set(d)
            self._on_source_changed()

    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="選擇單一 PDF",
            filetypes=[("PDF 檔案", "*.pdf"), ("所有檔案", "*.*")])
        if p:
            self.var_source.set(p)
            self._on_source_changed()

    def _pick_stamp(self):
        p = filedialog.askopenfilename(
            title="選擇電子章圖片",
            filetypes=[
                ("圖片檔", "*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff"),
                ("所有檔案", "*.*"),
            ])
        if p:
            self.var_stamp.set(p)

    def _pick_outfolder(self):
        d = filedialog.askdirectory(title="選擇輸出資料夾")
        if d:
            self.var_outfolder.set(d)
            self._on_source_changed()

    def _list_pdfs(self):
        src = self.var_source.get().strip()
        if not src:
            return []
        p = Path(src)
        if p.is_file():
            return [p] if p.suffix.lower() == ".pdf" else []
        if not p.is_dir():
            return []
        pdfs = sorted(x for x in p.glob("*.pdf") if x.is_file())
        kw = self.var_filter.get().strip().lower()
        if kw:
            pdfs = [x for x in pdfs if kw in x.name.lower()]
        return pdfs

    def _update_filter_visibility(self):
        src = self.var_source.get().strip()
        is_file = bool(src) and os.path.isfile(src)
        for w in (self.lbl_filter, self.ent_filter, self.lbl_filter_hint):
            if is_file:
                w.grid_remove()
            else:
                w.grid()

    def _on_source_changed(self):
        self._update_filter_visibility()
        count = self._refresh_pdf_list()
        self.lbl_status.config(text=f"待處理：{count} 個 PDF" if count else "")

    def _refresh_pdf_list(self):
        if not hasattr(self, "tv_pdfs"):
            return 0
        self.tv_pdfs.delete(*self.tv_pdfs.get_children())
        pdfs = self._list_pdfs()
        for i, p in enumerate(pdfs):
            self.tv_pdfs.insert("", "end", iid=str(i),
                                values=(p.name, str(p.parent)))
        if pdfs:
            self.tv_pdfs.selection_set("0")
        return len(pdfs)

    def _selected_pdf(self):
        pdfs = self._list_pdfs()
        sel = self.tv_pdfs.selection()
        if not sel:
            return pdfs[0] if pdfs else None
        try:
            idx = int(sel[0])
        except ValueError:
            return None
        if 0 <= idx < len(pdfs):
            return pdfs[idx]
        return None

    def _refresh_state(self):
        if self.var_placement.get() == "fixed" and self.var_scope.get() == "每個搜尋結果":
            self.var_scope.set("只蓋一次")

    def _current_rule(self):
        stamp = self.var_stamp.get().strip()
        if not stamp or not os.path.exists(stamp):
            raise ValueError("請選擇有效的電子章圖片。")
        placement = self.var_placement.get()
        scope = SCOPE_VALUES.get(self.var_scope.get(), "once")
        if placement == "fixed" and scope == "each_match":
            scope = "once"
        rule = {
            "stamp_image": stamp,
            "search_text": self.var_search.get().strip(),
            "placement": placement,
            "scope": scope,
            "fixed_page": _as_int(self.var_page.get(), 1),
            "fixed_x": _as_float(self.var_x.get(), 72),
            "fixed_y": _as_float(self.var_y.get(), 72),
            "offset_x": _as_float(self.var_offset_x.get(), 4),
            "offset_y": _as_float(self.var_offset_y.get(), 0),
            "width": _as_float(self.var_width.get(), 72),
            "height": _as_float(self.var_height.get(), 36),
        }
        if placement == "search" and not rule["search_text"]:
            raise ValueError("搜尋文字後方模式請輸入搜尋文字。")
        return rule

    def _set_form_rule(self, rule):
        self.var_stamp.set(rule.get("stamp_image", ""))
        self.var_search.set(rule.get("search_text", ""))
        self.var_placement.set(rule.get("placement", "search"))
        self.var_scope.set(SCOPE_NAMES.get(rule.get("scope", "once"), "只蓋一次"))
        self.var_page.set(str(rule.get("fixed_page", 1)))
        self.var_x.set(str(rule.get("fixed_x", 72)))
        self.var_y.set(str(rule.get("fixed_y", 72)))
        self.var_offset_x.set(str(rule.get("offset_x", 4)))
        self.var_offset_y.set(str(rule.get("offset_y", 0)))
        self.var_width.set(str(rule.get("width", 72)))
        self.var_height.set(str(rule.get("height", 36)))
        self._refresh_state()

    def _rule_values(self, rule):
        stamp = Path(rule.get("stamp_image", "")).name
        placement = PLACEMENT_NAMES.get(rule.get("placement"), "搜尋文字後方")
        if rule.get("placement") == "fixed":
            target = (
                f"第 {rule.get('fixed_page', 1)} 頁, "
                f"X {rule.get('fixed_x', 72)}, Y {rule.get('fixed_y', 72)}"
            )
        else:
            target = (
                f"{rule.get('search_text', '')} "
                f"(偏移 {rule.get('offset_x', 4)}, {rule.get('offset_y', 0)})"
            )
        scope = SCOPE_NAMES.get(rule.get("scope", "once"), "只蓋一次")
        size = f"{rule.get('width', 72)} x {rule.get('height', 36)}"
        return (stamp, placement, target, scope, size)

    def _refresh_rule_set_combo(self):
        names = sorted(self.rule_sets)
        self.cmb_rule_set.configure(values=names)
        if self.var_rule_set.get() not in names:
            self.var_rule_set.set(names[0] if names else "")

    def _refresh_rules(self, save=True):
        self.tv_rules.delete(*self.tv_rules.get_children())
        for i, rule in enumerate(self.rules):
            self.tv_rules.insert("", "end", iid=str(i),
                                 values=self._rule_values(rule))
        self._refresh_rule_set_combo()
        if save:
            self._save_settings()
        self.lbl_status.config(text=f"目前有 {len(self.rules)} 筆電子章規則。")

    def _selected_rule_index(self):
        sel = self.tv_rules.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _add_rule(self):
        try:
            self.rules.append(self._current_rule())
        except Exception as e:
            messagebox.showerror("錯誤", str(e))
            return
        self._refresh_rules()

    def _update_rule(self):
        idx = self._selected_rule_index()
        if idx is None or not (0 <= idx < len(self.rules)):
            messagebox.showwarning("提示", "請先選取要更新的規則。")
            return
        try:
            self.rules[idx] = self._current_rule()
        except Exception as e:
            messagebox.showerror("錯誤", str(e))
            return
        self._refresh_rules()
        self.tv_rules.selection_set(str(idx))

    def _delete_rule(self):
        idx = self._selected_rule_index()
        if idx is None or not (0 <= idx < len(self.rules)):
            messagebox.showwarning("提示", "請先選取要刪除的規則。")
            return
        del self.rules[idx]
        self._refresh_rules()

    def _clear_rules(self):
        if not self.rules:
            return
        if not messagebox.askyesno("清空確認", "確定要清空所有電子章規則？"):
            return
        self.rules.clear()
        self._refresh_rules()

    def _load_rule_set(self):
        name = self.var_rule_set.get().strip()
        if not name:
            messagebox.showwarning("提示", "請先選擇要載入的規則組。")
            return
        rules = self.rule_sets.get(name)
        if rules is None:
            messagebox.showwarning("提示", "找不到選取的規則組。")
            return
        self.rules = [dict(r) for r in rules]
        self._refresh_rules()

    def _save_rule_set(self):
        if not self.rules:
            messagebox.showwarning("提示", "目前沒有可儲存的電子章規則。")
            return
        name = simpledialog.askstring(
            "儲存規則組",
            "請輸入規則組名稱。\n若名稱已存在，會覆蓋原本的規則組：",
            initialvalue=self.var_rule_set.get() or "常用規則")
        if not name:
            return
        name = name.strip()
        if not name:
            return
        self.rule_sets[name] = [dict(r) for r in self.rules]
        self.var_rule_set.set(name)
        self._refresh_rules()

    def _delete_rule_set(self):
        name = self.var_rule_set.get().strip()
        if not name:
            messagebox.showwarning("提示", "請先選擇要刪除的規則組。")
            return
        if name not in self.rule_sets:
            messagebox.showwarning("提示", "找不到選取的規則組。")
            return
        if not messagebox.askyesno("刪除確認", f"確定要刪除規則組「{name}」？"):
            return
        del self.rule_sets[name]
        self.var_rule_set.set("")
        self._refresh_rules()

    def _rules_for_run(self):
        if self.rules:
            return [dict(r) for r in self.rules]
        return [self._current_rule()]

    def _values(self):
        pdfs = self._list_pdfs()
        outfolder = self.var_outfolder.get().strip()
        if not pdfs:
            raise ValueError("請選擇有效的 PDF 或資料夾。")
        if outfolder:
            os.makedirs(outfolder, exist_ok=True)
        return {
            "pdfs": pdfs,
            "outfolder": outfolder,
            "rules": self._rules_for_run(),
        }

    def _preview_selected(self, _evt=None):
        try:
            opts = self._values()
            picked = self._selected_pdf()
            if picked is None:
                raise ValueError("請先選擇要預覽的 PDF。")
            src = str(picked)
            data, count = stamp_pdf_rules_bytes(src, opts["rules"])
        except Exception as e:
            messagebox.showerror("錯誤", str(e))
            return
        if count <= 0:
            messagebox.showinfo("預覽", "沒有找到可蓋章的位置。")
            return
        self.lbl_status.config(
            text=f"已產生預覽（未寫入檔案）：{Path(src).name}，共 {count} 個電子章。")
        if self.open_bytes_in_viewer is not None:
            self.open_bytes_in_viewer(data, f"{Path(src).stem}-電子章預覽.pdf")
        else:
            messagebox.showwarning(
                "預覽",
                "目前執行環境沒有內建 PDF 預覽器回呼，因此未產生檔案。")

    def _preview(self):
        self._preview_selected()

    def _run(self):
        try:
            opts = self._values()
            if not opts["outfolder"]:
                ok = messagebox.askyesno(
                    "覆寫確認",
                    "輸出資料夾留空，會覆寫所有來源 PDF。是否繼續？")
                if not ok:
                    return
            total_count = 0
            done = 0
            failed = []
            for src_path in opts["pdfs"]:
                src = str(src_path)
                dst = _output_path(src, opts["outfolder"])
                try:
                    count = stamp_pdf_rules_file(src, dst, opts["rules"])
                    if count > 0:
                        done += 1
                        total_count += count
                except Exception as e:
                    failed.append(f"{Path(src).name}: {e}")
        except Exception as e:
            messagebox.showerror("錯誤", str(e))
            return
        if total_count <= 0 and not failed:
            messagebox.showinfo("完成", "沒有找到可蓋章的位置，未輸出新 PDF。")
            return
        msg = f"完成：{done} 個 PDF，共加上 {total_count} 個電子章。"
        if opts["outfolder"]:
            msg += f"\n輸出資料夾：{opts['outfolder']}"
        if failed:
            msg += "\n\n失敗 {} 個：\n{}".format(len(failed), "\n".join(failed[:8]))
        self.lbl_status.config(text=msg.splitlines()[0])
        (messagebox.showwarning if failed else messagebox.showinfo)("完成", msg)


def create_frame(parent, presets_dir=None, open_in_viewer=None,
                 open_bytes_in_viewer=None):
    frame = ttk.Frame(parent)
    App(frame, presets_dir=presets_dir, open_in_viewer=open_in_viewer,
        open_bytes_in_viewer=open_bytes_in_viewer)
    return frame


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
