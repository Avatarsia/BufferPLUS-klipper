#!/bin/bash
# Interaktiver Installer für die BufferPLUS-klipper (python-ansatz)-Extension.
#
# Führt nach einer Übersicht/Checkliste die ausgewählten Schritte automatisch
# aus. Jeder Schritt ist idempotent: ein bereits erledigter Schritt wird als
# "bereits gesetzt" erkannt und nicht erneut angewendet.
#
# Pfade können via Umgebungsvariablen überschrieben werden:
#   KLIPPER_DIR          (default: ~/klipper)
#   PRINTER_CFG_DIR      (default: ~/printer_data/config)
#   MOONRAKER_CONF       (default: ~/printer_data/config/moonraker.conf)
#   KLIPPER_SERVICE      (default: klipper)

set -eu

KLIPPER_DIR="${KLIPPER_DIR:-${HOME}/klipper}"
PRINTER_CFG_DIR="${PRINTER_CFG_DIR:-${HOME}/printer_data/config}"
MOONRAKER_CONF="${MOONRAKER_CONF:-${PRINTER_CFG_DIR}/moonraker.conf}"
KLIPPER_SERVICE="${KLIPPER_SERVICE:-klipper}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

EXT_SOURCE="${REPO_DIR}/klipper_extras/buffer_feeder.py"
EXT_TARGET="${KLIPPER_DIR}/klippy/extras/buffer_feeder.py"
CFG_SOURCE="${REPO_DIR}/lll.cfg"
CFG_TARGET="${PRINTER_CFG_DIR}/lll.cfg"
PRINTER_CFG="${PRINTER_CFG_DIR}/printer.cfg"

# ---------- Farben / Format ----------
if [ -t 1 ]; then
    C_BOLD='\033[1m'
    C_RED='\033[0;31m'
    C_GREEN='\033[0;32m'
    C_YELLOW='\033[0;33m'
    C_CYAN='\033[0;36m'
    C_RESET='\033[0m'
else
    C_BOLD=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_RESET=''
fi

say()  { printf '%b\n' "$*"; }
ok()   { printf '%b✔%b %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
miss() { printf '%b✘%b %s\n' "${C_RED}"   "${C_RESET}" "$*"; }
warn() { printf '%b!%b %s\n' "${C_YELLOW}" "${C_RESET}" "$*"; }
hr()   { printf '%b--------------------------------------------------------------------%b\n' "${C_CYAN}" "${C_RESET}"; }

ask_yn() {
    # $1 = prompt, $2 = default (y|n). Returns 0 for yes, 1 for no.
    local prompt="$1" default="${2:-n}" yn
    if [ "$default" = "y" ]; then
        printf '%b?%b %s [Y/n]: ' "${C_CYAN}" "${C_RESET}" "$prompt"
    else
        printf '%b?%b %s [y/N]: ' "${C_CYAN}" "${C_RESET}" "$prompt"
    fi
    read -r yn || yn=""
    yn="${yn:-$default}"
    case "$yn" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------- Preflight ----------

if [ ! -f "${EXT_SOURCE}" ]; then
    say "${C_RED}FEHLER:${C_RESET} ${EXT_SOURCE} nicht gefunden."
    say "Script im Repo-Root ausführen."
    exit 1
fi

if [ ! -d "${KLIPPER_DIR}/klippy/extras" ]; then
    say "${C_RED}FEHLER:${C_RESET} ${KLIPPER_DIR}/klippy/extras existiert nicht."
    say "Ist Klipper installiert? KLIPPER_DIR=<pfad> ./install.sh falls anderer Pfad."
    exit 1
fi

# ---------- Status ermitteln ----------

status_extension() {
    if [ -L "${EXT_TARGET}" ]; then
        local link; link="$(readlink "${EXT_TARGET}")"
        if [ "${link}" = "${EXT_SOURCE}" ]; then
            echo "installed"
        else
            echo "wrong_symlink:${link}"
        fi
    elif [ -e "${EXT_TARGET}" ]; then
        echo "regular_file"
    else
        echo "missing"
    fi
}

status_cfg() {
    if [ -L "${CFG_TARGET}" ]; then
        local link; link="$(readlink "${CFG_TARGET}")"
        if [ "${link}" = "${CFG_SOURCE}" ]; then
            echo "installed"
        else
            echo "wrong_symlink:${link}"
        fi
    elif [ -e "${CFG_TARGET}" ]; then
        echo "regular_file"
    else
        echo "missing"
    fi
}

status_printer_cfg_include() {
    [ -f "${PRINTER_CFG}" ] || { echo "no_printer_cfg"; return; }
    if grep -qE '^\s*\[\s*include\s+lll\.cfg\s*\]' "${PRINTER_CFG}"; then
        echo "present"
    else
        echo "missing"
    fi
}

status_printer_cfg_pauseresume() {
    [ -f "${PRINTER_CFG}" ] || { echo "no_printer_cfg"; return; }
    if grep -qE '^\s*\[pause_resume\]' "${PRINTER_CFG}"; then
        echo "present"
    else
        echo "missing"
    fi
}

status_moonraker() {
    [ -f "${MOONRAKER_CONF}" ] || { echo "no_moonraker"; return; }
    if grep -qE '^\s*\[update_manager\s+buffer_feeder\]' "${MOONRAKER_CONF}"; then
        echo "present"
    else
        echo "missing"
    fi
}

# ---------- Banner + Status ----------

hr
say "${C_BOLD}BufferPLUS-klipper Installer (python-ansatz)${C_RESET}"
hr
say "Repo:          ${REPO_DIR}"
say "Klipper:       ${KLIPPER_DIR}"
say "Printer-Cfg:   ${PRINTER_CFG_DIR}"
say "Moonraker:     ${MOONRAKER_CONF}"
say "Klipper-Svc:   ${KLIPPER_SERVICE}"
hr
say "${C_BOLD}Aktueller Zustand:${C_RESET}"

S_EXT="$(status_extension)"
case "${S_EXT}" in
    installed)  ok "Extension-Symlink: ${EXT_TARGET}" ;;
    missing)    miss "Extension-Symlink fehlt: ${EXT_TARGET}" ;;
    wrong_symlink:*) warn "Extension-Symlink zeigt auf ${S_EXT#wrong_symlink:} (nicht auf Repo)" ;;
    regular_file)    warn "${EXT_TARGET} existiert als normale Datei (kein Symlink)" ;;
esac

S_CFG="$(status_cfg)"
case "${S_CFG}" in
    installed)  ok "Config-Symlink: ${CFG_TARGET}" ;;
    missing)    miss "Config-Symlink fehlt: ${CFG_TARGET}" ;;
    wrong_symlink:*) warn "Config-Symlink zeigt auf ${S_CFG#wrong_symlink:}" ;;
    regular_file)    warn "${CFG_TARGET} existiert als normale Datei" ;;
esac

S_INC="$(status_printer_cfg_include)"
case "${S_INC}" in
    present)        ok "printer.cfg enthält [include lll.cfg]" ;;
    missing)        miss "printer.cfg: [include lll.cfg] fehlt" ;;
    no_printer_cfg) warn "${PRINTER_CFG} nicht gefunden" ;;
esac

S_PR="$(status_printer_cfg_pauseresume)"
case "${S_PR}" in
    present)        ok "printer.cfg enthält [pause_resume]" ;;
    missing)        miss "printer.cfg: [pause_resume] fehlt" ;;
    no_printer_cfg) : ;;
esac

S_MR="$(status_moonraker)"
case "${S_MR}" in
    present)        ok "moonraker.conf enthält [update_manager buffer_feeder]" ;;
    missing)        miss "moonraker.conf: [update_manager buffer_feeder] fehlt (optional)" ;;
    no_moonraker)   warn "${MOONRAKER_CONF} nicht gefunden (Moonraker-Update-Manager wird übersprungen)" ;;
esac

hr
say "${C_BOLD}Auswahl der Schritte:${C_RESET}"
say "Du kannst jeden Schritt einzeln bestätigen, oder mit 'A' alle fehlenden ausführen."
say "Enter = Default (siehe [] am Ende). 'q' = Abbruch."
echo ""

# ---------- Menü ----------

ACTIONS=""   # wird mit den zu erledigenden Aktionen gefüllt

printf '%bWillst du alle fehlenden Schritte auf einmal ausführen (ohne weitere Rückfragen)?%b [y/N]: ' "${C_CYAN}" "${C_RESET}"
read -r ALL_YN || ALL_YN=""
ALL_YN="${ALL_YN:-n}"
case "${ALL_YN}" in
    [yY]|[yY][eE][sS]) AUTO_ALL=1 ;;
    *) AUTO_ALL=0 ;;
esac

# Hilfsfunktion: falls AUTO_ALL und Status=missing → automatisch ja; sonst fragen.
want() {
    # $1 = status, $2 = prompt
    local status="$1" prompt="$2"
    case "$status" in
        installed|present) return 1 ;;  # nichts zu tun
    esac
    if [ "${AUTO_ALL}" = "1" ]; then
        return 0
    fi
    if ask_yn "${prompt}" y; then
        return 0
    fi
    return 1
}

# 1) Extension
if want "${S_EXT}" "Extension-Symlink ${EXT_TARGET} anlegen"; then
    ACTIONS="${ACTIONS} ext"
fi

# 2) Config
if want "${S_CFG}" "Config-Symlink ${CFG_TARGET} anlegen"; then
    ACTIONS="${ACTIONS} cfg"
fi

# 3) printer.cfg-Include
if [ -f "${PRINTER_CFG}" ] && want "${S_INC}" "[include lll.cfg] in printer.cfg am Ende ergänzen"; then
    ACTIONS="${ACTIONS} inc"
fi

# 4) [pause_resume] prüfen/warnen (nicht automatisch, weil ggf. in einer anderen Inkludedatei steht)
if [ -f "${PRINTER_CFG}" ] && [ "${S_PR}" = "missing" ]; then
    say "${C_YELLOW}Hinweis:${C_RESET} [pause_resume] fehlt in printer.cfg."
    say "Manchmal steht das in einer eingebundenen Datei (macros.cfg o.ä.)."
    say "Ich ergänze das NICHT automatisch — bitte selbst prüfen/ergänzen."
fi

# 5) Moonraker
if [ "${S_MR}" = "missing" ] && want "${S_MR}" "[update_manager buffer_feeder] in moonraker.conf ergänzen (optional, für Auto-Update)"; then
    ACTIONS="${ACTIONS} mr"
fi

# 6) Klipper-Restart
RESTART=0
if [ -n "${ACTIONS}" ]; then
    if [ "${AUTO_ALL}" = "1" ] || ask_yn "Klipper nach den Änderungen neu starten (sudo systemctl restart ${KLIPPER_SERVICE})" y; then
        RESTART=1
    fi
fi

if [ -z "${ACTIONS}" ]; then
    hr
    ok "Nichts zu tun — alles ist bereits eingerichtet."
    exit 0
fi

hr
say "${C_BOLD}Führe aus:${C_RESET}${ACTIONS}"
hr

# ---------- Ausführung ----------

for ACT in $ACTIONS; do
    case "$ACT" in
        ext)
            if [ -e "${EXT_TARGET}" ] && [ ! -L "${EXT_TARGET}" ]; then
                warn "${EXT_TARGET} ist eine normale Datei. Ich mache ein Backup: ${EXT_TARGET}.bak"
                mv "${EXT_TARGET}" "${EXT_TARGET}.bak"
            fi
            ln -sf "${EXT_SOURCE}" "${EXT_TARGET}"
            ok "Extension-Symlink gesetzt."
            ;;
        cfg)
            mkdir -p "${PRINTER_CFG_DIR}"
            if [ -e "${CFG_TARGET}" ] && [ ! -L "${CFG_TARGET}" ]; then
                warn "${CFG_TARGET} ist eine normale Datei. Backup: ${CFG_TARGET}.bak"
                mv "${CFG_TARGET}" "${CFG_TARGET}.bak"
            fi
            ln -sf "${CFG_SOURCE}" "${CFG_TARGET}"
            ok "Config-Symlink gesetzt."
            ;;
        inc)
            # Backup vorher
            cp -a "${PRINTER_CFG}" "${PRINTER_CFG}.bak.$(date +%Y%m%d-%H%M%S)"
            # Klipper's SAVE_CONFIG schreibt automatisch generierte
            # Config-Werte (PID, Probe-Offsets etc.) in einen Block am
            # Dateiende, markiert durch `#*# <------ SAVE_CONFIG ----->`.
            # Dieser Block MUSS die letzten Zeilen bleiben — alles
            # dahinter wird beim nächsten SAVE_CONFIG zerschossen.
            # Daher: Include VOR diesem Block einfügen, nicht anhängen.
            if grep -qE '^#\*# <-+[[:space:]]*SAVE_CONFIG[[:space:]]*-+>' "${PRINTER_CFG}"; then
                awk '
                    /^#\*# <-+[[:space:]]*SAVE_CONFIG[[:space:]]*-+>/ && !inserted {
                        print ""
                        print "# Auto-eingefuegt durch BufferPLUS-klipper installer"
                        print "[include lll.cfg]"
                        print ""
                        inserted = 1
                    }
                    { print }
                ' "${PRINTER_CFG}" > "${PRINTER_CFG}.tmp"
                mv "${PRINTER_CFG}.tmp" "${PRINTER_CFG}"
                ok "[include lll.cfg] VOR SAVE_CONFIG-Block eingefügt (Backup erstellt)."
            else
                printf '\n# Auto-eingefuegt durch BufferPLUS-klipper installer\n[include lll.cfg]\n' >> "${PRINTER_CFG}"
                ok "[include lll.cfg] an printer.cfg angehängt (Backup erstellt, kein SAVE_CONFIG-Block vorhanden)."
            fi
            ;;
        mr)
            cp -a "${MOONRAKER_CONF}" "${MOONRAKER_CONF}.bak.$(date +%Y%m%d-%H%M%S)"
            cat <<EOF >> "${MOONRAKER_CONF}"

# Auto-eingefügt durch BufferPLUS-klipper installer
[update_manager buffer_feeder]
type: git_repo
path: ${REPO_DIR}
origin: https://github.com/Avatarsia/BufferPLUS-klipper.git
primary_branch: python-ansatz
is_system_service: False
managed_services: ${KLIPPER_SERVICE}
EOF
            ok "Moonraker-Update-Manager-Eintrag ergänzt (Backup erstellt)."
            ;;
    esac
done

if [ "${RESTART}" = "1" ]; then
    say ""
    if sudo -n true 2>/dev/null; then
        SUDO="sudo"
    else
        warn "Für den Klipper-Restart wird sudo benötigt — du wirst jetzt nach dem Passwort gefragt."
        SUDO="sudo"
    fi
    if ${SUDO} systemctl restart "${KLIPPER_SERVICE}"; then
        ok "Klipper-Service neu gestartet."
    else
        warn "Klipper-Restart fehlgeschlagen — manuell prüfen: sudo systemctl status ${KLIPPER_SERVICE}"
    fi
fi

# ---------- Abschluss ----------

hr
say "${C_BOLD}Fertig.${C_RESET}"
hr
say "Was gemacht wurde:"
for ACT in $ACTIONS; do
    case "$ACT" in
        ext) say "  - Extension-Symlink ${EXT_TARGET} → ${EXT_SOURCE}" ;;
        cfg) say "  - Config-Symlink ${CFG_TARGET} → ${CFG_SOURCE}" ;;
        inc) say "  - [include lll.cfg] an printer.cfg angehängt (Backup: ${PRINTER_CFG}.bak.*)" ;;
        mr)  say "  - [update_manager buffer_feeder] in moonraker.conf ergänzt (Backup: ${MOONRAKER_CONF}.bak.*)" ;;
    esac
done
[ "${RESTART}" = "1" ] && say "  - Klipper-Service neu gestartet."

say ""
say "${C_BOLD}Offene To-Dos (manuell zu prüfen):${C_RESET}"
say "  - [pause_resume] in printer.cfg vorhanden? (manchmal in includierter macros.cfg)"
say "  - [extruder] max_extrude_only_distance >= 200 ?"
say "  - Nach Klipper-Boot: 'BUFFER_STATE_DUMP' in der Konsole — die Extension"
say "    sollte 'state = IDLE' und die Sensor-States ausgeben."
say ""
say "${C_BOLD}Recovery-Cheatsheet (im Mainsail-Terminal):${C_RESET}"
say "  - BUFFER_STATE_DUMP        → kompletten State + Sensoren ausgeben"
say "  - BUFFER_CLEAR_JAM         → JAM-Lockout aufheben (nach Ursachenbehebung)"
say "  - STOP_BUFFER_FILL         → laufenden Fill/Grip/Manual abbrechen"
say "  - BUFFER_AUTO_OFF          → Bang-Bang aus + alle Recovery-Flags clearen"
say "  - BUFFER_AUTO_ON           → Bang-Bang wieder an"
say "  - BUFFER_HALT              → Sofortstopp (sticky)"
say ""
say "${C_BOLD}Vor dem ersten LOAD_FILAMENT:${C_RESET}"
say "  - MEASURE_LOAD_START → Feed-Taster → Filament bis zum Hotend fuettern →"
say "    Feed-Taster wieder → angezeigte Distanz minus 10-20mm in lll.cfg"
say "    als 'load_fast_distance' eintragen. Ohne Kalibrierung pumpt Phase 1"
say "    den Default 1000mm — meist deutlich zu viel."
say ""
