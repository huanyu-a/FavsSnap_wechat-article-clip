@echo off
chcp 65001 >nul
cd /d "%~dp0"
"C:/ProgramData/anaconda3/envs/python/python.exe" -u "FavsSnap_wechat-article-clip.py" %*
pause
