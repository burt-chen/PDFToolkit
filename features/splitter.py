#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 切割工具 (GUI 版)

依四種模式偵測每份文件邊界，預覽（可編輯）後輸出為多個 PDF 或 ZIP。
演算法照搬自 PDF切割.md / backend/api/v1/pdf_split.py，core 與 UI 解耦。
詳細說明見 README.md。
"""

import io
import json
import os
import re
import sys
import unicodedata
import zipfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


# ===========================================================================
#  路徑 / 設定
# ===========================================================================

PRESETS_FILE = "presets.json"
MAX_BYTES = None  # 桌機版不設上限（web 上傳才需要 100MB 限制）


def app_dir():
    """程式所在資料夾。PyInstaller 打包後也能正確指向 .exe 旁邊。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _import_fitz():
    """延遲匯入 PyMuPDF，沒裝時給清楚訊息。"""
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


def _load_viewer_module():
    """載入同目錄的 viewer 模組(供「檢視」分頁預覽切割頁面用)。
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


# ===========================================================================
#  core/detector — 四種偵測（純函式，可單元測試）
# ===========================================================================

def _norm(s: str) -> str:
    """NFKC 正規化。政府謄本常內嵌 CJK 相容表意字元，不正規化會比對不到。"""
    return unicodedata.normalize("NFKC", s or "")


def detect_by_keyword(doc, trigger, name_extract, name_prefix,
                      trigger_pos="start",
                      name_search_keyword="", name_search_extract="line"):
    """模式一：遇到關鍵字切割。每次出現都無條件開新組（同名也不合併）。

    trigger_pos:
      "start" — 關鍵字所在頁為新文件**起始**(預設,原始行為);
               起始之前的頁面被丟棄。
      "end"   — 關鍵字所在頁為當前文件**結束**;下一頁起算新文件;
               結尾之後沒有關鍵字的頁面被丟棄。

    name_extract == "search":
      切完後再到該群組的每一頁中搜尋 name_search_keyword,
      取第一個命中行,以 name_search_extract (before/after/line) 擷取檔名。
      找不到 → 用「未識別」。
    """
    total = len(doc)
    groups, cur_pages, cur_name = [], [], None
    trigger = _norm(trigger)
    for i, page in enumerate(doc):
        text = _norm(page.get_text())
        hit = bool(trigger and trigger in text)
        if hit:
            line = next(
                (ln.strip() for ln in text.splitlines() if trigger in ln),
                trigger,
            )
            if name_extract == "before":
                name = line[: line.index(trigger)].strip()[:100] or trigger
            elif name_extract == "after":
                name = line[line.index(trigger) + len(trigger):].strip()[:100] or trigger
            elif name_extract == "sequential":
                name = f"__seq__{len(groups)}"
            elif name_extract == "search":
                name = "__search__"  # 占位,於下方 post-process 從群組內容擷取
            else:  # line
                name = line[:100]

        if trigger_pos == "end":
            # 關鍵字 = 文件結尾。先把當前頁併入組,再結束此組
            cur_pages.append(i)
            if hit:
                groups.append((name, cur_pages))
                cur_pages, cur_name = [], None
        else:
            # 關鍵字 = 文件起始(原行為)
            if hit:
                if cur_pages:
                    groups.append((cur_name, cur_pages))
                cur_pages, cur_name = [i], name
            else:
                if cur_pages:
                    cur_pages.append(i)
    # 結尾處理:
    # - start: 最後一組若有頁,提交
    # - end:   尾段沒有關鍵字收尾的頁面 → 丟棄(與 start 開頭丟棄對稱)
    if trigger_pos == "start" and cur_pages:
        groups.append((cur_name, cur_pages))
    if name_extract == "sequential":
        groups = [
            (f"{name_prefix}_{idx + 1:03d}", pg)
            for idx, (_, pg) in enumerate(groups)
        ]
    elif name_extract == "search":
        sk = _norm(name_search_keyword)
        renamed = []
        for _, pg in groups:
            picked = None
            if sk:
                for idx in pg:
                    text = _norm(doc[idx].get_text())
                    if sk in text:
                        ln = next((l.strip() for l in text.splitlines()
                                   if sk in l), sk)
                        if name_search_extract == "before":
                            picked = ln[:ln.index(sk)].strip()[:100] or sk
                        elif name_search_extract == "after":
                            picked = ln[ln.index(sk) + len(sk):].strip()[:100] or sk
                        else:  # line
                            picked = ln[:100]
                        break
            renamed.append((picked or "未識別", pg))
        groups = renamed
    return total, groups


def detect_by_pages(doc, pages_per_file, name_prefix):
    """模式二：每 N 頁切一份。"""
    total = len(doc)
    pages_per_file = max(1, int(pages_per_file))
    groups, idx = [], 1
    for start in range(0, total, pages_per_file):
        end = min(start + pages_per_file - 1, total - 1)
        groups.append((f"{name_prefix}_{idx:03d}", list(range(start, end + 1))))
        idx += 1
    return total, groups


def parse_page_splits(input_str, total, prefix="文件"):
    """模式三：頁碼分割點 → 規則清單（1-based）。

    n   → 在第 n 頁之後分割（新文件從 n+1 起）
    n-m → 第 n 頁之前與第 m 頁之後分割（新文件從 n 起，m+1 再起新文件）
    """
    starts = {1}
    for tok in re.split(r"[\s,，、]+", (input_str or "").strip()):
        if not tok:
            continue
        rng = re.match(r"^(\d+)-(\d+)$", tok)
        if rng:
            n, m = int(rng.group(1)), int(rng.group(2))
            if 1 <= n <= total:
                starts.add(n)
            if m + 1 <= total:
                starts.add(m + 1)
        else:
            try:
                n = int(tok)
            except ValueError:
                continue
            if n + 1 <= total:
                starts.add(n + 1)
    s = sorted(starts)
    rules = []
    for i, st in enumerate(s):
        end = (s[i + 1] - 1) if i + 1 < len(s) else total
        rules.append({"name": f"{prefix}_{i + 1:03d}", "start_page": st, "end_page": end})
    return rules


def detect_by_regex(doc, pattern_str):
    """模式四：自訂組合（規則組出的 regex）。沿用『同名相鄰才併組』。"""
    try:
        pattern = re.compile(pattern_str)
    except re.error as e:
        raise ValueError(f"規則格式錯誤：{e}")
    total = len(doc)
    groups, cur_pages, cur_name = [], [], None
    for i, page in enumerate(doc):
        m = pattern.search(_norm(page.get_text()))
        if m:
            caps = [g for g in m.groups() if g]
            name = "".join(caps) if caps else m.group(0)
            if cur_name != name:
                if cur_pages:
                    groups.append((cur_name, cur_pages))
                cur_pages, cur_name = [i], name
            else:
                cur_pages.append(i)
        else:
            if cur_pages:
                cur_pages.append(i)
    if cur_pages:
        groups.append((cur_name, cur_pages))
    return total, groups


def groups_to_rules(groups):
    """偵測結果 → 給 UI 的規則（0-based → 1-based）。"""
    rules = []
    for name, pages in groups:
        if not pages:
            continue
        rules.append({
            "name": name or "未識別",
            "start_page": pages[0] + 1,
            "end_page": pages[-1] + 1,
        })
    return rules


# ===========================================================================
#  core/blocks — 模式四：Block → regex 組裝
# ===========================================================================

# 規則型別 ↔ 中文顯示
BLOCK_TYPE_LABELS = {
    "fixed": "固定文字",
    "chinese": "文字（可指定結尾字）",
    "number": "數字（可含符號）",
    "any": "任意文字",
    "space": "空白",
}


def block_type_label(t):
    return BLOCK_TYPE_LABELS.get(t, t)


def block_type_key(label):
    for k, v in BLOCK_TYPE_LABELS.items():
        if v == label:
            return k
    return label


def _esc(s: str) -> str:
    """regex 跳脫（涵蓋 .*+?^${}()|[]\\ 等）。"""
    return re.escape(s or "")


def block_to_regex(b: dict) -> str:
    """單一規則 → regex 片段。對照 PDF切割.md 第 3 節模式四表格。"""
    t = b.get("type")
    in_fn = bool(b.get("inFilename"))
    optional = bool(b.get("optional"))

    if t == "space":
        return r"\s*"  # 固定，忽略 optional / inFilename

    if t == "fixed":
        v = _esc(b.get("value", ""))
        frag = f"({v})" if in_fn else v
    elif t == "chinese":
        ew = b.get("endsWith") or ""
        if ew:
            frag = rf"(\S+{_esc(ew)})"  # 一律 capture
        else:
            frag = r"([一-鿿]+)"
    elif t == "number":
        syms = b.get("symbols") or ""
        if syms:
            cls = "".join(_esc(c) for c in syms)
            body = rf"\d+(?:[{cls}]\d+)*"
            frag = f"({body})" if in_fn else f"(?:{body})"
        else:
            frag = r"(\d+)"
    elif t == "any":
        frag = "(.+)" if in_fn else ".+"
    else:
        frag = _esc(b.get("value", ""))

    if optional and t != "space":
        frag = f"(?:{frag})?"
    return frag


def blocks_to_pattern(blocks) -> str:
    """各規則片段字串相接成完整 regex。"""
    return "".join(block_to_regex(b) for b in blocks)


# 內建範例：地籍謄本（初始不自動載入，使用者可按鈕帶入）
CADASTRAL_PRESET = {
    "name": "地籍謄本",
    "example": "北屯區旱溪段0001-0002地號",
    "blocks": [
        {"type": "chinese", "value": "", "endsWith": "區", "symbols": "",
         "optional": False, "inFilename": True},
        {"type": "chinese", "value": "", "endsWith": "段", "symbols": "",
         "optional": False, "inFilename": True},
        {"type": "chinese", "value": "", "endsWith": "小段", "symbols": "",
         "optional": True, "inFilename": True},
        {"type": "space", "value": "", "endsWith": "", "symbols": "",
         "optional": False, "inFilename": False},
        {"type": "number", "value": "", "endsWith": "", "symbols": "-",
         "optional": False, "inFilename": True},
        {"type": "fixed", "value": "地號", "endsWith": "", "symbols": "",
         "optional": False, "inFilename": False},
    ],
}


# ===========================================================================
#  core/splitter — 切割並輸出
# ===========================================================================

def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name or "未識別")


def build_outputs(fitz, doc, rules):
    """rules: list of {name, start_page, end_page} (1-based)。
    回傳 list of (filename, pdf_bytes)。同名去重：名稱.pdf、名稱_1.pdf…"""
    out, name_count = [], {}
    for r in rules:
        new_pdf = fitz.open()
        start = max(0, int(r["start_page"]) - 1)
        end = min(len(doc) - 1, int(r["end_page"]) - 1)
        for p in range(start, end + 1):
            new_pdf.insert_pdf(doc, from_page=p, to_page=p)
        buf = io.BytesIO()
        new_pdf.save(buf)
        new_pdf.close()
        safe = _safe_name(r["name"])
        c = name_count.get(safe, 0)
        name_count[safe] = c + 1
        fname = f"{safe}.pdf" if c == 0 else f"{safe}_{c}.pdf"
        out.append((fname, buf.getvalue()))
    return out


# ===========================================================================
#  core/presets — 自訂組合預設的 JSON 讀寫
# ===========================================================================

class PresetStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.presets = self._load()

    def _load(self):
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
        except Exception:
            return []
        # 檔案不存在 → 首次執行,內建「地籍謄本」預設(之後使用者可自行刪改)
        seed = [{
            "name": CADASTRAL_PRESET["name"],
            "blocks": [dict(b) for b in CADASTRAL_PRESET["blocks"]],
            "example": CADASTRAL_PRESET["example"],
        }]
        self.presets = seed
        self.save()
        return seed

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.presets, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        except Exception:
            return False

    def names(self):
        return [p.get("name", "") for p in self.presets]

    def get(self, name):
        for p in self.presets:
            if p.get("name") == name:
                return p
        return None

    def upsert(self, name, blocks, example):
        entry = {"name": name, "blocks": blocks, "example": example}
        for i, p in enumerate(self.presets):
            if p.get("name") == name:
                self.presets[i] = entry
                return self.save()
        self.presets.append(entry)
        return self.save()

    def delete(self, name):
        before = len(self.presets)
        self.presets = [p for p in self.presets if p.get("name") != name]
        if len(self.presets) != before:
            return self.save()
        return False


# ===========================================================================
#  GUI
# ===========================================================================

FONT = ("Microsoft JhengHei UI", 12)


class App:
    def __init__(self, root, presets_dir=None):
        """root 可為 tk.Tk / Toplevel / Frame(嵌入用)。
        presets_dir: 指定預設檔資料夾;None 時放在 app_dir()。"""
        self.root = root
        if isinstance(root, (tk.Tk, tk.Toplevel)):
            self.root.title("PDF 切割工具")
            self.root.geometry("1040x820")
            self.root.minsize(940, 680)
            try:
                self.root.state("zoomed")
            except tk.TclError:
                try:
                    self.root.attributes("-zoomed", True)
                except tk.TclError:
                    pass

        base = Path(presets_dir) if presets_dir else app_dir()
        self.presets_path = base / PRESETS_FILE
        self.fitz = None
        self.doc = None
        self.pdf_path = None
        self.total_pages = 0
        self.rules = []  # list of {name, start_page, end_page}

        self.presets = PresetStore(self.presets_path)
        self._edit_widget = None

        # 嵌入(root 是 Frame)時不動全域 ttk 樣式/字型,否則 theme_use 與
        # option_add("*Font") 會擴散到 launcher 與其他工具的 UI
        if isinstance(root, (tk.Tk, tk.Toplevel)):
            self._setup_style()
        self._build_ui()
        self._refresh_preset_combo()

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
        style.configure("TLabelframe.Label", font=("Microsoft JhengHei UI", 12, "bold"))
        style.configure("TButton", padding=[10, 4])
        style.configure("Accent.TButton", padding=[20, 6],
                        font=("Microsoft JhengHei UI", 12, "bold"))

    # ---- UI ----
    def _build_ui(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self.tab_main = ttk.Frame(self.notebook)
        self.tab_help = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_main, text="切割")
        self._build_view_tab()                 # 「檢視」分頁(切割與說明之間)
        self.notebook.add(self.tab_help, text="說明")

        self._build_main_tab()
        self._build_help_tab()

    def _build_view_tab(self):
        self.tab_view = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_view, text="檢視")
        self._view_body = None
        self._view_hint = ttk.Label(
            self.tab_view,
            text="在「切割」分頁的預覽結果上按右鍵 →「檢視」，"
                 "即可在此預覽該份切割的頁面。",
            foreground="#888")
        self._view_hint.pack(expand=True)

    # ----------------------------------------------------------------- 主分頁
    def _build_main_tab(self):
        f = self.tab_main

        # 檔案選擇
        file_box = ttk.LabelFrame(f, text="1. 選擇 PDF")
        file_box.pack(fill="x", padx=8, pady=(8, 4))
        self.var_pdf = tk.StringVar()
        ttk.Entry(file_box, textvariable=self.var_pdf).pack(
            side="left", fill="x", expand=True, padx=(4, 6), pady=4)
        ttk.Button(file_box, text="瀏覽…", command=self._pick_pdf).pack(
            side="left", padx=4, pady=4)
        self.lbl_pages = ttk.Label(file_box, text="")
        self.lbl_pages.pack(side="left", padx=8)

        # 模式選擇
        mode_box = ttk.LabelFrame(f, text="2. 切割方式")
        mode_box.pack(fill="x", padx=8, pady=4)
        self.var_mode = tk.StringVar(value="keyword")
        modes = [
            ("keyword", "遇到關鍵字切割"),
            ("pages", "每固定頁數切割"),
            ("page_ranges", "頁碼切割"),
            ("custom", "自訂組合規則"),
        ]
        row = ttk.Frame(mode_box)
        row.pack(fill="x", padx=4, pady=2)
        for val, txt in modes:
            ttk.Radiobutton(row, text=txt, value=val, variable=self.var_mode,
                            command=self._switch_mode).pack(side="left", padx=(0, 16))

        # 各模式設定容器
        self.opt_container = ttk.Frame(mode_box)
        self.opt_container.pack(fill="both", expand=True, padx=4, pady=(4, 6))
        self._build_opt_keyword()
        self._build_opt_pages()
        self._build_opt_ranges()
        self._build_opt_custom()
        self._switch_mode()

        # 偵測按鈕
        act = ttk.Frame(f)
        act.pack(fill="x", padx=8, pady=2)
        ttk.Button(act, text="開始偵測", style="Accent.TButton",
                   command=self._run_detect).pack(side="left")
        self.lbl_status = ttk.Label(act, text="")
        self.lbl_status.pack(side="left", padx=12)

        # 輸出（先建並釘在最底,確保任何模式下都看得到）
        out = ttk.LabelFrame(f, text="4. 輸出")
        out.pack(side="bottom", fill="x", padx=8, pady=(4, 8))
        ttk.Button(out, text="輸出到資料夾（多個 PDF）", style="Accent.TButton",
                   command=self._output_folder).pack(side="left", padx=4, pady=6)
        ttk.Button(out, text="打包成 ZIP", command=self._output_zip).pack(
            side="left", padx=4, pady=6)

        # 預覽表
        prev = ttk.LabelFrame(
            f, text="3. 預覽結果（雙擊欄位可編輯；右鍵可檢視該份頁面）")
        prev.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("name", "start", "end")
        self.tree = ttk.Treeview(prev, columns=cols, show="headings", height=8)
        self.tree.heading("name", text="名稱（檔名）")
        self.tree.heading("start", text="起始頁")
        self.tree.heading("end", text="結束頁")
        self.tree.column("name", width=560, anchor="w")
        self.tree.column("start", width=90, anchor="center")
        self.tree.column("end", width=90, anchor="center")
        vsb = ttk.Scrollbar(prev, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        vsb.pack(side="left", fill="y", pady=4)
        self.tree.bind("<Double-1>", self._on_tree_dblclick)
        # 右鍵選單:檢視該份切割的頁面
        self.row_menu = tk.Menu(self.tree, tearoff=0)
        self.row_menu.add_command(label="檢視", command=self._view_selected_split)
        self.tree.bind("<Button-3>", self._on_tree_rightclick)

        btns = ttk.Frame(prev)
        btns.pack(side="left", fill="y", padx=6, pady=4)
        ttk.Button(btns, text="新增一筆", command=self._rule_add).pack(fill="x", pady=2)
        ttk.Button(btns, text="刪除選取", command=self._rule_del).pack(fill="x", pady=2)
        ttk.Button(btns, text="清空", command=self._rule_clear).pack(fill="x", pady=2)

    # ---- 各模式設定面板 ----
    def _build_opt_keyword(self):
        fr = ttk.Frame(self.opt_container)
        self.opt_keyword = fr
        r = ttk.Frame(fr); r.pack(fill="x", pady=3)
        ttk.Label(r, text="觸發關鍵字：").pack(side="left")
        self.var_kw = tk.StringVar()
        ttk.Entry(r, textvariable=self.var_kw, width=30).pack(side="left", padx=4)
        rp = ttk.Frame(fr); rp.pack(fill="x", pady=3)
        ttk.Label(rp, text="關鍵字位置：").pack(side="left")
        self.var_kw_pos = tk.StringVar(value="start")
        for val, txt in [("start", "在文件起始頁"), ("end", "在文件結束頁")]:
            ttk.Radiobutton(rp, text=txt, value=val,
                            variable=self.var_kw_pos).pack(side="left", padx=(0, 12))
        r2 = ttk.Frame(fr); r2.pack(fill="x", pady=3)
        ttk.Label(r2, text="檔名來源：").pack(side="left")
        self.var_kw_extract = tk.StringVar(value="line")
        for val, txt in [("before", "關鍵字前"), ("after", "關鍵字後"),
                         ("line", "整行"), ("sequential", "流水號"),
                         ("search", "群組內搜尋")]:
            ttk.Radiobutton(r2, text=txt, value=val,
                            variable=self.var_kw_extract,
                            command=self._toggle_kw_search).pack(
                side="left", padx=(0, 12))
        # 「流水號」副欄位(預設隱藏,選到該選項才顯示)
        self.opt_kw_seq = ttk.Frame(fr)
        ttk.Label(self.opt_kw_seq, text="流水號前綴：").pack(side="left")
        self.var_kw_prefix = tk.StringVar(value="文件")
        ttk.Entry(self.opt_kw_seq, textvariable=self.var_kw_prefix,
                  width=16).pack(side="left", padx=4)

        # 「群組內搜尋」副欄位(預設隱藏,選到該選項才顯示)
        self.opt_kw_search = ttk.Frame(fr)
        ttk.Label(self.opt_kw_search, text="檔名關鍵字：").pack(side="left")
        self.var_kw_search_kw = tk.StringVar()
        ttk.Entry(self.opt_kw_search, textvariable=self.var_kw_search_kw,
                  width=22).pack(side="left", padx=4)
        ttk.Label(self.opt_kw_search, text="    擷取：").pack(side="left")
        self.var_kw_search_extract = tk.StringVar(value="line")
        for val, txt in [("before", "關鍵字前"), ("after", "關鍵字後"),
                         ("line", "整行")]:
            ttk.Radiobutton(self.opt_kw_search, text=txt, value=val,
                            variable=self.var_kw_search_extract).pack(
                side="left", padx=(0, 8))

    def _toggle_kw_search(self):
        ex = self.var_kw_extract.get()
        if ex == "sequential":
            self.opt_kw_seq.pack(fill="x", pady=3)
        else:
            self.opt_kw_seq.pack_forget()
        if ex == "search":
            self.opt_kw_search.pack(fill="x", pady=3)
        else:
            self.opt_kw_search.pack_forget()

    def _build_opt_pages(self):
        fr = ttk.Frame(self.opt_container)
        self.opt_pages = fr
        r = ttk.Frame(fr); r.pack(fill="x", pady=3)
        ttk.Label(r, text="每幾頁切一份：").pack(side="left")
        self.var_ppf = tk.StringVar(value="1")
        ttk.Spinbox(r, from_=1, to=9999, textvariable=self.var_ppf,
                    width=8).pack(side="left", padx=4)
        ttk.Label(r, text="    前綴：").pack(side="left")
        self.var_pg_prefix = tk.StringVar(value="文件")
        ttk.Entry(r, textvariable=self.var_pg_prefix, width=16).pack(side="left", padx=4)

    def _build_opt_ranges(self):
        fr = ttk.Frame(self.opt_container)
        self.opt_ranges = fr
        r = ttk.Frame(fr); r.pack(fill="x", pady=3)
        ttk.Label(r, text="分割點：").pack(side="left")
        self.var_ranges = tk.StringVar()
        ttk.Entry(r, textvariable=self.var_ranges, width=44).pack(side="left", padx=4)
        ttk.Label(r, text="    前綴：").pack(side="left")
        self.var_rg_prefix = tk.StringVar(value="文件")
        ttk.Entry(r, textvariable=self.var_rg_prefix, width=14).pack(side="left", padx=4)
        ttk.Label(fr, text="例：3,7 → 1-3、4-7、8-末；3-8 → 1-2、3-8、9-末",
                  foreground="#666").pack(anchor="w", pady=(2, 0))

    def _build_opt_custom(self):
        fr = ttk.Frame(self.opt_container)
        self.opt_custom = fr

        # 規則清單
        top = ttk.Frame(fr); top.pack(fill="both", expand=True)
        lb_box = ttk.Frame(top); lb_box.pack(side="left", fill="both", expand=True)
        self.blk_tree = ttk.Treeview(
            lb_box, columns=("desc",), show="headings", height=6)
        self.blk_tree.heading("desc", text="規則（由上到下相接成完整比對規則）")
        self.blk_tree.column("desc", width=560, anchor="w")
        self.blk_tree.pack(side="left", fill="both", expand=True)
        bsb = ttk.Scrollbar(lb_box, orient="vertical", command=self.blk_tree.yview)
        self.blk_tree.configure(yscrollcommand=bsb.set)
        bsb.pack(side="left", fill="y")

        bb = ttk.Frame(top); bb.pack(side="left", fill="y", padx=6)
        ttk.Button(bb, text="新增規則", command=self._blk_add).pack(fill="x", pady=2)
        ttk.Button(bb, text="編輯", command=self._blk_edit).pack(fill="x", pady=2)
        ttk.Button(bb, text="刪除", command=self._blk_del).pack(fill="x", pady=2)
        ttk.Button(bb, text="↑", command=lambda: self._blk_move(-1)).pack(fill="x", pady=2)
        ttk.Button(bb, text="↓", command=lambda: self._blk_move(1)).pack(fill="x", pady=2)
        ttk.Button(bb, text="清空規則", command=self._blk_clear).pack(fill="x", pady=2)

        self.blocks = []  # list of Block dict

        # 規則預覽 + 測試
        mid = ttk.Frame(fr); mid.pack(fill="x", pady=(6, 2))
        ttk.Label(mid, text="組出的規則：").pack(side="left")
        self.var_pattern = tk.StringVar()
        ttk.Entry(mid, textvariable=self.var_pattern, state="readonly",
                  width=60).pack(side="left", fill="x", expand=True, padx=4)

        t = ttk.Frame(fr); t.pack(fill="x", pady=2)
        ttk.Label(t, text="測試文字：").pack(side="left")
        self.var_test = tk.StringVar()
        ttk.Entry(t, textvariable=self.var_test, width=44).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(t, text="測試", command=self._blk_test).pack(side="left", padx=4)
        self.lbl_test = ttk.Label(t, text="", foreground="#666")
        self.lbl_test.pack(side="left", padx=6)

        # 預設管理
        pset = ttk.LabelFrame(fr, text="自訂組合預設")
        pset.pack(fill="x", pady=(6, 2))
        pr = ttk.Frame(pset); pr.pack(fill="x", pady=3)
        self.var_preset = tk.StringVar()
        self.cmb_preset = ttk.Combobox(pr, textvariable=self.var_preset,
                                       state="readonly", width=24)
        self.cmb_preset.pack(side="left", padx=4)
        ttk.Button(pr, text="套用", command=self._preset_apply).pack(side="left", padx=2)
        ttk.Button(pr, text="覆蓋此預設", command=self._preset_overwrite).pack(
            side="left", padx=2)
        ttk.Button(pr, text="刪除", command=self._preset_delete).pack(side="left", padx=2)
        pr2 = ttk.Frame(pset); pr2.pack(fill="x", pady=3)
        ttk.Label(pr2, text="另存新設定名稱：").pack(side="left")
        self.var_new_preset = tk.StringVar()
        ttk.Entry(pr2, textvariable=self.var_new_preset, width=24).pack(
            side="left", padx=4)
        ttk.Button(pr2, text="另存新設定", command=self._preset_save_new).pack(
            side="left", padx=2)
        pr3 = ttk.Frame(pset); pr3.pack(fill="x", pady=3)
        ttk.Label(pr3, text="組合資料：").pack(side="left")
        ttk.Button(pr3, text="匯出選取", command=self._preset_export).pack(
            side="left", padx=2)
        ttk.Button(pr3, text="匯出全部", command=self._preset_export_all).pack(
            side="left", padx=2)
        ttk.Button(pr3, text="匯入", command=self._preset_import).pack(
            side="left", padx=2)

    def _switch_mode(self):
        for w in (self.opt_keyword, self.opt_pages, self.opt_ranges, self.opt_custom):
            w.pack_forget()
        m = self.var_mode.get()
        {"keyword": self.opt_keyword, "pages": self.opt_pages,
         "page_ranges": self.opt_ranges, "custom": self.opt_custom}[m].pack(
            fill="both", expand=True)
        # 切換切割方式 → 清掉先前的偵測/預覽結果
        # (初次建構時 tree / lbl_status 尚未建立,故 hasattr 防呆)
        if hasattr(self, "tree"):
            self.rules = []
            self._reload_tree()
        if hasattr(self, "lbl_status"):
            self.lbl_status.config(text="")

    # ----------------------------------------------------------------- 檔案
    def _pick_pdf(self):
        p = filedialog.askopenfilename(
            title="選擇 PDF", filetypes=[("PDF 檔", "*.pdf"), ("所有檔案", "*.*")])
        if not p:
            return
        if not p.lower().endswith(".pdf"):
            messagebox.showwarning("提示", "請上傳 PDF 檔案")
            return
        if MAX_BYTES and os.path.getsize(p) > MAX_BYTES:
            messagebox.showwarning("提示", "檔案過大（上限 100MB）")
            return
        try:
            fitz = self.fitz or _import_fitz()
            self.fitz = fitz
            if self.doc:
                self.doc.close()
            self.doc = fitz.open(p)
            self.total_pages = len(self.doc)
        except Exception as e:
            messagebox.showerror("錯誤", str(e))
            return
        self.pdf_path = p
        self.var_pdf.set(p)
        self.lbl_pages.config(text=f"共 {self.total_pages} 頁")

    # ----------------------------------------------------------------- 偵測
    def _run_detect(self):
        mode = self.var_mode.get()
        # 頁碼模式只需總頁數；其餘需開檔
        if mode != "page_ranges" and self.doc is None:
            messagebox.showwarning("提示", "請先選擇 PDF 檔案")
            return
        if mode == "page_ranges" and self.doc is None and not self.total_pages:
            messagebox.showwarning("提示", "請先選擇 PDF 檔案")
            return
        try:
            if mode == "keyword":
                kw = self.var_kw.get().strip()
                if not kw:
                    messagebox.showwarning("提示", "請輸入觸發關鍵字")
                    return
                ex = self.var_kw_extract.get()
                sk = self.var_kw_search_kw.get().strip()
                if ex == "search" and not sk:
                    messagebox.showwarning("提示", "請輸入檔名關鍵字")
                    return
                total, groups = detect_by_keyword(
                    self.doc, kw, ex,
                    self.var_kw_prefix.get().strip() or "文件",
                    trigger_pos=self.var_kw_pos.get(),
                    name_search_keyword=sk,
                    name_search_extract=self.var_kw_search_extract.get())
                rules = groups_to_rules(groups)
            elif mode == "pages":
                try:
                    ppf = int(self.var_ppf.get())
                except ValueError:
                    messagebox.showwarning("提示", "每幾頁請填數字")
                    return
                total, groups = detect_by_pages(
                    self.doc, ppf, self.var_pg_prefix.get().strip() or "文件")
                rules = groups_to_rules(groups)
            elif mode == "page_ranges":
                rules = parse_page_splits(
                    self.var_ranges.get(), self.total_pages,
                    self.var_rg_prefix.get().strip() or "文件")
            else:  # custom
                pattern = blocks_to_pattern(self.blocks)
                if not pattern:
                    messagebox.showwarning("提示", "請至少新增一條規則")
                    return
                total, groups = detect_by_regex(self.doc, pattern)
                rules = groups_to_rules(groups)
        except ValueError as e:
            messagebox.showerror("錯誤", str(e))
            return
        except Exception as e:
            messagebox.showerror("錯誤", f"偵測失敗：{e}")
            return

        if not rules:
            if messagebox.askyesno(
                    "找不到分頁點",
                    "找不到符合條件的分頁點，請調整設定或手動新增。\n\n"
                    "要新增『整份 1–末頁』一筆規則嗎？"):
                rules = [{"name": "未識別", "start_page": 1,
                          "end_page": self.total_pages or 1}]
            else:
                return
        self.rules = rules
        self._reload_tree()
        self.lbl_status.config(text=f"偵測完成：共 {len(rules)} 份")

    # ----------------------------------------------------------------- 預覽表
    def _reload_tree(self):
        self._cancel_edit()
        for it in self.tree.get_children():
            self.tree.delete(it)
        for r in self.rules:
            self.tree.insert("", "end", values=(
                r["name"], r["start_page"], r["end_page"]))

    def _sync_rules_from_tree(self):
        self.rules = []
        for it in self.tree.get_children():
            n, s, e = self.tree.item(it, "values")
            self.rules.append({"name": n, "start_page": int(s), "end_page": int(e)})

    def _rule_add(self):
        last_end = self.rules[-1]["end_page"] if self.rules else 0
        start = last_end + 1
        end = max(start, self.total_pages or start)
        self.rules.append({"name": "未識別", "start_page": start, "end_page": end})
        self._reload_tree()

    def _rule_del(self):
        sel = self.tree.selection()
        if not sel:
            return
        idxs = sorted((self.tree.index(s) for s in sel), reverse=True)
        for i in idxs:
            del self.rules[i]
        self._reload_tree()

    def _rule_clear(self):
        self.rules = []
        self._reload_tree()

    def _on_tree_dblclick(self, event):
        self._cancel_edit()
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row:
            return
        col_idx = int(col[1:]) - 1
        x, y, w, h = self.tree.bbox(row, col)
        cur = self.tree.item(row, "values")[col_idx]
        var = tk.StringVar(value=str(cur))
        e = ttk.Entry(self.tree, textvariable=var)
        e.place(x=x, y=y, width=w, height=h)
        e.focus_set()
        e.select_range(0, "end")
        self._edit_widget = e

        def commit(_=None):
            new = var.get().strip()
            vals = list(self.tree.item(row, "values"))
            if col_idx in (1, 2):  # 起始 / 結束頁需為正整數
                try:
                    n = int(new)
                    if n < 1:
                        raise ValueError
                except ValueError:
                    messagebox.showwarning("提示", "頁碼需為正整數")
                    self._cancel_edit()
                    return
                vals[col_idx] = n
            else:
                vals[col_idx] = new
            self.tree.item(row, values=vals)
            self._cancel_edit()
            self._sync_rules_from_tree()

        e.bind("<Return>", commit)
        e.bind("<Escape>", lambda _: self._cancel_edit())
        e.bind("<FocusOut>", commit)

    def _cancel_edit(self):
        if self._edit_widget is not None:
            try:
                self._edit_widget.destroy()
            except tk.TclError:
                pass
            self._edit_widget = None

    # ----------------------------------------------------------------- 檢視
    def _on_tree_rightclick(self, event):
        """在某列上按右鍵:先取消編輯、選取該列,再彈出「檢視」選單。"""
        self._cancel_edit()
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        try:
            self.row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.row_menu.grab_release()

    def _build_split_bytes(self, start_page, end_page):
        """把 self.doc 第 start_page~end_page 頁(1-based)組成新 PDF。回傳 bytes。"""
        fitz = self.fitz or _import_fitz()
        self.fitz = fitz
        start = max(0, int(start_page) - 1)
        end = min(len(self.doc) - 1, int(end_page) - 1)
        new = fitz.open()
        if end >= start:
            new.insert_pdf(self.doc, from_page=start, to_page=end)
        buf = io.BytesIO()
        new.save(buf)
        new.close()
        return buf.getvalue()

    def _view_selected_split(self, _evt=None):
        """右鍵「檢視」:把選取列的頁面範圍組成 PDF,在「檢視」分頁預覽(不落地)。"""
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        try:
            name, start, end = str(vals[0]), int(vals[1]), int(vals[2])
        except (ValueError, IndexError):
            return
        if self.doc is None:
            messagebox.showwarning("提示", "請先選擇 PDF 檔案")
            return
        if start < 1 or end < start:
            messagebox.showwarning("提示", f"「{name}」頁碼範圍不正確")
            return
        try:
            data = self._build_split_bytes(start, end)
        except Exception as e:
            messagebox.showerror("錯誤", f"產生預覽失敗：\n{e}")
            return
        self._show_split_view(f"{name}（第 {start}–{end} 頁）", data)

    def _show_split_view(self, title, data):
        """在「檢視」分頁建立檢視器並載入該份切割(每次重建,銷毀舊檢視器會關檔)。"""
        try:
            viewer = _load_viewer_module()
        except Exception as e:
            messagebox.showerror("錯誤", f"無法載入檢視器：\n{e}")
            return
        if self._view_hint is not None:
            self._view_hint.destroy()
            self._view_hint = None
        if self._view_body is not None:
            self._view_body.destroy()
        body = ttk.Frame(self.tab_view)
        body.pack(fill="both", expand=True)
        self._view_body = body
        ttk.Label(body, text=title,
                  font=("Microsoft JhengHei UI", 12, "bold")).pack(
                      anchor="w", padx=8, pady=(6, 0))
        vf = viewer.create_frame(body, show_open=False)  # 工具列保留,不要「開啟」鈕
        vf.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        body.after(60, lambda: vf.app.open_bytes(data, title))
        self.notebook.select(self.tab_view)

    # ----------------------------------------------------------------- 規則
    def _refresh_blk_tree(self):
        for it in self.blk_tree.get_children():
            self.blk_tree.delete(it)
        for b in self.blocks:
            self.blk_tree.insert("", "end", values=(self._blk_desc(b),))
        self.var_pattern.set(blocks_to_pattern(self.blocks))

    @staticmethod
    def _blk_desc(b):
        t = b.get("type")
        parts = [block_type_label(t)]
        if t == "fixed":
            parts.append(f"「{b.get('value', '')}」")
        elif t == "chinese" and b.get("endsWith"):
            parts.append(f"結尾「{b['endsWith']}」")
        elif t == "number" and b.get("symbols"):
            parts.append(f"符號「{b['symbols']}」")
        if b.get("optional"):
            parts.append("(可省略)")
        if t != "space":
            parts.append("[納入檔名]" if b.get("inFilename") else "[不納入檔名]")
        return " ".join(parts)

    def _blk_add(self):
        BlockDialog(self.root, None, self._blk_on_save)

    def _blk_edit(self):
        sel = self.blk_tree.selection()
        if not sel:
            return
        i = self.blk_tree.index(sel[0])
        BlockDialog(self.root, dict(self.blocks[i]),
                    lambda b: self._blk_on_save(b, i))

    def _blk_on_save(self, block, idx=None):
        if idx is None:
            self.blocks.append(block)
        else:
            self.blocks[idx] = block
        self._refresh_blk_tree()

    def _blk_del(self):
        sel = self.blk_tree.selection()
        if not sel:
            return
        i = self.blk_tree.index(sel[0])
        del self.blocks[i]
        self._refresh_blk_tree()

    def _blk_move(self, d):
        sel = self.blk_tree.selection()
        if not sel:
            return
        i = self.blk_tree.index(sel[0])
        j = i + d
        if 0 <= j < len(self.blocks):
            self.blocks[i], self.blocks[j] = self.blocks[j], self.blocks[i]
            self._refresh_blk_tree()
            self.blk_tree.selection_set(self.blk_tree.get_children()[j])

    def _blk_clear(self):
        self.blocks = []
        self.var_test.set("")
        self._refresh_blk_tree()
        self.lbl_test.config(text="")

    def _blk_test(self):
        pat = blocks_to_pattern(self.blocks)
        if not pat:
            self.lbl_test.config(text="尚無規則", foreground="#a00")
            return
        try:
            m = re.compile(pat).search(_norm(self.var_test.get()))
        except re.error as e:
            self.lbl_test.config(text=f"規則格式錯誤：{e}", foreground="#a00")
            return
        if m:
            caps = [g for g in m.groups() if g]
            name = "".join(caps) if caps else m.group(0)
            self.lbl_test.config(text=f"✓ 命中，檔名 → {name}", foreground="#080")
        else:
            self.lbl_test.config(text="✗ 未命中", foreground="#a00")

    # ----------------------------------------------------------------- 預設
    def _refresh_preset_combo(self):
        names = self.presets.names()
        self.cmb_preset["values"] = names
        if names and not self.var_preset.get():
            self.var_preset.set(names[0])

    def _preset_apply(self):
        name = self.var_preset.get()
        p = self.presets.get(name)
        if not p:
            messagebox.showinfo("提示", "請先選擇一個預設")
            return
        self.blocks = [dict(b) for b in p.get("blocks", [])]
        self.var_test.set(p.get("example", ""))
        self._refresh_blk_tree()

    def _preset_check_test(self):
        """儲存前：測試文字必須能被目前規則命中。"""
        pat = blocks_to_pattern(self.blocks)
        if not pat:
            messagebox.showwarning("提示", "請至少新增一條規則")
            return None
        txt = self.var_test.get().strip()
        if not txt:
            messagebox.showwarning("提示", "請先填寫測試文字")
            return None
        try:
            if not re.compile(pat).search(_norm(txt)):
                messagebox.showwarning("提示", "測試文字無法被目前規則命中，請調整後再儲存")
                return None
        except re.error as e:
            messagebox.showerror("錯誤", f"規則格式錯誤：{e}")
            return None
        return txt

    def _preset_save_new(self):
        name = self.var_new_preset.get().strip()
        if not name:
            messagebox.showwarning("提示", "請輸入新設定名稱")
            return
        if self.presets.get(name):
            messagebox.showwarning("提示", "已有同名設定，『另存新設定』不允許重複名稱")
            return
        txt = self._preset_check_test()
        if txt is None:
            return
        if self.presets.upsert(name, [dict(b) for b in self.blocks], txt):
            self.var_new_preset.set("")
            self._refresh_preset_combo()
            self.var_preset.set(name)
            messagebox.showinfo("完成", f"已儲存預設「{name}」")
        else:
            messagebox.showerror("錯誤", "寫入預設檔失敗")

    def _preset_overwrite(self):
        name = self.var_preset.get()
        if not name or not self.presets.get(name):
            messagebox.showinfo("提示", "請先選擇要覆蓋的預設")
            return
        txt = self._preset_check_test()
        if txt is None:
            return
        if not messagebox.askyesno("確認", f"確定覆蓋預設「{name}」？"):
            return
        if self.presets.upsert(name, [dict(b) for b in self.blocks], txt):
            messagebox.showinfo("完成", f"已覆蓋「{name}」")
        else:
            messagebox.showerror("錯誤", "寫入預設檔失敗")

    def _preset_delete(self):
        name = self.var_preset.get()
        if not name or not self.presets.get(name):
            messagebox.showinfo("提示", "請先選擇要刪除的預設")
            return
        confirm = SimpleAsk(
            self.root, "刪除確認",
            f"請輸入完整預設名稱以確認刪除：\n「{name}」").result
        if confirm != name:
            if confirm is not None:
                messagebox.showwarning("提示", "名稱不符，已取消刪除")
            return
        if self.presets.delete(name):
            self.var_preset.set("")
            self._refresh_preset_combo()
            self._blk_clear()
            messagebox.showinfo("完成", f"已刪除「{name}」")
        else:
            messagebox.showerror("錯誤", "刪除失敗")

    def _preset_export(self):
        name = self.var_preset.get()
        entry = self.presets.get(name) if name else None
        if not entry:
            messagebox.showinfo("提示", "請先在下拉選單選取要匯出的預設")
            return
        p = filedialog.asksaveasfilename(
            title="匯出組合資料（單筆）", defaultextension=".json",
            initialfile=f"{_safe_name(name)}.json",
            filetypes=[("JSON 檔", "*.json")])
        if not p:
            return
        try:
            Path(p).write_text(
                json.dumps(entry, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            messagebox.showerror("錯誤", f"匯出失敗：{e}")
            return
        messagebox.showinfo("完成", f"已匯出「{name}」到：\n{p}")

    def _preset_export_all(self):
        if not self.presets.presets:
            messagebox.showinfo("提示", "目前沒有可匯出的組合資料")
            return
        p = filedialog.asksaveasfilename(
            title="匯出組合資料（全部）", defaultextension=".json",
            initialfile="自訂組合預設.json",
            filetypes=[("JSON 檔", "*.json")])
        if not p:
            return
        try:
            Path(p).write_text(
                json.dumps(self.presets.presets, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            messagebox.showerror("錯誤", f"匯出失敗：{e}")
            return
        messagebox.showinfo(
            "完成", f"已匯出全部 {len(self.presets.presets)} 組到：\n{p}")

    def _preset_import(self):
        p = filedialog.askopenfilename(
            title="匯入組合資料（單筆或多筆）",
            filetypes=[("JSON 檔", "*.json"), ("所有檔案", "*.*")])
        if not p:
            return
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("錯誤", f"讀取失敗：{e}")
            return
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not data:
            messagebox.showerror("錯誤", "檔案格式不正確（需為單筆或多筆組合資料）")
            return

        added = overwritten = skipped = 0
        last = None
        for entry in data:
            if (not isinstance(entry, dict) or not entry.get("name")
                    or not isinstance(entry.get("blocks"), list)):
                skipped += 1
                continue
            name = entry["name"]
            # 名稱已存在 → 讓使用者選擇 覆蓋 / 重新命名 / 不匯入
            while self.presets.get(name) is not None:
                choice = ConflictDialog(
                    self.root, f"已有同名設定「{name}」，請選擇處理方式：").result
                if choice == "overwrite":
                    break
                if choice == "skip":
                    name = None
                    break
                new = SimpleAsk(       # rename
                    self.root, "重新命名",
                    f"匯入的「{entry['name']}」與現有設定重複，\n"
                    "請輸入新的設定名稱：").result
                if not new:
                    name = None
                    break
                name = new
            if name is None:
                skipped += 1
                continue
            existed = self.presets.get(name) is not None
            self.presets.upsert(
                name,
                [dict(b) for b in entry["blocks"]],
                entry.get("example", ""))
            last = name
            if existed:
                overwritten += 1
            else:
                added += 1

        self._refresh_preset_combo()
        if last:
            self.var_preset.set(last)
        messagebox.showinfo(
            "完成",
            f"匯入完成：新增 {added}、覆蓋 {overwritten}、略過 {skipped}")

    # ----------------------------------------------------------------- 輸出
    def _collect_rules(self):
        self._cancel_edit()
        self._sync_rules_from_tree()
        if not self.rules:
            messagebox.showwarning("提示", "請至少設定一筆切割規則")
            return None
        if self.doc is None:
            messagebox.showwarning("提示", "請先選擇 PDF 檔案")
            return None
        for r in self.rules:
            if r["start_page"] < 1 or r["end_page"] < r["start_page"]:
                messagebox.showwarning(
                    "提示", f"規則「{r['name']}」頁碼範圍不正確")
                return None
        return self.rules

    def _output_folder(self):
        rules = self._collect_rules()
        if rules is None:
            return
        d = filedialog.askdirectory(title="選擇輸出資料夾")
        if not d:
            return
        try:
            outputs = build_outputs(self.fitz, self.doc, rules)
            for fname, data in outputs:
                (Path(d) / fname).write_bytes(data)
        except Exception as e:
            messagebox.showerror("錯誤", f"輸出失敗：{e}")
            return
        messagebox.showinfo("完成", f"已輸出 {len(outputs)} 個 PDF 到：\n{d}")

    def _output_zip(self):
        rules = self._collect_rules()
        if rules is None:
            return
        p = filedialog.asksaveasfilename(
            title="儲存 ZIP", defaultextension=".zip",
            initialfile="切割結果.zip", filetypes=[("ZIP 壓縮檔", "*.zip")])
        if not p:
            return
        try:
            outputs = build_outputs(self.fitz, self.doc, rules)
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
                for fname, data in outputs:
                    zf.writestr(fname, data)
        except Exception as e:
            messagebox.showerror("錯誤", f"輸出失敗：{e}")
            return
        messagebox.showinfo("完成", f"已打包 {len(outputs)} 個 PDF：\n{p}")

    # ----------------------------------------------------------------- 說明
    def _build_help_tab(self):
        txt = tk.Text(self.tab_help, wrap="word", font=FONT,
                      relief="flat", padx=12, pady=10)
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("1.0", HELP_TEXT)
        txt.config(state="disabled")


HELP_TEXT = """PDF 切割工具 — 使用說明

操作流程
  1. 選擇 PDF 檔
  2. 選切割方式並設定條件
  3. 「開始偵測」→ 預覽結果表（名稱、起始頁、結束頁）
  4. 雙擊欄位可編輯；可新增 / 刪除規則
     （在某列上按右鍵 →「檢視」，可在「檢視」分頁預覽該份切割的頁面）
  5. 「輸出到資料夾」直接寫出多個 PDF，或「打包成 ZIP」

四種切割方式
  • 遇到關鍵字切割：每次出現關鍵字就視為一份文件的切點（每次觸發必開新組，
    同名也不合併）。可選「關鍵字位置」:
      - 在文件起始頁(預設): 關鍵字頁為新文件起始,起始前的頁丟棄
      - 在文件結束頁: 關鍵字頁為當前文件結尾,下一頁起算新文件,
                     最後一個關鍵字之後沒收尾的頁丟棄
    檔名可取關鍵字前 / 後 / 整行 / 流水號 / 群組內搜尋。
    「群組內搜尋」: 切完後再用另一個「檔名關鍵字」在每份文件範圍內搜尋,
                取首個命中行套 before/after/整行 規則當檔名,找不到 → 未識別。
  • 每固定頁數切割：每 N 頁切一份，命名 前綴_001、_002…
  • 頁碼切割：輸入分割點（逗號 / 空白 / 、/ ， 分隔）。
        n   → 在第 n 頁之後分割
        n-m → 第 n 頁之前與第 m 頁之後分割
    起始頁集合一律含 1。此模式不需掃描內文。
  • 自訂組合規則：用一條條「規則」相接組出比對條件（不需懂正則）。
    規則型別：固定文字 / 文字(可指定結尾字) / 數字(可含符號) / 任意文字 / 空白。
    可勾「可省略」「納入檔名」。沿用『同名相鄰才併組』。
    內建「地籍謄本」預設：(\\S+區)(\\S+段)(\\S+小段)?\\s*(\\d+(?:-\\d+)*)地號
    注意「小段」與數字間一定要有一個『空白』規則，否則部分 PDF 比對不到。

自訂組合預設
  自訂組合初始已內建「地籍謄本」預設,於下方下拉選單選取後按「套用」即可帶入。
  測試文字需能被目前規則命中才允許儲存。「另存新設定」不允許重複名稱；
  選中預設可「覆蓋此預設」；刪除需輸入完整名稱二次確認。
  預設檔：獨立執行放程式資料夾 presets.json；
  透過 Launcher 執行放使用者家目錄 ~/.pdf_splitter_presets.json。

輸出
  檔名安全化：\\ / : * ? " < > | 會換成底線。
  同名去重：第一個 名稱.pdf，之後 名稱_1.pdf、名稱_2.pdf…
  規則數為 0 會擋下並提示「請至少設定一筆切割規則」。
"""


# ---------------------------------------------------------------------------
#  規則編輯對話框
# ---------------------------------------------------------------------------

class BlockDialog(tk.Toplevel):
    def __init__(self, parent, block, on_save):
        super().__init__(parent)
        self.title("規則設定")
        self.transient(parent)
        self.resizable(False, False)
        self.on_save = on_save
        b = block or {"type": "fixed", "value": "", "endsWith": "",
                      "symbols": "", "optional": False, "inFilename": True}

        self.var_type = tk.StringVar(value=block_type_label(b["type"]))
        self.var_value = tk.StringVar(value=b.get("value", ""))
        self.var_ends = tk.StringVar(value=b.get("endsWith", ""))
        self.var_syms = tk.StringVar(value=b.get("symbols", ""))
        self.var_opt = tk.BooleanVar(value=b.get("optional", False))
        self.var_infn = tk.BooleanVar(value=b.get("inFilename", True))

        pad = {"padx": 8, "pady": 4}
        r = ttk.Frame(self); r.pack(fill="x", **pad)
        ttk.Label(r, text="型別：").pack(side="left")
        cmb = ttk.Combobox(r, textvariable=self.var_type, state="readonly",
                           width=22, values=list(BLOCK_TYPE_LABELS.values()))
        cmb.pack(side="left", padx=4)
        cmb.bind("<<ComboboxSelected>>", lambda _: self._refresh_fields())

        self.row_value = ttk.Frame(self)
        ttk.Label(self.row_value, text="固定文字內容：").pack(side="left")
        ttk.Entry(self.row_value, textvariable=self.var_value, width=28).pack(
            side="left", padx=4)

        self.row_ends = ttk.Frame(self)
        ttk.Label(self.row_ends, text="結尾字（如 區 / 段，可留空）：").pack(side="left")
        ttk.Entry(self.row_ends, textvariable=self.var_ends, width=14).pack(
            side="left", padx=4)

        self.row_syms = ttk.Frame(self)
        ttk.Label(self.row_syms, text="可含符號（如 - / ）：").pack(side="left")
        ttk.Entry(self.row_syms, textvariable=self.var_syms, width=14).pack(
            side="left", padx=4)

        self.row_chk = ttk.Frame(self)
        ttk.Checkbutton(self.row_chk, text="可省略", variable=self.var_opt).pack(
            side="left", padx=(0, 16))
        self.chk_infn = ttk.Checkbutton(
            self.row_chk, text="納入檔名", variable=self.var_infn)
        self.chk_infn.pack(side="left")

        bb = ttk.Frame(self); bb.pack(fill="x", **pad)
        ttk.Button(bb, text="確定", command=self._ok).pack(side="right", padx=4)
        ttk.Button(bb, text="取消", command=self.destroy).pack(side="right")

        self._refresh_fields()
        self.grab_set()

    def _refresh_fields(self):
        for w in (self.row_value, self.row_ends, self.row_syms, self.row_chk):
            w.pack_forget()
        t = block_type_key(self.var_type.get())
        if t == "fixed":
            self.row_value.pack(fill="x", padx=8, pady=4)
        elif t == "chinese":
            self.row_ends.pack(fill="x", padx=8, pady=4)
        elif t == "number":
            self.row_syms.pack(fill="x", padx=8, pady=4)
        if t != "space":
            self.row_chk.pack(fill="x", padx=8, pady=4)

    def _ok(self):
        t = block_type_key(self.var_type.get())
        block = {
            "type": t,
            "value": self.var_value.get(),
            "endsWith": self.var_ends.get(),
            "symbols": self.var_syms.get(),
            "optional": bool(self.var_opt.get()),
            "inFilename": bool(self.var_infn.get()),
        }
        if t == "fixed" and not block["value"]:
            messagebox.showwarning("提示", "固定文字內容不可為空", parent=self)
            return
        self.on_save(block)
        self.destroy()


class SimpleAsk(tk.Toplevel):
    """簡易文字輸入對話框（回傳 .result，取消為 None）。"""

    def __init__(self, parent, title, prompt):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        ttk.Label(self, text=prompt, justify="left").pack(
            padx=14, pady=(14, 6))
        self.var = tk.StringVar()
        e = ttk.Entry(self, textvariable=self.var, width=32)
        e.pack(padx=14, pady=6)
        e.focus_set()
        bb = ttk.Frame(self); bb.pack(pady=(6, 12))
        ttk.Button(bb, text="確定", command=self._ok).pack(side="left", padx=6)
        ttk.Button(bb, text="取消", command=self.destroy).pack(side="left", padx=6)
        e.bind("<Return>", lambda _: self._ok())
        self.grab_set()
        self.wait_window()

    def _ok(self):
        self.result = self.var.get().strip()
        self.destroy()


class ConflictDialog(tk.Toplevel):
    """名稱重複時的三選一(回傳 .result: overwrite / rename / skip)。"""

    def __init__(self, parent, message):
        super().__init__(parent)
        self.title("名稱重複")
        self.transient(parent)
        self.resizable(False, False)
        self.result = "skip"  # 直接關閉視窗 = 不匯入
        ttk.Label(self, text=message, justify="left").pack(
            padx=16, pady=(16, 10))
        bb = ttk.Frame(self); bb.pack(pady=(0, 14))
        ttk.Button(bb, text="覆蓋",
                   command=lambda: self._pick("overwrite")).pack(
            side="left", padx=6)
        ttk.Button(bb, text="重新命名",
                   command=lambda: self._pick("rename")).pack(
            side="left", padx=6)
        ttk.Button(bb, text="不匯入",
                   command=lambda: self._pick("skip")).pack(
            side="left", padx=6)
        self.grab_set()
        self.wait_window()

    def _pick(self, val):
        self.result = val
        self.destroy()


def create_frame(parent, presets_dir=None):
    """供 PDF 工具集主程式嵌入用。回傳一個含完整切割 UI 的 Frame。"""
    frame = ttk.Frame(parent)
    App(frame, presets_dir=presets_dir)
    return frame


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
