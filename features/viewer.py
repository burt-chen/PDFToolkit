#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 檢視器 (GUI 版) — 連續捲動式

開啟 PDF 後,所有頁面由上而下接續排列(像網頁),用滾輪即可一路往下看,
不必逐頁切換。參考 D:\\工具開發\\PDF 的檢視器功能,但改用本專案技術棧
(tkinter + PyMuPDF),不額外依賴 Qt / Pillow。

功能:
  • 開檔;連續捲動瀏覽全部頁面(滾輪上下、Shift+滾輪左右)
  • 上一頁 / 下一頁 / 跳頁(捲動到該頁)
  • 放大 / 縮小、適合寬度、適合頁面、Ctrl+滾輪縮放
  • 隨捲動延遲渲染:只繪製可視範圍內的頁面,大檔也不卡、省記憶體
  • 文字搜尋(Ctrl+F):右側面板列出所有符合處,點選即跳頁並以螢光框標示;
    可選「區分大小寫 / 整字」

備註:以 PyMuPDF 把每頁渲染成點陣圖顯示,不支援選取文字;
但可用上方搜尋功能定位文字。
"""

import re
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


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


FONT = ("Microsoft JhengHei UI", 12)

MIN_SCALE = 0.1
MAX_SCALE = 8.0
GAP = 12            # 頁與頁之間的間距(像素)


class App:
    def __init__(self, root, presets_dir=None, show_toolbar=True, show_open=True):
        """root 可為 tk.Tk / Toplevel / Frame(嵌入用)。presets_dir 此功能未用。
        show_toolbar=False:不顯示自身工具列(供比對檢視由外部共用工具列驅動)。
        show_open=False:工具列保留,但不顯示「開啟」鈕(供預覽既有內容的情境)。"""
        self.root = root
        self.show_toolbar = show_toolbar
        self.show_open = show_open
        if isinstance(root, (tk.Tk, tk.Toplevel)):
            self.root.title("PDF 檢視器")
            self.root.geometry("1040x780")
            self.root.minsize(900, 600)
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass

        self.fitz = None
        self.doc = None
        self.pdf_path = None
        self.page_index = 0              # 目前位於可視區頂端的頁(0-based)
        self.scale = 1.0                 # 像素/點(pixels per point)
        self.fit_mode = "page"           # "width" | "page" | "free"(預設適合頁面)
        self.cols = 1                    # 多格檢視:一列顯示幾頁

        self.layout = []                 # 每頁:{x,y,w,h,rect,img}
        self.photos = {}                 # idx -> PhotoImage(持有參照避免 GC)
        self.rendered = {}               # idx -> 已渲染時的 scale
        self._content_h = 0
        self._last_canvas_size = (0, 0)
        self._resize_job = None
        self._vis_job = None

        # 搜尋狀態
        self.search_visible = False
        self._matches = []               # [{page, rect(x0,y0,x1,y1), snippet}]
        self._active_match = None        # 目前標示的那一筆
        self.highlight_all = False       # True:所有命中都淡黃標示(用於標示取代處)

        # 外部驅動(供比對檢視:共用工具列 + 同步捲動 + 點選結果跨邊跳頁)
        self._sync_cb = None             # 本檢視器捲動時回呼 fn(fraction)
        self._sync_lock = False          # 被動捲動中,避免回呼造成兩邊互推
        self.on_state = None             # 頁碼/縮放變動時回呼(更新外部工具列)
        self.on_match_jump = None        # 點選搜尋結果跳頁時回呼 fn(page_index)

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
        style.configure("TButton", padding=[8, 4])

    # ---- UI ----
    def _build_ui(self):
        # show_toolbar=False:仍建立工具列元件(var_page/lbl_zoom 等供內部沿用),
        # 只是不 pack 出來;改由外部共用工具列驅動。
        bar = ttk.Frame(self.root)
        if self.show_toolbar:
            bar.pack(fill="x", padx=6, pady=(6, 2))

        if self.show_open:
            ttk.Button(bar, text="開啟", command=self._open).pack(side="left")
            ttk.Separator(bar, orient="vertical").pack(
                side="left", fill="y", padx=6)

        ttk.Button(bar, text="◀ 上一頁", command=self.prev_page).pack(side="left")
        self.var_page = tk.StringVar(value="0")
        ent = ttk.Entry(bar, textvariable=self.var_page, width=5, justify="center")
        ent.pack(side="left", padx=(6, 2))
        ent.bind("<Return>", self._goto_from_entry)
        self.lbl_total = ttk.Label(bar, text="/ 0")
        self.lbl_total.pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="下一頁 ▶", command=self.next_page).pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(bar, text="－", width=3, command=self.zoom_out).pack(side="left")
        self.lbl_zoom = ttk.Label(bar, text="100%", width=6, anchor="center")
        self.lbl_zoom.pack(side="left", padx=2)
        ttk.Button(bar, text="＋", width=3, command=self.zoom_in).pack(side="left")
        ttk.Button(bar, text="適合寬度", command=self.fit_width).pack(
            side="left", padx=(6, 2))
        ttk.Button(bar, text="適合頁面", command=self.fit_page).pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Label(bar, text="欄數：").pack(side="left")
        self.var_cols = tk.StringVar(value="1")
        cmb = ttk.Combobox(bar, textvariable=self.var_cols, state="readonly",
                           width=3, values=["1", "2", "3", "4"])
        cmb.pack(side="left")
        cmb.bind("<<ComboboxSelected>>", self._on_cols_changed)

        # 搜尋面板切換(預設隱藏,亦可按 Ctrl+F)
        ttk.Button(bar, text="🔍 搜尋", command=self._toggle_search).pack(
            side="right")

        # 檢視區容器:左=畫布(+雙捲軸)、右=搜尋面板
        content = ttk.Frame(self.root)
        content.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.search_panel = self._build_search_panel(content)   # 建好但尚未 pack
        wrap = ttk.Frame(content)
        wrap.pack(side="left", fill="both", expand=True)
        self._wrap = wrap

        self.canvas = tk.Canvas(wrap, background="#666666",
                                highlightthickness=0)
        self.vsb = ttk.Scrollbar(wrap, orient="vertical",
                                 command=self.canvas.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.canvas.xview)
        # yscrollcommand 用自訂 hook:任何捲動來源(滾輪/拖捲軸/跳頁)都會觸發,
        # 藉此做延遲渲染與目前頁碼更新。
        self.canvas.configure(yscrollcommand=self._on_yscroll, xscrollcommand=hsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self._hint_id = self.canvas.create_text(
            20, 20, anchor="nw", fill="#eeeeee", font=FONT,
            text="把 PDF 拖曳到這裡，或按左上角「開啟」選擇檔案。")

        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_wheel_shift)
        self.canvas.bind("<Control-MouseWheel>", self._on_wheel_ctrl)
        self.canvas.bind("<Button-1>", lambda _e: self.canvas.focus_set())
        self.canvas.bind("<Prior>", lambda _e: self.prev_page())   # PageUp
        self.canvas.bind("<Next>", lambda _e: self.next_page())    # PageDown
        # Ctrl+F 綁在自身元件子樹(非全域、非 toplevel):同一視窗內可同時有多個
        # 檢視器(如比對分頁的左右兩個 + 主檢視器),綁到 toplevel 會互搶;綁在
        # self.root 時,事件由具焦點的子元件向上冒泡到此才觸發,各自獨立。
        self.root.bind("<Control-f>", self._focus_search)
        self._setup_dnd()

        self.lbl_status = ttk.Label(self.root, text="", foreground="#555",
                                    anchor="w")
        if self.show_toolbar:
            self.lbl_status.pack(fill="x", padx=8, pady=(0, 4))

        # 元件被銷毀(切換功能/關閉)時關閉文件,釋放檔案鎖定
        self.canvas.bind("<Destroy>", self._on_destroy)

    def _on_destroy(self, _evt=None):
        if self.doc is not None:
            try:
                self.doc.close()
            except Exception:
                pass
            self.doc = None

    # ----------------------------------------------------------------- 開檔
    def _open(self):
        p = filedialog.askopenfilename(
            title="開啟 PDF",
            filetypes=[("PDF 檔", "*.pdf"), ("所有檔案", "*.*")])
        if p:
            self.open_path(p)

    # ---- 拖放開檔(需 tkinterdnd2;沒裝則靜默略過,改用「開啟…」按鈕) ----
    def _setup_dnd(self):
        self.dnd_ok = False
        try:
            from tkinterdnd2 import TkinterDnD, DND_FILES
            TkinterDnD._require(self.root.winfo_toplevel())
            self.canvas.drop_target_register(DND_FILES)
            self.canvas.dnd_bind("<<Drop>>", self._on_drop)
            self.dnd_ok = True
        except Exception:
            self.dnd_ok = False

    def _on_drop(self, event):
        # event.data 可能是以大括號包住、空白分隔的多個路徑;用 Tcl splitlist 正確拆解
        try:
            paths = list(self.canvas.tk.splitlist(event.data))
        except Exception:
            paths = [event.data]
        pdfs = [p for p in paths if str(p).lower().endswith(".pdf")]
        target = pdfs[0] if pdfs else (paths[0] if paths else None)
        if target:
            self.open_path(str(target))
        return getattr(event, "action", None)

    def open_path(self, path):
        try:
            fitz = self.fitz or _import_fitz()
            self.fitz = fitz
            doc = fitz.open(path)
        except Exception as e:
            messagebox.showerror("錯誤", f"無法開啟：\n{e}")
            return
        self._load_doc(doc, Path(path).name, path)

    def open_bytes(self, data, name="(預覽)"):
        """從記憶體 bytes 開啟 PDF(供「取代後預覽」等不落地的情境)。"""
        try:
            fitz = self.fitz or _import_fitz()
            self.fitz = fitz
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as e:
            messagebox.showerror("錯誤", f"無法開啟：\n{e}")
            return
        self._load_doc(doc, name, None)

    def _load_doc(self, doc, display_name, path):
        """共用:換掉目前文件並重建畫面。path 為來源路徑(記憶體開檔時為 None)。"""
        if self.doc:
            try:
                self.doc.close()
            except Exception:
                pass
        self.doc = doc
        self.pdf_path = path
        self.page_index = 0
        self.fit_mode = "page"           # 開檔預設:適合頁面
        self.canvas.itemconfigure(self._hint_id, text="")
        self.lbl_total.config(text=f"/ {len(doc)}")
        self.lbl_status.config(text=f"{display_name}（共 {len(doc)} 頁）")
        self._clear_search()
        self._build_pages()
        self.canvas.yview_moveto(0.0)
        self._reflow()

    # ----------------------------------------------------------------- 翻頁
    def show_page(self, idx):
        if not self.doc:
            return
        idx = max(0, min(idx, len(self.doc) - 1))
        self.page_index = idx
        self.var_page.set(str(idx + 1))
        self._scroll_to_page(idx)

    def next_page(self):
        self.show_page(self.page_index + 1)

    def prev_page(self):
        self.show_page(self.page_index - 1)

    def _goto_from_entry(self, _evt=None):
        if not self.doc:
            return
        try:
            n = int(self.var_page.get())
        except ValueError:
            self.var_page.set(str(self.page_index + 1))
            return
        self.show_page(n - 1)

    def _scroll_to_page(self, idx):
        if not self.layout or self._content_h <= 0:
            return
        y = self.layout[idx]["y"] - GAP
        self.canvas.yview_moveto(max(0.0, min(y / self._content_h, 1.0)))

    # ----------------------------------------------------------------- 縮放
    def set_scale(self, scale):
        self.fit_mode = "free"
        self.scale = max(MIN_SCALE, min(scale, MAX_SCALE))
        self._invalidate()
        self._reflow(keep_page=True)

    def zoom_in(self):
        self.set_scale(self.scale * 1.25)

    def zoom_out(self):
        self.set_scale(self.scale / 1.25)

    def fit_width(self):
        self.fit_mode = "width"
        self._invalidate()
        self._reflow(keep_page=True)

    def fit_page(self):
        self.fit_mode = "page"
        self._invalidate()
        self._reflow(keep_page=True)

    def _on_cols_changed(self, _evt=None):
        try:
            self.cols = max(1, min(4, int(self.var_cols.get())))
        except ValueError:
            self.cols = 1
        # 多格時以「適合欄寬」呈現,讓多頁並排剛好放得下
        self.fit_mode = "width"
        self._invalidate()
        self._reflow(keep_page=True)

    # ----------------------------------------------------------------- 排版
    def _page_pixel_size(self, i, scale=None):
        """第 i 頁在指定 scale 下的像素尺寸。
        page.rect 已反映頁面旋轉,與 get_pixmap 輸出一致,故不必自行交換寬高。"""
        s = self.scale if scale is None else scale
        r = self.doc[i].rect
        return int(round(r.width * s)), int(round(r.height * s))

    def _fit_scale(self):
        """依目前可視頁(page_index)計算 適合寬度/頁面 的 scale(含多格欄寬)。"""
        i = max(0, min(self.page_index, len(self.doc) - 1))
        r = self.doc[i].rect      # 已含旋轉,直接用
        w, h = r.width, r.height
        cols = max(1, self.cols)
        # 每欄可用寬度 = (畫布寬 - 各間距) / 欄數
        cell_w = max(50, (self.canvas.winfo_width() - GAP * (cols + 1)) / cols)
        ch = max(50, self.canvas.winfo_height() - GAP * 2)
        s = cell_w / w if self.fit_mode == "width" else min(cell_w / w, ch / h)
        return max(MIN_SCALE, min(s, MAX_SCALE))

    def _build_pages(self):
        """開檔時建立每頁的 canvas 圖元(白底矩形 + 影像),座標稍後再排。"""
        self.canvas.delete("page")
        self.layout = []
        self.photos = {}
        self.rendered = {}
        for _ in range(len(self.doc)):
            rect = self.canvas.create_rectangle(
                0, 0, 1, 1, fill="white", outline="#bbbbbb", tags=("page",))
            img = self.canvas.create_image(0, 0, anchor="nw", tags=("page",))
            self.layout.append({"x": 0, "y": 0, "w": 1, "h": 1,
                                "rect": rect, "img": img})

    def _invalidate(self):
        """scale 變更 → 清空已渲染快取,讓可視頁以新倍率重畫。"""
        for i in list(self.photos):
            self.canvas.itemconfigure(self.layout[i]["img"], image="")
        self.photos.clear()
        self.rendered.clear()

    def _reflow(self, keep_page=False):
        """重算每頁座標、scrollregion,並渲染可視頁。"""
        if not self.doc:
            return
        # 畫布尚未配置好大小(剛建立) → 稍後重試,避免 fit 算出極小值
        if self.canvas.winfo_width() <= 1:
            self.canvas.after(50, lambda: self._reflow(keep_page))
            return
        if self.fit_mode in ("width", "page"):
            self.scale = self._fit_scale()

        cw = self.canvas.winfo_width()
        cols = max(1, self.cols)
        sizes = [self._page_pixel_size(i) for i in range(len(self.doc))]
        col_w = max([w for w, _ in sizes] + [1])          # 欄寬取最寬頁
        grid_w = cols * col_w + (cols - 1) * GAP
        left = max(GAP, (cw - grid_w) // 2)               # 整個網格水平置中
        n = len(sizes)
        y = GAP
        i = 0
        while i < n:
            row = sizes[i:i + cols]
            row_h = max(h for _, h in row)                 # 同列以最高者為列高
            for c, (w, h) in enumerate(row):
                idx = i + c
                cell_x = left + c * (col_w + GAP)
                x = cell_x + (col_w - w) // 2              # 頁面置中於該欄
                lay = self.layout[idx]
                lay.update(x=x, y=y, w=w, h=h)
                self.canvas.coords(lay["rect"], x, y, x + w, y + h)
                self.canvas.coords(lay["img"], x, y)
            y += row_h + GAP
            i += cols
        self._content_h = y
        content_w = max(cw, grid_w + 2 * GAP)
        self.canvas.configure(scrollregion=(0, 0, content_w, y))
        self.lbl_zoom.config(text=f"{round(self.scale * 100)}%")
        if keep_page:
            self._scroll_to_page(self.page_index)
        self._render_visible()
        self._draw_highlight()
        self._notify_state()

    # ----------------------------------------------------------------- 渲染
    def _ensure_rendered(self, i):
        if self.rendered.get(i) == self.scale:
            return
        try:
            page = self.doc[i]
            mat = self.fitz.Matrix(self.scale, self.scale)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            photo = tk.PhotoImage(data=pix.tobytes("png"))
        except Exception:
            return
        self.photos[i] = photo            # 持有參照
        self.rendered[i] = self.scale
        lay = self.layout[i]
        self.canvas.itemconfigure(lay["img"], image=photo)
        # 白底矩形貼齊實際渲染圖尺寸,消除四捨五入造成的白邊
        self.canvas.coords(lay["rect"], lay["x"], lay["y"],
                           lay["x"] + photo.width(), lay["y"] + photo.height())

    def _free(self, i):
        if i in self.photos:
            self.canvas.itemconfigure(self.layout[i]["img"], image="")
            del self.photos[i]
            self.rendered.pop(i, None)

    def _render_visible(self):
        """只渲染可視範圍(含上下各一螢幕緩衝)的頁;其餘釋放以省記憶體。"""
        if not self.doc or not self.layout:
            return
        top = self.canvas.canvasy(0)
        vh = self.canvas.winfo_height()
        bottom = top + vh
        buf = vh
        for i, lay in enumerate(self.layout):
            y0, y1 = lay["y"], lay["y"] + lay["h"]
            if y1 >= top - buf and y0 <= bottom + buf:
                self._ensure_rendered(i)
            else:
                self._free(i)

    def _update_current_page(self):
        """依捲動位置更新「目前頁」(可視區頂端的那頁)。

        與 _scroll_to_page 的「-GAP 邊距」一致:目前頁 = 頁頂(含 GAP 容差)仍在
        可視頂端以上的最後一頁。避免落在頁間距時被算成前一頁而差 1。"""
        if not self.layout:
            return
        top = self.canvas.canvasy(0)
        idx = 0
        for i, lay in enumerate(self.layout):
            if lay["y"] - GAP > top + 1:    # 此頁明顯起始於可視頂端之下 → 停
                break
            idx = i
        self.page_index = idx
        self.var_page.set(str(idx + 1))

    # ----------------------------------------------------------------- 事件
    def _on_yscroll(self, first, last):
        self.vsb.set(first, last)
        # 主動捲動 → 通知同步對象(被動捲動時 _sync_lock 為真,不回呼以免互推)
        if self._sync_cb is not None and not self._sync_lock:
            try:
                self._sync_cb(float(first))
            except Exception:
                pass
        # 捲動頻繁 → debounce 後再做延遲渲染與頁碼更新
        if self._vis_job is not None:
            self.canvas.after_cancel(self._vis_job)
        self._vis_job = self.canvas.after(20, self._after_scroll)

    def _after_scroll(self):
        self._vis_job = None
        self._render_visible()
        self._update_current_page()
        self._notify_state()

    def _on_canvas_resize(self, _evt=None):
        size = (self.canvas.winfo_width(), self.canvas.winfo_height())
        if size == self._last_canvas_size:
            return
        self._last_canvas_size = size
        if not self.doc:
            return
        if self._resize_job is not None:
            self.canvas.after_cancel(self._resize_job)
        self._resize_job = self.canvas.after(
            80, lambda: self._reflow(keep_page=True))

    def _on_wheel(self, evt):
        self.canvas.yview_scroll(-1 if evt.delta > 0 else 1, "units")
        return "break"

    def _on_wheel_shift(self, evt):
        self.canvas.xview_scroll(-1 if evt.delta > 0 else 1, "units")
        return "break"

    def _on_wheel_ctrl(self, evt):
        self.zoom_in() if evt.delta > 0 else self.zoom_out()
        return "break"

    # ----------------------------------------------------------------- 搜尋
    def _build_search_panel(self, parent):
        """建立右側搜尋面板(回傳 Frame,呼叫端決定何時顯示)。"""
        panel = ttk.Frame(parent, width=300)
        panel.pack_propagate(False)              # 固定寬度,不被內容撐開

        top = ttk.Frame(panel)
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.var_search = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.var_search)
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<Return>", self._do_search)
        ent.bind("<Escape>", lambda _e: self._toggle_search())
        self._search_entry = ent
        ttk.Button(top, text="搜尋", command=self._do_search).pack(
            side="left", padx=(6, 0))

        opts = ttk.Frame(panel)
        opts.pack(fill="x", padx=8)
        self.var_case = tk.BooleanVar(value=False)
        self.var_whole = tk.BooleanVar(value=False)
        # 切換選項時若已有關鍵字則立即重新搜尋
        ttk.Checkbutton(opts, text="區分大小寫", variable=self.var_case,
                        command=self._do_search).pack(side="left")
        ttk.Checkbutton(opts, text="整字", variable=self.var_whole,
                        command=self._do_search).pack(side="left", padx=(12, 0))

        self.lbl_results = ttk.Label(panel, text="", foreground="#555")
        self.lbl_results.pack(fill="x", padx=8, pady=(6, 2))

        tvwrap = ttk.Frame(panel)
        tvwrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tv = ttk.Treeview(tvwrap, show="tree", selectmode="browse")
        tvsb = ttk.Scrollbar(tvwrap, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=tvsb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        tvsb.pack(side="right", fill="y")
        self.tv.bind("<<TreeviewSelect>>", self._on_result_select)
        return panel

    def _toggle_search(self):
        if self.search_visible:
            self.search_panel.pack_forget()
            self.search_visible = False
        else:
            self.search_panel.pack(side="right", fill="y", before=self._wrap)
            self.search_visible = True
            self._search_entry.focus_set()

    def _focus_search(self, _evt=None):
        """Ctrl+F:展開面板並聚焦輸入框。"""
        if not self.search_visible:
            self._toggle_search()
        else:
            self._search_entry.focus_set()
            self._search_entry.select_range(0, "end")
        return "break"

    def _clear_search(self):
        self._matches = []
        self._active_match = None
        if hasattr(self, "tv"):
            self.tv.delete(*self.tv.get_children())
        if hasattr(self, "lbl_results"):
            self.lbl_results.config(text="")
        self.canvas.delete("search_hl")

    def _do_search(self, _evt=None):
        if not hasattr(self, "tv"):
            return
        self.tv.delete(*self.tv.get_children())
        self._matches = []
        self._active_match = None
        self.canvas.delete("search_hl")
        needle = self.var_search.get().strip()
        if not self.doc or not needle:
            self.lbl_results.config(text="")
            return
        try:
            matches = self._collect_matches(
                needle, self.var_case.get(), self.var_whole.get())
        except Exception as e:
            self.lbl_results.config(text=f"搜尋失敗:{e}")
            return
        self._matches = matches
        for i, m in enumerate(matches):
            label = f"第 {m['page'] + 1} 頁   {m['snippet']}"
            self.tv.insert("", "end", iid=str(i), text=label)
        self.lbl_results.config(
            text=f"共找到 {len(matches)} 筆結果" if matches else "沒有符合的結果")

    def _collect_matches(self, needle, case, whole):
        """掃描全部頁面,回傳每個出現處的 {page, rect, snippet}。

        以 PyMuPDF 的 search_for 取得矩形(不分大小寫的子字串比對),
        再依「區分大小寫 / 整字」過濾,確保清單與螢光標示一致。"""
        out = []
        for pno in range(len(self.doc)):
            page = self.doc[pno]
            try:
                rects = page.search_for(needle)
            except Exception:
                continue
            for r in rects:
                if case:
                    got = (page.get_textbox(r) or "").strip()
                    if needle not in got:        # 大小寫不符 → 略過
                        continue
                snippet = self._line_snippet(page, r, needle)
                if whole and not self._whole_word(snippet, needle, case):
                    continue
                out.append({"page": pno,
                            "rect": (r.x0, r.y0, r.x1, r.y1),
                            "snippet": snippet})
        return out

    def _line_snippet(self, page, r, needle, span=24):
        """取出符合處所在「整行」的文字,並裁切到關鍵字前後約 span 個字。"""
        band = self.fitz.Rect(0, r.y0 - 1, page.rect.width, r.y1 + 1)
        try:
            text = " ".join((page.get_textbox(band) or "").split())
        except Exception:
            text = ""
        pos = text.lower().find(needle.lower())
        if pos < 0:
            return text[:span * 2].strip()
        a, b = max(0, pos - span), min(len(text), pos + len(needle) + span)
        s = text[a:b].strip()
        return ("…" if a > 0 else "") + s + ("…" if b < len(text) else "")

    @staticmethod
    def _whole_word(snippet, needle, case):
        """整字:關鍵字前後不可緊鄰英數字(對中文無邊界概念,等同永遠成立)。"""
        flags = 0 if case else re.IGNORECASE
        pat = r"(?<![0-9A-Za-z])" + re.escape(needle) + r"(?![0-9A-Za-z])"
        return re.search(pat, snippet, flags) is not None

    def _on_result_select(self, _evt=None):
        sel = self.tv.selection()
        if not sel:
            return
        try:
            self._goto_match(self._matches[int(sel[0])])
        except (ValueError, IndexError):
            pass

    def _goto_match(self, m, center=True):
        """跳到該筆所在頁並畫螢光框。
        center=True:符合處置於可視區上方約 1/4(搜尋用);
        center=False:對齊頁頂,與 sync_to_page 一致(取代處導覽,兩邊才不錯位)。"""
        self._active_match = m
        page = m["page"]
        if self.layout and self._content_h > 0:
            lay = self.layout[page]
            if center:
                y = lay["y"] + m["rect"][1] * self.scale \
                    - self.canvas.winfo_height() * 0.25
            else:
                y = lay["y"] - GAP        # 頁頂(同 _scroll_to_page)
            # 此處捲動不觸發比例同步(改由下方 on_match_jump 明確帶另一邊到同一頁)
            self._sync_lock = True
            try:
                self.canvas.yview_moveto(max(0.0, min(y / self._content_h, 1.0)))
            finally:
                self._sync_lock = False
        self.page_index = page
        self.var_page.set(str(page + 1))
        self._render_visible()
        self._draw_highlight()
        self._notify_state()
        if self.on_match_jump is not None:
            try:
                self.on_match_jump(page)
            except Exception:
                pass

    def sync_to_page(self, idx):
        """被動跳到指定頁(供另一面板點選搜尋結果時同步;不回呼避免互推)。"""
        self._sync_lock = True
        try:
            self.show_page(idx)
        finally:
            self._sync_lock = False
        self._render_visible()
        self._draw_highlight()

    def _draw_highlight(self):
        """畫螢光框。highlight_all=True 時所有命中淡黃標示,目前選取者再加紅框。"""
        self.canvas.delete("search_hl")
        if not self.layout:
            return
        if self.highlight_all:
            for m in self._matches:
                if m is not self._active_match:
                    self._draw_hl_rect(m, "#fff0a0", "#e6b800", 1)
        if self._active_match is not None:
            self._draw_hl_rect(self._active_match, "#ffe14d", "#d40000", 2)
        self.canvas.tag_raise("search_hl")

    def _draw_hl_rect(self, m, fill, outline, width):
        lay = self.layout[m["page"]]
        x0, y0, x1, y1 = m["rect"]
        s = self.scale
        self.canvas.create_rectangle(
            lay["x"] + x0 * s - 2, lay["y"] + y0 * s - 1,
            lay["x"] + x1 * s + 2, lay["y"] + y1 * s + 1,
            outline=outline, width=width, fill=fill, stipple="gray50",
            tags=("search_hl",))

    # ----------------------------------------------- 外部驅動(比對檢視共用)
    def set_sync(self, cb):
        """設定捲動同步回呼:本檢視器主動捲動時呼叫 cb(fraction)。"""
        self._sync_cb = cb

    def sync_to(self, fraction):
        """被動捲到指定比例(_sync_lock 期間不回呼,避免兩邊互推成迴圈)。"""
        self._sync_lock = True
        try:
            self.canvas.yview_moveto(fraction)
        finally:
            self._sync_lock = False
        self._render_visible()
        self._draw_highlight()

    def _notify_state(self):
        if self.on_state is not None:
            try:
                self.on_state()
            except Exception:
                pass

    def toggle_search(self):
        """切換搜尋結果面板顯示/隱藏(供外部:各面板標題旁的搜尋鈕)。"""
        self._toggle_search()

    def run_search(self, text):
        """以指定文字搜尋並跳到第一筆(供外部標示取代處)。回傳命中數。"""
        self.var_search.set(text or "")
        self._do_search()
        if self._matches:
            self._goto_match(self._matches[0], center=False)
        else:
            self._draw_highlight()
        return len(self._matches)

    def goto_match_index(self, i):
        """跳到第 i 筆命中(供外部取代處導覽,對齊頁頂與另一邊一致)。"""
        if 0 <= i < len(self._matches):
            self._goto_match(self._matches[i], center=False)


def create_frame(parent, presets_dir=None, show_toolbar=True, show_open=True):
    """供 PDF 工具集主程式嵌入用。回傳含檢視器 UI 的 Frame。"""
    frame = ttk.Frame(parent)
    frame.app = App(frame, presets_dir=presets_dir,   # 掛在 frame 上供外部存取
                    show_toolbar=show_toolbar, show_open=show_open)
    return frame


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
