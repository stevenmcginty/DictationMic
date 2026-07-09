@echo off
rem Smart App Control blocks freshly built (unsigned) DictationMic.exe
rem hashes until they earn cloud reputation. This starts the exact same
rem app from source using the signed Python runtime instead - same
rem settings, notes, shots and model. Safe to double-click twice: only
rem one copy ever runs.
start "DictationMic" /d "%~dp0" "%~dp0venv\Scripts\pythonw.exe" app.py
