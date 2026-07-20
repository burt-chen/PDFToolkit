# PDF 工具集 (pdf2)

桌機版 PDF 多功能工具集。左側分類功能列、右側內容區;功能模組放在 `features/`,新增功能 = 寫一個模組 + 在 `pdftools.py` 的 `FEATURES` 加一筆。

目前已內建：
- **分割 PDF**(`features/splitter.py`)— 四種偵測模式 + 可編輯預覽 + 多PDF/ZIP 輸出 + 自訂組合預設
- **文字取代**(`features/replace_text.py`)— 搜尋 PDF 文字並批次取代/填寫
- **電子章**(`features/digital_signature.py`)— 批次處理單一 PDF 或資料夾，可用檔名篩選並顯示會處理的 PDF 清單；支援多筆電子章規則，每筆可獨立設定搜尋結果後方或指定座標、只蓋一次/每個搜尋結果/每頁；可選取 PDF 直接預覽蓋章結果且不先寫出檔案；規則可儲存成規則組後用下拉選單載入

---

## 執行方式

**透過 MyTools Launcher**(建議):在 launcher 工具清單安裝後直接開啟,依賴 `PyMuPDF` 由 launcher 依 `requirements.txt` 自動 pip 安裝。

**獨立執行**:Python 3.8+(內建 tkinter)。

```powershell
pip install PyMuPDF
python pdftools.py
```

---

## 目錄結構

```
PDF2/
├── pdftools.py              # 主程式:側邊欄 + 右側內容區 + 功能登錄表 FEATURES
├── main_frame.py            # launcher 嵌入入口(create_frame)
├── features/
│   ├── __init__.py
│   └── splitter.py          # 分割 PDF 功能(獨立可執行,亦提供 create_frame)
├── requirements.txt         # PyMuPDF
├── PDF工具.spec             # PyInstaller spec
├── build.bat                # 一鍵打包
└── README.md
```

---

## 新增功能

1. 在 `features/` 下建一個 `<feature>.py`,提供:
   ```python
   def create_frame(parent, **kwargs) -> ttk.Frame:
       frame = ttk.Frame(parent)
       # ... 建立 UI
       return frame
   ```
2. 在 `pdftools.py` 的 `FEATURES` 加一筆:
   ```python
   {
       "id": "唯一代號",
       "name": "顯示名稱",
       "category": "分類群組(同名歸同一區)",
       "module": "features.<feature>",
       "factory": "create_frame",
       # 可選: "kwargs": {...}  傳給 factory
   }
   ```
3. 主程式啟動時會自動按 `category` 分組顯示於左側 Treeview。

各功能可選地接受 `presets_dir`(由主程式統一傳入,嵌入 launcher 時為使用者家目錄,獨立執行為程式所在目錄)用於存放該功能的設定/預設檔。

---

## 嵌入 MyTools Launcher

`main_frame.py` 的 `create_frame(parent)` 由 launcher 動態載入。嵌入時:
- 不覆寫全域 ttk 樣式與字型(交由 launcher 統一)
- 各功能設定檔放 `~/.pdf2/`,工具更新重裝不會被清掉

加入 catalog 範例(`小工具管理/tools.json` 的 `tools` 陣列):

```json
{
  "id": "pdf2",
  "name": "PDF 工具集",
  "description": "桌機版 PDF 多功能工具集(分割 / 後續陸續新增)",
  "version": "0.1.0",
  "url": "https://github.com/burt-chen/PDF2/releases/download/v0.1.0/pdf2-v0.1.0.zip",
  "category": "資料處理",
  "homepage": "https://github.com/burt-chen/PDF2"
}
```

---

## 打包

```powershell
pip install pyinstaller
.\build.bat
```

產出 `dist\PDF工具.exe`。新增 feature 後需在 `build.bat` 與 `.spec` 補上對應的 `--hidden-import=features.<feature>`。
