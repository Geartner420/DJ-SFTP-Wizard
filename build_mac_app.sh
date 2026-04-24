#!/bin/bash
set -e

cd "$(dirname "$0")"

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt pyinstaller

pyinstaller --windowed --name "DJ SFTP Wizard" sftp_browser.py

echo ""
echo "Fertig: dist/DJ SFTP Wizard.app"
echo "Diese App kannst du per Doppelklick starten."
