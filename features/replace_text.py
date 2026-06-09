#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 文字取代 (GUI 版)

批次掃描資料夾內的 PDF,把「搜尋文字」就地換成「取代文字」,
保留原字型 / 字級 / 顏色 / 書寫方向(含直書與頁面旋轉)。

做法:用 PyMuPDF 找到文字所在的 text-run(span)→ 以遮蔽(redaction)
蓋掉舊字 → 在同一原點以對應字型重寫新字。只動到該段文字,
影像與向量線條(地籍/勘查圖的線)完全不碰。

限制:
  • 只能處理「文字型」PDF;掃描成影像的 PDF 文字是圖,無法取代(清單會標示)。
  • 比對以 NFKC 正規化,忽略全形/半形差異(全形「：」與半形「:」視為相同)。
  • 搜尋字串需落在同一段 text-run 內;若 PDF 把字串拆成多段則該段不會命中。

新增功能 = 寫一個 features/<name>.py + 在 main_frame.py 的 FEATURES 加一筆。
"""

import io
import os
import sys
import unicodedata
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


def app_dir():
    """程式所在資料夾。PyInstaller 打包後也能正確指向 .exe 旁邊。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _load_viewer_module():
    """載入同目錄的 viewer 模組(供並排比對視窗用)。
    先試套件匯入(透過 launcher);失敗則以檔案路徑載入(獨立執行)。"""
    try:
        from features import viewer as mod
        return mod
    except Exception:
        import importlib.util
        vp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer.py")
        spec = importlib.util.spec_from_file_location("viewer", vp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _import_fitz():
    """延遲匯入 PyMuPDF,沒裝時給清楚訊息。"""
    try:
        import fitz  # noqa: F401  (PyMuPDF)
        return fitz
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "找不到 PyMuPDF (fitz) 套件。\n\n"
            "獨立執行請先安裝：pip install PyMuPDF\n"
            "（透過 MyTools Launcher 安裝時會自動依 requirements.txt 裝好）\n\n"
            f"原始錯誤：{e}"
        )


# ===========================================================================
#  core/text — 正規化與字型解析(純函式)
# ===========================================================================

def _norm(s: str) -> str:
    """NFKC 正規化。讓全形/半形(含全形冒號、全形空格)與相容表意字一致比對。"""
    return unicodedata.normalize("NFKC", s or "")


_WIN_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def _wf(name):
    return os.path.join(_WIN_FONTS, name)


# 顯示名稱 → 字型檔(僅列出存在者供選單;直書中文用)
_FONT_CANDIDATES = [
    ("新細明體 PMingLiU", _wf("mingliu.ttc")),
    ("微軟正黑體 JhengHei", _wf("msjh.ttc")),
    ("標楷體 DFKai-SB", _wf("kaiu.ttf")),
    ("細明體 MingLiU", _wf("mingliu.ttc")),
    ("微軟雅黑 YaHei", _wf("msyh.ttc")),
    ("新宋體 SimSun", _wf("simsun.ttc")),
]

AUTO_FONT = "自動（沿用原字型，建議）"

# PDF 內字型 PostScript 名稱關鍵字 → Windows 字型檔(自動沿用用)
_PS_MAP = [
    ("pmingliu", _wf("mingliu.ttc")),
    ("mingliu", _wf("mingliu.ttc")),
    ("jhenghei", _wf("msjh.ttc")),
    ("yahei", _wf("msyh.ttc")),
    ("dfkai", _wf("kaiu.ttf")),
    ("kaiu", _wf("kaiu.ttf")),
    ("kai", _wf("kaiu.ttf")),
    ("nsimsun", _wf("simsun.ttc")),
    ("simsun", _wf("simsun.ttc")),
    ("song", _wf("simsun.ttc")),
    ("simhei", _wf("simhei.ttf")),
]


def available_fonts():
    """回傳可選字型顯示名稱清單(自動 + 系統實際存在的)。"""
    seen, out = set(), [AUTO_FONT]
    for label, path in _FONT_CANDIDATES:
        if os.path.exists(path) and label not in seen:
            out.append(label)
            seen.add(label)
    return out


def _font_label_to_file(label):
    for lb, path in _FONT_CANDIDATES:
        if lb == label and os.path.exists(path):
            return path
    return None


def _default_font_file():
    for _, path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None  # 系統找不到任何 CJK 字型(極罕見)


def resolve_font_file(span_font, choice):
    """決定某 span 重寫時用哪個字型檔。
    choice == AUTO_FONT:依 span 原字型自動對應,找不到再用系統預設。
    其他:固定用使用者選的字型。"""
    if choice and choice != AUTO_FONT:
        f = _font_label_to_file(choice)
        if f:
            return f
    n = (span_font or "").lower()
    for key, path in _PS_MAP:
        if key in n and os.path.exists(path):
            return path
    return _default_font_file()


def _rotate_for_dir(d):
    """由 span 書寫方向(line dir)推回 insert_text 用的旋轉角(0/90/180/270)。"""
    dx, dy = d
    if abs(dx - 1) < 0.05:
        return 0
    if abs(dy - 1) < 0.05:
        return 270   # 由上往下(配合頁面 /Rotate 後視覺為水平)
    if abs(dx + 1) < 0.05:
        return 180
    return 90


def _color_to_rgb(color):
    return ((color >> 16 & 255) / 255.0,
            (color >> 8 & 255) / 255.0,
            (color & 255) / 255.0)


# ===========================================================================
#  core/scan + replace
# ===========================================================================

def _collect_targets(page, nsearch, nreplace=None, mode="normal"):
    """掃一頁,回傳所有命中的 span 目標(不修改頁面)。

    mode 決定「標籤:值」欄位的處理方式:
      • "normal"     一般取代:只替換搜尋字串本身。
      • "overwrite"  取代至文字結尾:標籤到該段文字結尾整段換成取代字串。
      • "fill_empty" 排除非文字結尾:標籤後(到段尾)已有非空白舊值則略過不動。
    任一模式都會跳過「已是目標值」的 span,避免重複執行時內容疊加。
    """
    idempotent = bool(nreplace) and (nsearch in nreplace)
    targets = []
    for blk in page.get_text("dict")["blocks"]:
        for line in blk.get("lines", []):
            for sp in line.get("spans", []):
                ntext = _norm(sp["text"])
                if nsearch not in ntext:
                    continue
                if mode in ("overwrite", "fill_empty"):
                    idx = ntext.find(nsearch)
                    after = ntext[idx + len(nsearch):]
                    if mode == "fill_empty" and after.strip():
                        continue        # 已有舊值 → 保留不動
                    if nreplace is not None and ntext[idx:] == nreplace:
                        continue        # 已是目標值 → 跳過(冪等)
                elif idempotent and nreplace in ntext:
                    continue            # 一般模式:已是先前取代結果
                targets.append({
                    "bbox": sp["bbox"],
                    "origin": sp["origin"],
                    "size": sp["size"],
                    "color": sp["color"],
                    "dir": line["dir"],
                    "font": sp["font"],
                    "ntext": ntext,
                })
    return targets


def scan_pdf(fitz, path, search, replace="", mode="normal"):
    """掃一個 PDF。回傳 dict:
       {hits:int, has_text:bool, error:str|None}
    hits = 命中(且尚未取代過)的 span 數;has_text = 整份是否有可擷取文字。"""
    nsearch = _norm(search)
    if not nsearch:
        return {"hits": 0, "has_text": True, "error": None}
    nreplace = _norm(replace)
    try:
        doc = fitz.open(path)
    except Exception as e:
        return {"hits": 0, "has_text": False, "error": str(e)}
    hits, has_text = 0, False
    try:
        for page in doc:
            if not has_text and page.get_text("text").strip():
                has_text = True
            hits += len(_collect_targets(page, nsearch, nreplace, mode))
    finally:
        doc.close()
    return {"hits": hits, "has_text": has_text, "error": None}


def _unique_fontname(stem, used):
    """取一個不與 used(現有資源名集合)衝突的字型參照名,並登記進 used。"""
    nm, i = stem, 0
    while nm in used:
        i += 1
        nm = f"{stem}{i}"
    used.add(nm)
    return nm


def replace_in_doc(fitz, doc, search, replace, font_choice=AUTO_FONT,
                   mode="normal"):
    """就地在已開啟的 doc 取代文字。回傳實際取代的 span 數。

    mode("overwrite"/"fill_empty"):把命中 span 內「搜尋字串到該段結尾」整段
    換成取代字串(fill_empty 只在原本沒有舊值時才動),避免新值疊加在舊值前面。"""
    nsearch = _norm(search)
    if not nsearch:
        return 0
    nreplace = _norm(replace)
    total = 0
    for page in doc:
        targets = _collect_targets(page, nsearch, nreplace, mode)
        if not targets:
            continue
        for t in targets:
            page.add_redact_annot(fitz.Rect(t["bbox"]))
        # 只移除文字;影像與向量線條完全不動
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        # 重新嵌入字型時用「不與頁面現有資源名衝突」的新名稱,
        # 避免沿用先前已子集化(缺字)的同名字型 —— 否則第二次取代會變框框。
        used = {f[4] for f in page.get_fonts()}
        name_cache = {}   # 字型檔 → 本頁使用的 refname
        for t in targets:
            if mode != "normal":
                idx = t["ntext"].find(nsearch)        # 標籤起到段尾整段換掉
                new_text = t["ntext"][:idx] + replace
            else:
                new_text = t["ntext"].replace(nsearch, replace)
            ff = resolve_font_file(t["font"], font_choice)
            if ff:
                nm = name_cache.get(ff)
                if nm is None:
                    nm = _unique_fontname(Path(ff).stem, used)
                    page.insert_font(fontname=nm, fontfile=ff)
                    name_cache[ff] = nm
            else:
                nm = "helv"
            page.insert_text(
                t["origin"], new_text,
                fontname=nm,
                fontsize=t["size"],
                color=_color_to_rgb(t["color"]),
                rotate=_rotate_for_dir(t["dir"]))
            total += 1
    return total


def replace_pdf_file(fitz, src, dst, search, replace, font_choice=AUTO_FONT,
                     mode="normal"):
    """讀 src、取代、寫到 dst。回傳取代的 span 數(0 表示沒命中,呼叫端可選擇不輸出)。

    先把來源讀進記憶體再開檔(stream),避免長期鎖住原檔 —— 這樣 dst 與 src
    相同(就地覆蓋)時也能安全寫回。"""
    doc = fitz.open(stream=Path(src).read_bytes(), filetype="pdf")
    try:
        n = replace_in_doc(fitz, doc, search, replace, font_choice, mode)
        if n:
            try:
                doc.subset_fonts()  # 縮小嵌入字型(失敗不影響結果)
            except Exception:
                pass
            tmp = io.BytesIO()
            doc.save(tmp, garbage=4, deflate=True)
            Path(dst).write_bytes(tmp.getvalue())
        return n
    finally:
        doc.close()


# ===========================================================================
#  GUI
# ===========================================================================

FONT = ("Microsoft JhengHei UI", 12)


class App:
    def __init__(self, root, presets_dir=None, open_in_viewer=None,
                 open_bytes_in_viewer=None):
        """root 可為 tk.Tk / Toplevel / Frame(嵌入用)。
        presets_dir: 指定預設檔資料夾;None 時放在 app_dir()。
        open_in_viewer: 回呼 fn(path);把檔案丟到「檢視 PDF」開啟(看原始檔)。
        open_bytes_in_viewer: 回呼 fn(data, name);以記憶體 bytes 在檢視器開啟
                              (看取代後預覽,不落地)。兩者獨立執行時為 None,
                              改用系統預設程式開檔。"""
        self.root = root
        self.open_in_viewer = open_in_viewer
        self.open_bytes_in_viewer = open_bytes_in_viewer
        if isinstance(root, (tk.Tk, tk.Toplevel)):
            self.root.title("PDF 文字取代")
            self.root.geometry("1040x780")
            self.root.minsize(940, 640)
            try:
                self.root.state("zoomed")
            except tk.TclError:
                try:
                    self.root.attributes("-zoomed", True)
                except tk.TclError:
                    pass

        self.fitz = None
        self.folder = None
        # 掃描結果:list of {path, name, hits, has_text, error}
        self.results = []

        # 嵌入時不動全域 ttk 樣式/字型(同 splitter.py 處理)
        if isinstance(root, (tk.Tk, tk.Toplevel)):
            self._setup_style()
        self._build_ui()

    # ---- 樣式 ----
    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.option_add("*Font", FONT)
        style.configure("TNotebook.Tab", padding=[18, 8], font=FONT)
        style.configure("TLabelframe", padding=8)
        style.configure("TLabelframe.Label",
                        font=("Microsoft JhengHei UI", 12, "bold"))
        style.configure("TButton", padding=[10, 4])
        style.configure("Accent.TButton", padding=[20, 6],
                        font=("Microsoft JhengHei UI", 12, "bold"))

    # ---- UI ----
    def _build_ui(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.tab_main = ttk.Frame(self.notebook)
        self.tab_help = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_main, text="取代")
        self._build_view_tab()                    # 「檢視」分頁(取代與說明之間)
        self.notebook.add(self.tab_help, text="說明")
        self._build_main_tab()
        self._build_help_tab()

    def _build_view_tab(self):
        self.tab_view = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_view, text="檢視")
        self._view_body = None
        self._view_viewers = []
        self._view_paned = None
        self._view_sync_lock = False      # 同步時的防迴圈鎖(兩邊互推保護)
        self._view_hint = ttk.Label(
            self.tab_view,
            text="在「取代」分頁的掃描結果上按右鍵 →「檢視」。\n"
                 "未掃描或未命中只顯示原始檔；命中則左右並排原始與取代後預覽。",
            foreground="#888", justify="left")
        self._view_hint.pack(expand=True)

    def _build_main_tab(self):
        f = self.tab_main

        # 1. 來源(資料夾或單一檔案)
        fbox = ttk.LabelFrame(f, text="1. 選擇來源（資料夾批次，或單一 PDF 檔）")
        fbox.pack(fill="x", padx=8, pady=(8, 4))
        g1 = ttk.Frame(fbox); g1.pack(fill="x", padx=4, pady=4)
        ttk.Label(g1, text="來源：").grid(row=0, column=0, sticky="w", pady=3)
        self.var_folder = tk.StringVar()
        ttk.Entry(g1, textvariable=self.var_folder).grid(
            row=0, column=1, sticky="we", padx=4, pady=3)
        src_btns = ttk.Frame(g1)
        src_btns.grid(row=0, column=2, sticky="w")
        ttk.Button(src_btns, text="選資料夾", command=self._pick_folder).pack(
            side="left")
        ttk.Button(src_btns, text="選檔案", command=self._pick_file).pack(
            side="left", padx=(4, 0))
        self.lbl_filter = ttk.Label(g1, text="檔名篩選：")
        self.lbl_filter.grid(row=1, column=0, sticky="w", pady=3)
        self.var_filter = tk.StringVar()
        self.ent_filter = ttk.Entry(g1, textvariable=self.var_filter)
        self.ent_filter.grid(row=1, column=1, sticky="we", padx=4, pady=3)
        self.ent_filter.bind("<KeyRelease>", lambda _e: self._on_folder_changed())
        self.lbl_filter_hint = ttk.Label(
            g1, text="只處理檔名包含此文字的 PDF（留空＝資料夾內全部）",
            foreground="#666")
        self.lbl_filter_hint.grid(row=2, column=1, sticky="w", padx=4)
        ttk.Label(g1, text="輸出資料夾：").grid(row=3, column=0, sticky="w", pady=3)
        self.var_outfolder = tk.StringVar()
        ttk.Entry(g1, textvariable=self.var_outfolder).grid(
            row=3, column=1, sticky="we", padx=4, pady=3)
        ttk.Button(g1, text="瀏覽…", command=self._pick_outfolder).grid(
            row=3, column=2, pady=3)
        ttk.Label(g1, text="留空＝直接覆蓋原檔（無法復原）",
                  foreground="#a60").grid(row=4, column=1, sticky="w", padx=4)
        g1.columnconfigure(1, weight=1)

        # 2. 搜尋 / 取代
        sbox = ttk.LabelFrame(f, text="2. 搜尋與取代內容")
        sbox.pack(fill="x", padx=8, pady=4)
        g = ttk.Frame(sbox); g.pack(fill="x", padx=4, pady=4)
        ttk.Label(g, text="搜尋文字：").grid(row=0, column=0, sticky="w", pady=3)
        self.var_search = tk.StringVar()
        ttk.Entry(g, textvariable=self.var_search, width=52).grid(
            row=0, column=1, sticky="we", padx=4, pady=3)
        ttk.Label(g, text="取代為：").grid(row=1, column=0, sticky="w", pady=3)
        self.var_replace = tk.StringVar()
        ttk.Entry(g, textvariable=self.var_replace, width=52).grid(
            row=1, column=1, sticky="we", padx=4, pady=3)
        ttk.Label(g, text="重寫字型：").grid(row=2, column=0, sticky="w", pady=3)
        self.var_font = tk.StringVar(value=AUTO_FONT)
        self.cmb_font = ttk.Combobox(
            g, textvariable=self.var_font, state="readonly",
            width=28, values=available_fonts())
        self.cmb_font.grid(row=2, column=1, sticky="w", padx=4, pady=3)
        g.columnconfigure(1, weight=1)
        ttk.Label(
            sbox,
            text="提示：自動忽略全形／半形差異（全形「：」與半形「:」視為相同）。",
            foreground="#666").pack(anchor="w", padx=8, pady=(0, 4))
        # 「標籤：值」欄位處理方式(三選一)
        self.var_field_mode = tk.StringVar(value="normal")
        mbox = ttk.Frame(sbox)
        mbox.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(mbox, text="欄位處理：").pack(side="left")
        for val, label in (
                ("normal", "一般取代"),
                ("overwrite", "取代至文字結尾"),
                ("fill_empty", "排除非文字結尾")):
            ttk.Radiobutton(
                mbox, text=label, value=val, variable=self.var_field_mode,
                command=self._on_folder_changed).pack(side="left", padx=(0, 10))

        # 3. 掃描
        act = ttk.Frame(f); act.pack(fill="x", padx=8, pady=2)
        ttk.Button(act, text="掃描預覽", style="Accent.TButton",
                   command=self._scan).pack(side="left")
        self.lbl_status = ttk.Label(act, text="")
        self.lbl_status.pack(side="left", padx=12)

        # 4. 輸出(釘最底)
        out = ttk.LabelFrame(f, text="4. 輸出")
        out.pack(side="bottom", fill="x", padx=8, pady=(4, 8))
        ttk.Button(out, text="執行取代", style="Accent.TButton",
                   command=self._run_replace).pack(side="left", padx=4, pady=6)
        self.lbl_outhint = ttk.Label(
            out,
            text="輸出到上方「輸出資料夾」；留空則覆蓋原檔。只會輸出有命中的檔案。",
            foreground="#666")
        self.lbl_outhint.pack(side="left", padx=8)

        # 預覽表
        prev = ttk.LabelFrame(f, text="3. 掃描結果")
        prev.pack(fill="both", expand=True, padx=8, pady=4)
        ttk.Label(prev,
                  text="提示：在檔名上按右鍵 →「檢視」。未命中只顯示原始檔；"
                       "命中則左右並排原始與取代後預覽。",
                  foreground="#666").pack(anchor="w", padx=8, pady=(2, 0))
        cols = ("name", "hits", "status")
        self.tree = ttk.Treeview(prev, columns=cols, show="headings", height=10)
        self.tree.heading("name", text="檔名")
        self.tree.heading("hits", text="命中")
        self.tree.heading("status", text="狀態")
        self.tree.column("name", width=560, anchor="w")
        self.tree.column("hits", width=80, anchor="center")
        self.tree.column("status", width=220, anchor="w")
        self.tree.tag_configure("hit", foreground="#080")
        self.tree.tag_configure("miss", foreground="#888")
        self.tree.tag_configure("warn", foreground="#a60")
        vsb = ttk.Scrollbar(prev, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        vsb.pack(side="left", fill="y", pady=4)
        # 右鍵選單:單一「檢視」(自動判斷只看原始或並排比對)
        self.row_menu = tk.Menu(self.tree, tearoff=0)
        self.row_menu.add_command(label="檢視", command=self._view_selected)
        self.tree.bind("<Button-3>", self._on_row_rightclick)

    # ----------------------------------------------------------------- 資料夾
    def _pick_folder(self):
        d = filedialog.askdirectory(title="選擇要批次處理的資料夾")
        if not d:
            return
        self.var_folder.set(d)
        self._on_folder_changed()

    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="選擇單一 PDF 檔",
            filetypes=[("PDF 檔", "*.pdf"), ("所有檔案", "*.*")])
        if not p:
            return
        self.var_folder.set(p)
        self._on_folder_changed()

    def _pick_outfolder(self):
        d = filedialog.askdirectory(title="選擇輸出資料夾（留空則覆蓋原檔）")
        if d:
            self.var_outfolder.set(d)

    def _list_pdfs(self):
        """來源可為資料夾(批次)或單一 PDF 檔。回傳要處理的 PDF 路徑清單。"""
        src = self.var_folder.get().strip()
        if not src:
            return []
        p = Path(src)
        if p.is_file():                       # 單檔:不套用檔名篩選
            return [p] if p.suffix.lower() == ".pdf" else []
        if not p.is_dir():
            return []
        pdfs = sorted(x for x in p.glob("*.pdf") if x.is_file())
        # 檔名篩選(僅資料夾):留空＝全部;否則只留檔名包含關鍵字者。
        # 以 NFKC + 小寫比對,忽略全形/半形與英文大小寫差異。
        kw = _norm(self.var_filter.get()).strip().lower()
        if kw:
            pdfs = [x for x in pdfs if kw in _norm(x.name).lower()]
        return pdfs

    def _update_filter_visibility(self):
        """檔名篩選只在「來源為資料夾(或尚未選擇)」時顯示;選單一檔案時隱藏。"""
        src = self.var_folder.get().strip()
        is_file = bool(src) and os.path.isfile(src)
        widgets = (self.lbl_filter, self.ent_filter, self.lbl_filter_hint)
        for w in widgets:
            (w.grid_remove() if is_file else w.grid())

    def _on_folder_changed(self):
        self._update_filter_visibility()
        # 換來源 / 改篩選 → 先把「會處理的 PDF」以待掃描狀態列出
        self.results = [{
            "path": str(p), "name": p.name,
            "hits": None, "has_text": None, "error": None, "scanned": False,
        } for p in self._list_pdfs()]
        self._reload_tree()
        n = len(self.results)
        self.lbl_status.config(text=f"待掃描：{n} 個檔案" if n else "")

    # ----------------------------------------------------------------- 掃描
    def _scan(self):
        pdfs = self._list_pdfs()
        if not pdfs:
            messagebox.showwarning("提示", "請先選擇來源（資料夾或 PDF 檔）")
            return
        search = self.var_search.get()
        if not _norm(search).strip():
            messagebox.showwarning("提示", "請輸入搜尋文字")
            return
        try:
            fitz = self.fitz or _import_fitz()
            self.fitz = fitz
        except Exception as e:
            messagebox.showerror("錯誤", str(e))
            return

        self.results = []
        self.lbl_status.config(text="掃描中…")
        self.root.update_idletasks()
        replace = self.var_replace.get()
        mode = self.var_field_mode.get()
        for p in pdfs:
            r = scan_pdf(fitz, str(p), search, replace, mode)
            self.results.append({
                "path": str(p),
                "name": p.name,
                "hits": r["hits"],
                "has_text": r["has_text"],
                "error": r["error"],
                "scanned": True,
            })
        self._reload_tree()
        hit_files = sum(1 for r in self.results if r["hits"] > 0)
        total_hits = sum(r["hits"] for r in self.results)
        self.lbl_status.config(
            text=f"掃描完成：{hit_files} 個檔案命中，共 {total_hits} 處")

    def _status_text(self, r):
        if not r.get("scanned"):
            return "miss", "待掃描"
        if r["error"]:
            return "warn", "讀取失敗"
        if r["hits"] and r["hits"] > 0:
            return "hit", f"可取代 {r['hits']} 處"
        if not r["has_text"]:
            return "warn", "無文字（可能為掃描檔）"
        return "miss", "未命中"

    def _reload_tree(self):
        for it in self.tree.get_children():
            self.tree.delete(it)
        # iid 設為 results 索引,雙擊時據此回查該列對應的檔案路徑
        for i, r in enumerate(self.results):
            tag, status = self._status_text(r)
            self.tree.insert("", "end", iid=str(i),
                             values=(r["name"], r["hits"] or "", status),
                             tags=(tag,))

    def _on_row_rightclick(self, event):
        """在某列上按右鍵:先選取該列,再彈出選單。空白處則不顯示。"""
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        try:
            self.row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.row_menu.grab_release()

    def _selected_result(self):
        """回傳右鍵/選取列對應的 result dict;沒有則 None。"""
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return self.results[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def _build_replaced_bytes(self, path):
        """以目前設定在記憶體產生取代後的 PDF。回傳 (bytes, 取代處數)。"""
        fitz = self.fitz or _import_fitz()
        self.fitz = fitz
        doc = fitz.open(stream=Path(path).read_bytes(), filetype="pdf")
        try:
            n = replace_in_doc(
                fitz, doc, self.var_search.get(), self.var_replace.get(),
                self.var_font.get(), self.var_field_mode.get())
            if n:
                try:
                    doc.subset_fonts()
                except Exception:
                    pass
            buf = io.BytesIO()
            doc.save(buf, garbage=4, deflate=True)
            return buf.getvalue(), n
        finally:
            doc.close()

    # ----------------------------------------------------------------- 檢視
    def _view_selected(self, _evt=None):
        """右鍵「檢視」:自動判斷 — 未掃描/未命中只顯示原始;命中則原始+取代後並排。"""
        r = self._selected_result()
        if r is None:
            return
        path = r["path"]
        if not os.path.exists(path):
            messagebox.showwarning("提示", f"找不到檔案：\n{path}")
            return
        try:
            orig = Path(path).read_bytes()       # 以 bytes 顯示,不鎖住原檔
        except Exception as e:
            messagebox.showerror("錯誤", f"無法讀取檔案：\n{e}")
            return
        # 已掃描且命中、且有搜尋字串 → 產生取代後預覽並排;否則只看原始
        dual = (bool(r.get("scanned")) and (r.get("hits") or 0) > 0
                and bool(_norm(self.var_search.get()).strip()))
        data, n = None, 0
        if dual:
            try:
                data, n = self._build_replaced_bytes(path)
            except Exception as e:
                messagebox.showerror("錯誤", f"產生取代預覽失敗：\n{e}")
                data, n = None, 0
            if not n:                            # 實際沒取代到 → 當未命中
                data = None
        self._show_view(r["name"], orig, data, n)

    def _show_view(self, name, orig_bytes, data, n):
        """在「檢視」分頁顯示。data=None → 只顯示原始;否則左原始/右取代後並排。
        共用單一工具列驅動所有面板;有兩面板時捲動同步。"""
        try:
            viewer = _load_viewer_module()
        except Exception as e:
            messagebox.showerror("錯誤", f"無法載入檢視器：\n{e}")
            return
        # 清掉舊內容(銷毀舊檢視器會觸發其關檔)
        if self._view_hint is not None:
            self._view_hint.destroy()
            self._view_hint = None
        if self._view_body is not None:
            self._view_body.destroy()
        body = ttk.Frame(self.tab_view)
        body.pack(fill="both", expand=True)
        self._view_body = body
        self._view_paned = None

        self._build_view_toolbar(body)

        area = ttk.Frame(body)
        area.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        if data is None:
            vf = self._add_view_pane(area, viewer, "原始檔案", "#000")
            self._view_viewers = [vf.app]
            body.after(60, lambda: vf.app.open_bytes(orig_bytes, name))
        else:
            paned = ttk.PanedWindow(area, orient="horizontal")
            paned.pack(fill="both", expand=True)
            self._view_paned = paned
            left = ttk.Frame(paned)
            paned.add(left, weight=1)
            lvf = self._add_view_pane(left, viewer, "原始檔案", "#000")
            right = ttk.Frame(paned)
            paned.add(right, weight=1)
            rvf = self._add_view_pane(
                right, viewer, f"取代後預覽（{n} 處）", "#067")
            lv, rv = lvf.app, rvf.app
            self._view_viewers = [lv, rv]
            # 滾輪捲動:比例同步;點選某邊搜尋結果:另一邊跳到同一頁。皆過防迴圈鎖。
            lv.set_sync(lambda f: self._sync_other(rv, frac=f))
            rv.set_sync(lambda f: self._sync_other(lv, frac=f))
            lv.on_match_jump = lambda p: self._sync_other(rv, page=p)
            rv.on_match_jump = lambda p: self._sync_other(lv, page=p)
            body.after(60, lambda: lv.open_bytes(orig_bytes, name))
            body.after(60, lambda: rv.open_bytes(data, name))
            body.after(160, self._center_view_sash)
        # 工具列顯示由第一個(主)檢視器回報頁碼/縮放
        self._view_viewers[0].on_state = self._refresh_view_toolbar
        self.notebook.select(self.tab_view)

    def _add_view_pane(self, parent, viewer, title, color):
        """在 parent 內建一個面板:標題列(標題 +「🔍 搜尋」鈕)+ 檢視器。
        搜尋面板預設隱藏,按標題旁的搜尋鈕才切換顯示。回傳檢視器 frame。"""
        head = ttk.Frame(parent)
        head.pack(fill="x", padx=6, pady=(2, 0))
        ttk.Label(head, text=title,
                  font=("Microsoft JhengHei UI", 12, "bold"),
                  foreground=color).pack(side="left")
        vf = viewer.create_frame(parent, show_toolbar=False)
        vf.pack(fill="both", expand=True)
        ttk.Button(head, text="🔍 搜尋", width=8,
                   command=vf.app.toggle_search).pack(side="left", padx=(8, 0))
        return vf

    def _build_view_toolbar(self, parent):
        """單一共用工具列,操作會套用到所有面板(原始 + 取代後)。無「開啟」鈕。"""
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Button(bar, text="◀ 上一頁",
                   command=lambda: self._view_each("prev_page")).pack(side="left")
        self._view_page = tk.StringVar(value="1")
        ent = ttk.Entry(bar, textvariable=self._view_page, width=5,
                        justify="center")
        ent.pack(side="left", padx=(6, 2))
        ent.bind("<Return>", self._view_goto_page)
        self._view_total = ttk.Label(bar, text="/ 0")
        self._view_total.pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="下一頁 ▶",
                   command=lambda: self._view_each("next_page")).pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(bar, text="－", width=3,
                   command=lambda: self._view_each("zoom_out")).pack(side="left")
        self._view_zoom = ttk.Label(bar, text="100%", width=6, anchor="center")
        self._view_zoom.pack(side="left", padx=2)
        ttk.Button(bar, text="＋", width=3,
                   command=lambda: self._view_each("zoom_in")).pack(side="left")
        ttk.Button(bar, text="適合寬度",
                   command=lambda: self._view_each("fit_width")).pack(
                       side="left", padx=(6, 2))
        ttk.Button(bar, text="適合頁面",
                   command=lambda: self._view_each("fit_page")).pack(side="left")
        ttk.Label(bar, text="（翻頁／縮放同步兩邊；搜尋請按各面板標題旁的「🔍 搜尋」）",
                  foreground="#888").pack(side="left", padx=10)

    def _sync_other(self, target, frac=None, page=None):
        """把另一面板同步到指定位置(page 優先)。防迴圈鎖避免兩邊互相觸發。"""
        if self._view_sync_lock:
            return
        self._view_sync_lock = True
        try:
            if page is not None:
                target.sync_to_page(page)
            elif frac is not None:
                target.sync_to(frac)
        finally:
            self._view_sync_lock = False

    def _view_each(self, method):
        for v in self._view_viewers:
            try:
                getattr(v, method)()
            except Exception:
                pass

    def _view_goto_page(self, _evt=None):
        try:
            idx = int(self._view_page.get()) - 1
        except ValueError:
            return
        for v in self._view_viewers:
            v.show_page(idx)

    def _refresh_view_toolbar(self):
        if not self._view_viewers:
            return
        m = self._view_viewers[0]
        if m.doc is None:
            return
        self._view_page.set(str(m.page_index + 1))
        self._view_total.config(text=f"/ {len(m.doc)}")
        self._view_zoom.config(text=f"{round(m.scale * 100)}%")

    def _center_view_sash(self):
        try:
            if self._view_paned is not None:
                self._view_paned.sashpos(
                    0, max(200, self._view_paned.winfo_width() // 2))
        except Exception:
            pass

    # ----------------------------------------------------------------- 取代
    def _run_replace(self):
        if not self.results:
            messagebox.showwarning("提示", "請先選擇來源")
            return
        if not all(r.get("scanned") for r in self.results):
            messagebox.showwarning("提示", "請先「掃描預覽」")
            return
        hits = [r for r in self.results if (r["hits"] or 0) > 0]
        if not hits:
            messagebox.showinfo("提示", "沒有任何檔案命中搜尋文字，無可輸出。")
            return
        search = self.var_search.get()
        replace = self.var_replace.get()
        font_choice = self.var_font.get()
        mode = self.var_field_mode.get()

        # 來源可能是資料夾或單一檔案 → 取其所在資料夾作為「就地覆蓋」目標
        src = self.var_folder.get().strip()
        src_dir = src if os.path.isdir(src) else os.path.dirname(src)
        out_dir = self.var_outfolder.get().strip()
        if not out_dir:
            out_dir = src_dir         # 留空 → 就地覆蓋原檔
        # 與來源相同(含留空)時,會覆蓋原檔,先二次確認
        same = (os.path.normcase(os.path.abspath(out_dir)) ==
                os.path.normcase(os.path.abspath(src_dir)))
        if same:
            if not messagebox.askyesno(
                    "確認覆蓋",
                    "輸出資料夾與來源相同，將直接覆蓋原始檔案且無法復原。\n"
                    "確定要繼續嗎？"):
                return
        else:
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception as e:
                messagebox.showerror("錯誤", f"無法建立輸出資料夾：\n{e}")
                return

        fitz = self.fitz
        done, failed = 0, []
        self.lbl_status.config(text="處理中…")
        self.root.update_idletasks()
        for r in hits:
            dst = os.path.join(out_dir, r["name"])
            try:
                n = replace_pdf_file(
                    fitz, r["path"], dst, search, replace, font_choice, mode)
                if n:
                    done += 1
            except Exception as e:
                failed.append(f"{r['name']}：{e}")
        msg = f"完成：已輸出 {done} 個檔案到\n{out_dir}"
        if failed:
            msg += "\n\n失敗 {} 個：\n".format(len(failed)) + "\n".join(failed[:8])
            if len(failed) > 8:
                msg += f"\n…等共 {len(failed)} 個"
        (messagebox.showwarning if failed else messagebox.showinfo)("結果", msg)
        self.lbl_status.config(text=f"輸出完成：{done} 個檔案")

    # ----------------------------------------------------------------- 說明
    def _build_help_tab(self):
        txt = tk.Text(self.tab_help, wrap="word", font=FONT,
                      relief="flat", padx=12, pady=10)
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("1.0", HELP_TEXT)
        txt.config(state="disabled")


HELP_TEXT = """PDF 文字取代 — 使用說明

操作流程
  1. 選「來源」：可選整個資料夾(批次)或單一 PDF 檔；
     資料夾可用「檔名篩選」只處理檔名含指定文字的 PDF；
     要保留原檔就另選「輸出資料夾」，留空則覆蓋原檔
  2. 填「搜尋文字」與「取代為」，視需要選重寫字型
  3. 「掃描預覽」→ 看每個檔案命中幾處、狀態
     （在結果列上按右鍵 →「檢視」：未命中只顯示原始檔；命中則在「檢視」分頁
       左右並排原始與取代後預覽，捲動同步、共用一個工具列）
  4. 「執行取代」→ 輸出到上方指定的資料夾

說明
  • 自動忽略全形／半形差異：搜尋「列印日期:」也找得到檔案裡的「列印日期：」。
  • 取代後的文字以你輸入的「取代為」原樣寫入，請依需要打全形或半形。
  • 「欄位處理」三種模式 —— 以更新列印日期為例：
      搜尋：列印日期：
      取代：列印日期：115年6月9日

    －一般取代：只把「搜尋字串」本身換掉，後面既有內容會留著。
        原文「列印日期：115年6月2日」
        結果「列印日期：115年6月9日115年6月2日」  ← 舊日期殘留，慎用

    －取代至文字結尾：把「搜尋字串～該段文字結尾」整段換成取代內容。
        原文「列印日期：」          → 結果「列印日期：115年6月9日」
        原文「列印日期：115年6月2日」→ 結果「列印日期：115年6月9日」  ← 舊日期被換掉

    －排除非文字結尾：搜尋字串後面已有內容（非文字結尾）就略過不動；
      只有搜尋字串剛好在該段文字結尾（沒有舊值）時才填入。
        原文「列印日期：」          → 結果「列印日期：115年6月9日」  ← 填入
        原文「列印日期：115年6月2日」→ 不變（保留舊日期）

    注意：「取代至文字結尾／排除非文字結尾」都只處理到「該段文字（text-run）結尾」；
    若標籤與舊值被 PDF 拆成不同段，舊值可能不在同段而無法一起處理，請先用
    「掃描預覽」確認。
  • 重寫字型預設「自動」：盡量沿用原 PDF 的字型；找不到對應系統字型時，
    退而使用系統內建中文字型（新細明體等）。
  • 只輸出有命中的檔案；未命中或無文字（掃描影像）的檔案會跳過並在清單標示。
  • 可重複執行：對「已經取代過」的檔案再跑一次會自動略過（顯示 0 處），
    不會重複疊加或損壞文字。

可處理 / 不可處理
  • 可處理：文字型 PDF（內容可用滑鼠選取的）。直書、頁面旋轉都支援。
  • 不可處理：掃描成影像的 PDF（文字其實是圖片）。清單會標示「無文字（可能為掃描檔）」。
  • 搜尋字串需落在同一段文字（text-run）內；少數 PDF 會把字串拆成多段，
    那種情況該段不會命中。

輸出資料夾
  • 另選一個輸出資料夾：原檔保留，結果以相同檔名寫到該資料夾（不存在會自動建立）。
  • 留空（或與來源相同）：直接覆蓋來源資料夾的原檔，執行前會再次確認、且無法復原。
  • 只會輸出有命中的檔案；未命中的不動。
"""


def create_frame(parent, presets_dir=None, open_in_viewer=None,
                 open_bytes_in_viewer=None):
    """供 PDF 工具集主程式嵌入用。回傳含完整取代 UI 的 Frame。"""
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
