@echo off
rem Builds "Pocket Rogues Save Manager.exe" into dist\ (needs: pip install pyinstaller)
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "Pocket Rogues Save Manager" --icon assets\icon.ico --add-data "assets\icon.ico;assets" save_manager_ui.py
