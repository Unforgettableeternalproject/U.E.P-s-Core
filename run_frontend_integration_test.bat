@echo off
REM 運行前端整合測試
REM 此腳本會啟動完整的系統循環（包含前端）

echo ========================================
echo UEP 前端整合測試
echo ========================================
echo.

REM 激活虛擬環境
call .\env\Scripts\activate.bat

REM 設置 PYTHONPATH
set PYTHONPATH=%CD%

echo 啟動測試...
echo.

REM 運行測試（使用 -v 詳細模式，-s 顯示輸出）
pytest integration_tests\test_frontend_system_integration.py -v -s --tb=short

echo.
echo ========================================
echo 測試完成
echo ========================================
pause
