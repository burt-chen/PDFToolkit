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

備註:以 PyMuPDF 把每頁渲染成點陣圖顯示,不支援選取文字;
要取出文字請改用其他功能。
"""

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
    def __init__(self, root, presets_dir=None):
        """root 可為 tk.Tk / Toplevel / Frame(嵌入用)。presets_dir 此功能未用。"""
        self.root = root
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
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=6, pady=(6, 2))

        ttk.Button(bar, text="開啟", command=self._open).pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)

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

        # 檢視區(Canvas + 雙捲軸)
        wrap = ttk.Frame(self.root)
        wrap.pack(fill="both", expand=True, padx=6, pady=(2, 6))
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
        self._setup_dnd()

        self.lbl_status = ttk.Label(self.root, text="", foreground="#555",
                                    anchor="w")
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
        self.lbl_status.config(text=f"{Path(path).name}（共 {len(doc)} 頁）")
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
        """依捲動位置更新「目前頁」(可視區頂端的那頁)。"""
        if not self.layout:
            return
        top = self.canvas.canvasy(0)
        idx = 0
        # 取第一個底端仍在可視頂端以下的頁(= 最上方該列的第一頁)
        for i, lay in enumerate(self.layout):
            if lay["y"] + lay["h"] > top + 4:
                idx = i
                break
        self.page_index = idx
        self.var_page.set(str(idx + 1))

    # ----------------------------------------------------------------- 事件
    def _on_yscroll(self, first, last):
        self.vsb.set(first, last)
        # 捲動頻繁 → debounce 後再做延遲渲染與頁碼更新
        if self._vis_job is not None:
            self.canvas.after_cancel(self._vis_job)
        self._vis_job = self.canvas.after(20, self._after_scroll)

    def _after_scroll(self):
        self._vis_job = None
        self._render_visible()
        self._update_current_page()

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


def create_frame(parent, presets_dir=None):
    """供 PDF 工具集主程式嵌入用。回傳含檢視器 UI 的 Frame。"""
    frame = ttk.Frame(parent)
    frame.app = App(frame, presets_dir=presets_dir)   # 掛在 frame 上供外部存取
    return frame


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
