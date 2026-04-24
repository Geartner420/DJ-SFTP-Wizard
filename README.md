# DJ SFTP Wizard

Grafisches SFTP-Tool fuer DJs auf macOS.

## Features

- SFTP-Verbindung per SSH-Key oder Passwort
- Lokaler Dateibrowser
- Remote-Dateibrowser
- Dateien und Ordner herunterladen
- Dateien und Ordner hochladen
- Fortschritt und Log

## Start auf macOS

```bash
python3 -m pip install -r requirements.txt
python3 sftp_browser.py
```

## Als macOS-App bauen

```bash
chmod +x build_mac_app.sh
./build_mac_app.sh
```

Die fertige App liegt danach hier:

```text
dist/DJ SFTP Wizard.app
```

Diese `.app` kannst du per Doppelklick starten oder in den Programme-Ordner ziehen.
