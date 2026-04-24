#!/bin/bash
# Installer für die buffer_feeder Klipper-Extension (Python-Ansatz).
#
# Legt einen Symlink von klipper_extras/buffer_feeder.py nach
# ~/klipper/klippy/extras/buffer_feeder.py an. Danach ist
# [buffer_feeder <name>] ein valider Config-Abschnitt.
#
# Aufruf (auf dem Host mit Klipper, z.B. Raspberry Pi):
#   cd ~/BufferPLUS-klipper    # oder wo du das Repo geclont hast
#   ./install.sh
#
# Optional: Symlink für lll.cfg nach ~/printer_data/config/ :
#   ln -sf $(pwd)/lll.cfg ~/printer_data/config/lll.cfg
#
# Für Moonraker-Auto-Update siehe unten.

set -euo pipefail

KLIPPER_DIR="${KLIPPER_DIR:-${HOME}/klipper}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
EXT_SOURCE="${REPO_DIR}/klipper_extras/buffer_feeder.py"
EXT_TARGET="${KLIPPER_DIR}/klippy/extras/buffer_feeder.py"

echo "==> Klipper-Verzeichnis: ${KLIPPER_DIR}"
echo "==> Source-Datei:        ${EXT_SOURCE}"
echo "==> Ziel-Symlink:        ${EXT_TARGET}"

if [ ! -f "${EXT_SOURCE}" ]; then
    echo "FEHLER: ${EXT_SOURCE} nicht gefunden."
    echo "Wurde das Skript im Repo-Root ausgeführt?"
    exit 1
fi

if [ ! -d "${KLIPPER_DIR}/klippy/extras" ]; then
    echo "FEHLER: ${KLIPPER_DIR}/klippy/extras existiert nicht."
    echo "Ist Klipper installiert? Setze KLIPPER_DIR falls der Pfad abweicht:"
    echo "  KLIPPER_DIR=/pfad/zu/klipper ./install.sh"
    exit 1
fi

# Existenz prüfen — falls bereits ein NICHT-Symlink liegt, abbrechen.
if [ -e "${EXT_TARGET}" ] && [ ! -L "${EXT_TARGET}" ]; then
    echo "FEHLER: ${EXT_TARGET} existiert und ist kein Symlink."
    echo "Manuell wegräumen (sichern) und Skript erneut ausführen."
    exit 1
fi

ln -sf "${EXT_SOURCE}" "${EXT_TARGET}"
echo "==> Symlink gesetzt."

# Optionale Warnung falls lll.cfg nicht in printer_data ist.
PRINTER_CFG_DIR="${HOME}/printer_data/config"
if [ -d "${PRINTER_CFG_DIR}" ] && [ ! -e "${PRINTER_CFG_DIR}/lll.cfg" ]; then
    echo ""
    echo "HINWEIS: ${PRINTER_CFG_DIR}/lll.cfg existiert nicht."
    echo "Symlink anlegen mit:"
    echo "  ln -sf ${REPO_DIR}/lll.cfg ${PRINTER_CFG_DIR}/lll.cfg"
    echo "Danach in printer.cfg ergänzen:"
    echo "  [include lll.cfg]"
fi

cat <<EOF

==============================================================================
Installation abgeschlossen.

Nächste Schritte:
  1. Klipper neu starten:
       sudo systemctl restart klipper

  2. In printer.cfg sicherstellen:
       [pause_resume]
       [extruder]
       max_extrude_only_distance: 200
       [include lll.cfg]

  3. Bei erstem Start "BUFFER_STATE_DUMP" in der Klipper-Konsole —
     bestätigt, dass die Extension geladen wurde.

Optional für Moonraker Auto-Update — in moonraker.conf ergänzen:

[update_manager buffer_feeder]
type: git_repo
path: ${REPO_DIR}
origin: https://github.com/Avatarsia/BufferPLUS-klipper.git
primary_branch: python-ansatz
is_system_service: False
managed_services: klipper
==============================================================================
EOF
