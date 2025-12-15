@echo off
REM U.E.P 自動化構建批處理腳本 (Windows)
REM 
REM 此腳本會:
REM 1. 激活虛擬環境
REM 2. 運行構建腳本
REM 3. 顯示構建結果

echo ======================================
echo U.E.P 自動化構建工具
echo ======================================
echo.

REM 檢查虛擬環境是否存在
if not exist "env\Scripts\activate.bat" (
    echo [錯誤] 找不到虛擬環境
    echo 請確保虛擬環境位於 env\ 目錄下
    echo.
    pause
    exit /b 1
)

REM 激活虛擬環境
echo 正在激活虛擬環境...
call env\Scripts\activate.bat

REM 檢查 PyInstaller 是否已安裝
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [警告] 未安裝 PyInstaller
    echo 正在安裝 PyInstaller...
    pip install pyinstaller
    if errorlevel 1 (
        echo [錯誤] PyInstaller 安裝失敗
        pause
        exit /b 1
    )
)

echo.
echo 開始構建...
echo ======================================
echo.

REM 運行構建腳本
python build_app.py

REM 檢查構建結果
if errorlevel 1 (
    echo.
    echo ======================================
    echo [錯誤] 構建失敗
    echo ======================================
    echo.
    pause
    exit /b 1
) else (
    echo.
    echo ======================================
    echo [成功] 構建完成
    echo ======================================
    echo.
    echo 分發包位於 dist\ 目錄下
    echo.
)

pause
