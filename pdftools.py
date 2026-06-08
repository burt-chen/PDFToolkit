#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 工具集 — 桌機版

側邊欄分類功能列表 + 右側內容區。功能模組放在 features/ 下,每個模組
提供 create_frame(parent, **kwargs) -> ttk.Frame,由本程式動態載入。

新增功能 = 寫一個 features/<name>.py + 在 FEATURES 加一筆。
"""

import importlib
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
        "id": "splitter",
        "name": "分割 PDF",
        "category": "頁面操作",
        "module": "features.splitter",
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
        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # 左側 sidebar
        sidebar = ttk.Frame(paned, width=240)
        sidebar.pack_propagate(False)
        paned.add(sidebar, weight=0)

        title = ttk.Label(
            sidebar, text="  PDF 工具",
            font=("Microsoft JhengHei UI", 15, "bold"),
            anchor="w")
        title.pack(fill="x", padx=4, pady=(10, 6))
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

        # 右側 content 容器(每次切換 destroy 舊 frame)
        self.content = ttk.Frame(paned)
        paned.add(self.content, weight=1)

        self._show_placeholder()

        # 自動選第一個 feature(若有)
        if self._iid_to_feat:
            first_iid = next(iter(self._iid_to_feat))
            self.tree.selection_set(first_iid)
            self.tree.focus(first_iid)

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
        self._clear_content()
        ttk.Label(
            self.content,
            text="請從左側選擇功能",
            foreground="#888",
            font=("Microsoft JhengHei UI", 14)).pack(expand=True)

    def _clear_content(self):
        for c in self.content.winfo_children():
            c.destroy()

    def _show_feature(self, feat):
        if self._current_id == feat["id"]:
            return
        self._clear_content()
        try:
            mod = importlib.import_module(feat["module"])
            factory = getattr(mod, feat.get("factory", "create_frame"))
            kwargs = dict(feat.get("kwargs") or {})
            # 共用 presets_dir(嵌入 launcher 時由外部覆寫)
            if self.presets_dir is not None and "presets_dir" not in kwargs:
                kwargs["presets_dir"] = self.presets_dir
            frame = factory(self.content, **kwargs)
            frame.pack(fill="both", expand=True)
            self._current_id = feat["id"]
        except Exception as e:
            ttk.Label(
                self.content,
                text=f"載入「{feat['name']}」失敗：\n{e}",
                foreground="#a00", font=FONT,
                justify="left").pack(expand=True, padx=20, pady=20)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
