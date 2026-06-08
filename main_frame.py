"""嵌入式包裝 — 讓 PDF 工具集 跑在 Launcher 的分頁裡。

實作 create_frame(parent) -> ttk.Frame,由 Launcher 動態載入。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import tkinter as tk
from tkinter import ttk

_TOOL_ROOT = Path(__file__).parent


def _load_tool():
    """用 importlib 從絕對路徑載入 pdftools.py,給唯一模組名避免衝突。
    同時把工具根目錄加入 sys.path,讓 features.* 可被 import。"""
    if str(_TOOL_ROOT) not in sys.path:
        sys.path.insert(0, str(_TOOL_ROOT))
    spec = importlib.util.spec_from_file_location(
        "_pdf2_pdftools", _TOOL_ROOT / "pdftools.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_pdf2_pdftools"] = mod
    spec.loader.exec_module(mod)
    return mod


_tool = _load_tool()
_App = _tool.App


class _EmbeddedApp(_App):
    """嵌入 Launcher:不動全域樣式;預設檔放使用者家目錄,工具更新不會被清掉。"""

    def __init__(self, parent: tk.Widget) -> None:
        presets_dir = Path.home() / ".pdf2"
        super().__init__(parent, embed=True, presets_dir=presets_dir)

    def _setup_style(self) -> None:  # 不會被呼叫(embed=True),保險覆寫
        pass


def create_frame(parent: tk.Widget) -> ttk.Frame:
    frame = ttk.Frame(parent)
    _EmbeddedApp(frame)
    return frame
