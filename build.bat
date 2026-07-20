@echo off
chcp 65001 >nul
echo ================================================
echo   PDF 工具集 - 打包為 .exe
echo ================================================
echo.

echo [1/3] 安裝執行時依賴...
pip install PyMuPDF --quiet
if %errorlevel% neq 0 (
    echo 錯誤：無法安裝 PyMuPDF
    pause & exit /b 1
)

echo [2/3] 安裝 PyInstaller...
pip install pyinstaller --quiet
if %errorlevel% neq 0 (
    echo 錯誤：無法安裝 PyInstaller
    pause & exit /b 1
)

echo [3/3] 打包程式...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "PDF工具" ^
    --hidden-import=fitz ^
    --hidden-import=features.splitter ^
    --hidden-import=features.digital_signature ^
    --collect-binaries=fitz ^
    --clean ^
    pdftools.py

if %errorlevel% neq 0 (
    echo.
    echo 錯誤：打包失敗，請查看上方錯誤訊息
    pause & exit /b 1
)

echo.
echo ================================================
echo   打包完成！
echo   執行檔位置：dist\PDF工具.exe
echo ================================================
echo.
pause
