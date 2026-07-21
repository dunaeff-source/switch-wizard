@echo off
chcp 65001 >nul
echo === Сборка switch-wizard.exe ===
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed ^
  --name switch-wizard ^
  --hidden-import serial.tools.list_ports ^
  main.py
copy /Y profiles.yaml dist\profiles.yaml
echo.
echo Готово: dist\switch-wizard.exe  (рядом должен лежать profiles.yaml)
pause
