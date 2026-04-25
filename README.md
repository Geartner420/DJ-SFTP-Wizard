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

## Wichtiger Hinweis zu tkinter auf macOS

Die App benutzt `tkinter` fuer die GUI. Wenn beim Start die App sofort wieder
zugeht oder im Terminal `No module named 'tkinter'` bzw. `_tkinter` erscheint,
dann liegt es nicht am SFTP-Code, sondern am installierten Python.

Pruefe dein Python so:

```bash
python3 -m tkinter
```

Wenn dabei kein kleines Testfenster aufgeht, dann ist dein Python ohne
funktionsfaehiges Tk gebaut.

Moegliche Loesungen:

```bash
# Homebrew-Beispiel fuer Python 3.13
brew install python-tk@3.13
```

Oder ein aktuelles Python direkt von `python.org` installieren. Die macOS-
Installer von python.org enthalten laut offizieller Python-Doku bereits
`tkinter`/Tcl-Tk.

## Als macOS-App bauen

```bash
chmod +x build_mac_app.sh
chmod +x build_icon.sh
./build_mac_app.sh
```

Wichtig: Nicht das Apple-`python3` aus den Command Line Tools fuer den App-Build
verwenden. Dieses Python kann zwar laufen, erzeugt hier aber eine `.app`, die
sich direkt wieder beendet. Verwende fuer den Build lieber ein Python von
`python.org` oder uebergib den Interpreter explizit:

```bash
PYTHON_BIN=/usr/local/bin/python3 ./build_mac_app.sh
```

Die fertige App liegt danach hier:

```text
dist/DJ SFTP Wizard.app
```

Diese `.app` kannst du per Doppelklick starten oder in den Programme-Ordner ziehen.

## Verbindungsdaten merken

Die App merkt sich jetzt die zuletzt verwendeten Werte fuer:

- Host
- Port
- User
- Login-Modus
- SSH-Key-Pfad
- lokaler Pfad
- Remote-Pfad

Auf macOS werden Passwort und Key-Passphrase getrennt im Schluesselbund
abgelegt, damit sie nicht nur im Code oder in einer Textdatei verschwinden.
