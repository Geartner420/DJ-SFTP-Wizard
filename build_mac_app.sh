#!/bin/bash
set -e

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-}"

find_python() {
  local candidates=(
    "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
    "/usr/local/bin/python3"
    "/opt/homebrew/bin/python3.13"
    "/opt/homebrew/bin/python3.12"
    "/opt/homebrew/bin/python3.11"
    "/opt/homebrew/bin/python3.10"
    "python3"
  )

  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c "import os, sys, tkinter, ssl, zipfile, xml.parsers.expat, xmlrpc.client, venv; real=os.path.realpath(sys.executable); raise SystemExit(1 if real.startswith('/Library/Developer/CommandLineTools/') else 0)" >/dev/null 2>&1; then
        echo "$candidate"
        return 0
      fi
    fi
  done

  return 1
}

if [[ -n "$PYTHON_BIN" ]] && ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Hinweis: PYTHON_BIN existiert nicht: $PYTHON_BIN"
  PYTHON_BIN=""
fi

if [[ -z "$PYTHON_BIN" ]]; then
  if PYTHON_BIN="$(find_python)"; then
    echo "Automatisch gefundenes Build-Python: $PYTHON_BIN"
  else
    echo ""
    echo "Fehler: Kein geeignetes Python fuer den App-Build gefunden."
    echo "Benoetigt wird Python mit tkinter, venv und funktionierenden Standardmodulen."
    echo ""
    echo "Empfohlen:"
    echo "  brew install python@3.12 python-tk@3.12"
    echo "  PYTHON_BIN=/opt/homebrew/bin/python3.12 ./build_mac_app.sh"
    echo ""
    echo "Alternativ Python von python.org installieren und dann:"
    echo "  ./build_mac_app.sh"
    exit 1
  fi
fi

./build_icon.sh

PYTHON_PATH="$($PYTHON_BIN -c 'import sys; print(sys.executable)')"
PYTHON_REALPATH="$($PYTHON_BIN -c 'import os,sys; print(os.path.realpath(sys.executable))')"
echo "Nutze Python: $PYTHON_PATH"
if [[ "$PYTHON_REALPATH" != "$PYTHON_PATH" ]]; then
  echo "Aufgeloest zu: $PYTHON_REALPATH"
fi

if [[ "$PYTHON_REALPATH" == /Library/Developer/CommandLineTools/* ]]; then
  echo ""
  echo "Fehler: Das Apple-CommandLineTools-Python ist fuer App-Builds hier ungeeignet."
  echo "Es erzeugt eine .app, die auf diesem Mac sofort wieder beendet wird."
  echo ""
  echo "Bitte verwende stattdessen ein vollwertiges Python mit tkinter, z. B.:"
  echo "  - Python von python.org"
  echo "  - oder ein anderes Python ueber PYTHON_BIN=/pfad/zu/python3"
  echo ""
  echo "Homebrew-Beispiel:"
  echo "  brew install python@3.12 python-tk@3.12"
  echo "  PYTHON_BIN=/opt/homebrew/bin/python3.12 ./build_mac_app.sh"
  echo ""
  echo "Vorher pruefen:"
  echo "  /opt/homebrew/bin/python3.12 -m tkinter"
  exit 1
fi

echo "Pruefe Python/Tkinter ..."
if ! $PYTHON_BIN -c "import tkinter" >/dev/null 2>&1; then
  echo ""
  echo "Fehler: Dieses Python hat kein funktionsfaehiges tkinter/_tkinter."
  echo "Die App wurde mit Tkinter gebaut und kann ohne dieses Modul nicht starten."
  echo ""
  echo "So pruefst du es manuell:"
  echo "  $PYTHON_BIN -m tkinter"
  echo ""
  echo "Loesungen auf macOS:"
  echo "  1. Python von python.org installieren (enthaelt tkinter auf macOS)."
  echo "  2. Oder bei Homebrew passend zu deiner Python-Version python-tk installieren,"
  echo "     z. B. fuer Python 3.13:"
  echo "     brew install python-tk@3.13"
  echo ""
  echo "Danach dieses Skript erneut ausfuehren."
  exit 1
fi

echo "Pruefe Python-Standardmodule ..."
if ! $PYTHON_BIN -c "import ssl, zipfile, xml.parsers.expat, xmlrpc.client" >/dev/null 2>&1; then
  echo ""
  echo "Fehler: Dieses Python ist lokal defekt oder unvollstaendig."
  echo "Mindestens eines der Standardmodule laesst sich nicht laden:"
  echo "  ssl, zipfile, xml.parsers.expat, xmlrpc.client"
  echo ""
  echo "Bitte ein anderes Python verwenden oder die Installation reparieren."
  echo "Homebrew-Beispiel:"
  echo "  brew install python@3.12 python-tk@3.12"
  echo "  PYTHON_BIN=/opt/homebrew/bin/python3.12 ./build_mac_app.sh"
  exit 1
fi

VENV_DIR=".venv-build"
if [[ -d "$VENV_DIR" ]] && ! "$VENV_DIR/bin/python" -c "import pip" >/dev/null 2>&1; then
  echo "Build-Umgebung ist unvollstaendig, erstelle sie neu."
  rm -rf "$VENV_DIR"
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Erstelle lokale Build-Umgebung: $VENV_DIR"
  $PYTHON_BIN -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"
echo "Nutze Build-Python: $($VENV_PY -c 'import sys; print(sys.executable)')"

$VENV_PY -m pip install --upgrade pip
$VENV_PY -m pip install -r requirements.txt pyinstaller

$VENV_PY -m PyInstaller --clean --noconfirm "DJ SFTP Wizard.spec"

echo ""
echo "Fertig: dist/DJ SFTP Wizard.app"
echo "Diese App kannst du per Doppelklick starten."
