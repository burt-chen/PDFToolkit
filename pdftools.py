#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 工具集 — 桌機版

側邊欄分類功能列表 + 右側內容區。功能模組放在 features/ 下,每個模組
提供 create_frame(parent, **kwargs) -> ttk.Frame,由本程式動態載入。

新增功能 = 寫一個 features/<name>.py + 在 FEATURES 加一筆。
"""

import importlib
import inspect
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

FONT = ("Microsoft JhengHei UI", 12)


def app_dir():
    """程式所在資料夾。PyInstaller 打包後也能正確指向 .exe 旁邊。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
#  功能登錄表 — 新增功能就加一筆
#
#  每筆欄位:
#    id        — 唯一代號(供 cache / debug)
#    name      — 側邊欄顯示名稱
#    category  — 分類群組(同名歸同一區)
#    module    — features/ 下的模組名(import 路徑)
#    factory   — 模組內回傳 Frame 的函式名(預設 create_frame)
#    kwargs    — 可選,傳給 factory 的關鍵字參數
# ---------------------------------------------------------------------------

FEATURES = [
    {
        "id": "viewer",
        "name": "開啟 / 檢視 PDF",
        "category": "檢視",
        "module": "features.viewer",
        "factory": "create_frame",
    },
    {
        "id": "splitter",
        "name": "分割 PDF",
        "category": "頁面操作",
        "module": "features.splitter",
        "factory": "create_frame",
    },
    {
        "id": "replace_text",
        "name": "文字取代",
        "category": "文字處理",
        "module": "features.replace_text",
        "factory": "create_frame",
    },
    {
        "id": "digital_signature",
        "name": "電子章",
        "category": "簽章/用印",
        "module": "features.digital_signature",
        "factory": "create_frame",
    },
]


class App:
    def __init__(self, root, embed=False, presets_dir=None):
        """root 可為 tk.Tk / Toplevel / Frame。
        embed:  True 時不動全域樣式與字型(交給外部容器)。
        presets_dir: 給各 feature 使用的設定檔資料夾(嵌入時可指定家目錄)。
        """
        self.root = root
        self.embed = embed
        self.presets_dir = presets_dir

        if not embed and isinstance(root, (tk.Tk, tk.Toplevel)):
            self.root.title("PDF 工具集")
            self.root.geometry("1180x780")
            self.root.minsize(960, 640)
            try:
                self.root.state("zoomed")
            except tk.TclError:
                try:
                    self.root.attributes("-zoomed", True)
                except tk.TclError:
                    pass

        self._iid_to_feat = {}
        self._current_id = None
        # 各功能畫面快取:切換時只隱藏不銷毀,讓已開啟的內容(如檢視中的 PDF)持續存在
        self._feature_frames = {}
        self._placeholder = None
        self._error_widget = None

        if not embed:
            self._setup_style()
        self._build_ui()

    # ------------------------------------------------------------------ 樣式
    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.option_add("*Font", FONT)
        style.configure("TButton", padding=[10, 4])
        style.configure(
            "Accent.TButton", padding=[20, 6],
            font=("Microsoft JhengHei UI", 12, "bold"))
        style.configure("TLabelframe.Label",
                        font=("Microsoft JhengHei UI", 12, "bold"))
        # 側邊欄 Treeview
        style.configure(
            "Sidebar.Treeview", rowheight=30, font=FONT,
            background="#fafafa", fieldbackground="#fafafa",
            borderwidth=0)
        style.map("Sidebar.Treeview",
                  background=[("selected", "#cfe2ff")],
                  foreground=[("selected", "#000")])

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.sidebar_visible = True
        ttk.Style().configure(
            "Sidebar.Toggle.TButton",
            font=("Microsoft JhengHei UI", 16, "bold"), padding=[0, 8])

        # 收合後才顯示的左側細把手(▶ 展開);展開時不顯示
        self.rail = ttk.Frame(self.root, width=26)
        self.rail.pack_propagate(False)
        self.btn_expand = ttk.Button(
            self.rail, text="▶", style="Sidebar.Toggle.TButton",
            command=self._toggle_sidebar)
        self.btn_expand.pack(fill="both", expand=True, padx=1, pady=1)

        self.paned = paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(side="left", fill="both", expand=True)

        # 左側 sidebar
        self.sidebar = sidebar = ttk.Frame(paned, width=240)
        sidebar.pack_propagate(False)
        paned.add(sidebar, weight=0)

        # 標題列:「PDF 工具」+ 收合鈕(◀)
        title_bar = ttk.Frame(sidebar)
        title_bar.pack(fill="x", padx=4, pady=(10, 6))
        ttk.Label(
            title_bar, text="PDF 工具",
            font=("Microsoft JhengHei UI", 15, "bold"),
            anchor="w").pack(side="left")
        self.btn_collapse = ttk.Button(
            title_bar, text="◀", width=3, command=self._toggle_sidebar)
        self.btn_collapse.pack(side="right")
        ttk.Separator(sidebar, orient="horizontal").pack(fill="x", padx=4)

        self.tree = ttk.Treeview(
            sidebar, show="tree", selectmode="browse",
            style="Sidebar.Treeview")
        self.tree.pack(fill="both", expand=True, padx=4, pady=6)
        self.tree.tag_configure(
            "category",
            font=("Microsoft JhengHei UI", 10, "bold"),
            foreground="#666")

        # 依分類分組塞入
        cat_nodes = {}
        for feat in FEATURES:
            cat = feat["category"]
            if cat not in cat_nodes:
                cat_nodes[cat] = self.tree.insert(
                    "", "end", text=cat, open=True, tags=("category",))
            iid = self.tree.insert(
                cat_nodes[cat], "end",
                text=f"   {feat['name']}",
                tags=("feature",))
            self._iid_to_feat[iid] = feat

        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # 右側 content 容器(切換時隱藏舊 frame、保留其狀態,不銷毀)
        self.content = ttk.Frame(paned)
        paned.add(self.content, weight=1)

        self._show_placeholder()

        # 自動選第一個 feature(若有)
        if self._iid_to_feat:
            first_iid = next(iter(self._iid_to_feat))
            self.tree.selection_set(first_iid)
            self.tree.focus(first_iid)

    # ------------------------------------------------------------ 側邊欄收合
    def _toggle_sidebar(self):
        if self.sidebar_visible:
            # 收合:藏起側邊欄,左側改顯示細把手(▶)
            self.paned.forget(self.sidebar)
            self.rail.pack(side="left", fill="y", before=self.paned)
        else:
            # 展開:藏起把手,側邊欄放回
            self.rail.pack_forget()
            self.paned.insert(0, self.sidebar, weight=0)
        self.sidebar_visible = not self.sidebar_visible

    # ---------------------------------------------------------------- 切換
    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        feat = self._iid_to_feat.get(sel[0])
        if not feat:
            return  # 點到分類標題 — 不切換
        self._show_feature(feat)

    def _show_placeholder(self):
        if self._placeholder is None:
            self._placeholder = ttk.Frame(self.content)
            ttk.Label(
                self._placeholder,
                text="請從左側選擇功能",
                foreground="#888",
                font=("Microsoft JhengHei UI", 14)).pack(expand=True)
        self._placeholder.pack(fill="both", expand=True)

    def _hide_current(self):
        """隱藏目前顯示的內容(placeholder / 上一個功能 / 錯誤訊息),但不銷毀。"""
        if self._placeholder is not None:
            self._placeholder.pack_forget()
        if self._current_id and self._current_id in self._feature_frames:
            self._feature_frames[self._current_id].pack_forget()
        if self._error_widget is not None:
            self._error_widget.destroy()
            self._error_widget = None

    def _show_feature(self, feat):
        if self._current_id == feat["id"]:
            return
        self._hide_current()
        fid = feat["id"]
        frame = self._feature_frames.get(fid)
        if frame is None:
            # 第一次進入此功能 → 建立並快取;之後切回沿用同一個(狀態保留)
            try:
                mod = importlib.import_module(feat["module"])
                factory = getattr(mod, feat.get("factory", "create_frame"))
                kwargs = dict(feat.get("kwargs") or {})
                # 共用 presets_dir(嵌入 launcher 時由外部覆寫)
                if self.presets_dir is not None and "presets_dir" not in kwargs:
                    kwargs["presets_dir"] = self.presets_dir
                # 只對「接受對應參數」的功能注入檢視器回呼,讓它能把檔案/預覽
                # 丟到「檢視 PDF」開啟(如文字取代右鍵);其餘功能簽名不必改。
                try:
                    params = inspect.signature(factory).parameters
                    has_kw = any(p.kind == p.VAR_KEYWORD for p in params.values())
                    for cb in ("open_in_viewer", "open_bytes_in_viewer"):
                        if cb in params or has_kw:
                            kwargs.setdefault(cb, getattr(self, cb))
                except (TypeError, ValueError):
                    pass
                frame = factory(self.content, **kwargs)
                self._feature_frames[fid] = frame
            except Exception as e:
                self._error_widget = ttk.Label(
                    self.content,
                    text=f"載入「{feat['name']}」失敗：\n{e}",
                    foreground="#a00", font=FONT, justify="left")
                self._error_widget.pack(expand=True, padx=20, pady=20)
                self._current_id = None
                return
        frame.pack(fill="both", expand=True)
        self._current_id = fid

    # ------------------------------------------------ 跨功能:在檢視器開檔
    def _activate_viewer(self):
        """切換到「檢視 PDF」功能(同步側邊欄、建立畫面),回傳其 App 供開檔。"""
        feat = next((f for f in FEATURES if f["id"] == "viewer"), None)
        if feat is None:
            return None
        for iid, f in self._iid_to_feat.items():
            if f["id"] == "viewer":
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                break
        self._show_feature(feat)              # 已是目前功能時為無動作
        frame = self._feature_frames.get(feat["id"])
        return getattr(frame, "app", None)

    def open_in_viewer(self, path):
        """切換到「檢視 PDF」並開啟指定檔案。供其他功能(如文字取代右鍵)呼叫。"""
        app = self._activate_viewer()
        if app is not None:
            try:
                app.open_path(path)
            except Exception:
                pass

    def open_bytes_in_viewer(self, data, name="(預覽)"):
        """切換到「檢視 PDF」並以記憶體 bytes 開啟(供取代後預覽,不落地)。"""
        app = self._activate_viewer()
        if app is not None:
            try:
                app.open_bytes(data, name)
            except Exception:
                pass


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
