@echo off
chcp 65001 > nul
echo.
echo  ============================================
echo   VideoText Maker  -  http://localhost:5001
echo  ============================================
echo.
echo  Checking / installing dependencies...
pip install -q -r requirements.txt
echo.
echo  Starting...
python app.py
pause
