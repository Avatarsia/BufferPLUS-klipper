#!/bin/bash
# Interaktiver Installer fuer die BufferPLUS-klipper-Extension.
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
CURRENT_BRANCH="$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"

EXT_SOURCE="${REPO_DIR}/klipper_extras/buffer_feeder.py"
EXT_TARGET="${KLIPPER_DIR}/klippy/extras/buffer_feeder.py"
EXT_DIR="${REPO_DIR}/klipper_extras"
EXT_TARGET_DIR="${KLIPPER_DIR}/klippy/extras"
CFG_SOURCE="${REPO_DIR}/lll.cfg"
CFG_TARGET="${PRINTER_CFG_DIR}/lll.cfg"
PRINTER_CFG="${PRINTER_CFG_DIR}/printer.cfg"

collect_ext_sub_modules() {
    # All Python sidecar modules ship next to buffer_feeder.py and are
    # symlinked flat into klippy/extras/. Exclude the main entry-point and
    # our local package marker; Klipper already owns klippy/extras/__init__.py.
    local sub_path sub_name
    EXT_SUB_MODULES=()
    for sub_path in "${EXT_DIR}"/*.py; do
        [ -e "${sub_path}" ] || continue
        sub_name="$(basename "${sub_path}")"
        case "${sub_name}" in
            buffer_feeder.py|__init__.py)
                continue
                ;;
        esac
        EXT_SUB_MODULES+=("${sub_name}")
    done
}

collect_ext_sub_modules

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
    local link sub sub_src sub_dst sidecar_issues
    if [ -L "${EXT_TARGET}" ]; then
        link="$(readlink "${EXT_TARGET}")"
        if [ "${link}" != "${EXT_SOURCE}" ]; then
            echo "wrong_symlink:${link}"
            return
        fi
    elif [ -e "${EXT_TARGET}" ]; then
        echo "regular_file"
        return
    else
        echo "missing"
        return
    fi

    sidecar_issues=""
    for sub in "${EXT_SUB_MODULES[@]}"; do
        sub_src="${EXT_DIR}/${sub}"
        sub_dst="${EXT_TARGET_DIR}/${sub}"
        if [ -L "${sub_dst}" ]; then
            link="$(readlink "${sub_dst}")"
            if [ "${link}" != "${sub_src}" ]; then
                sidecar_issues="${sidecar_issues:+${sidecar_issues}, }${sub} -> ${link}"
            fi
        elif [ -e "${sub_dst}" ]; then
            sidecar_issues="${sidecar_issues:+${sidecar_issues}, }${sub} (regular file)"
        else
            sidecar_issues="${sidecar_issues:+${sidecar_issues}, }${sub} (missing)"
        fi
    done

    if [ -n "${sidecar_issues}" ]; then
        echo "sidecars:${sidecar_issues}"
    else
        echo "installed"
    fi
}

status_cfg() {
    # Config wird als KOPIE installiert (nicht Symlink), damit Mainsail
    # sie direkt editieren kann. Mögliche Zustände:
    #   missing          — Datei existiert nicht
    #   legacy_symlink   — Symlink auf Repo-Version (alter Install-Stil)
    #   wrong_symlink:X  — Symlink auf andere Datei
    #   copy_in_sync     — normale Datei, Inhalt identisch zur Repo-Version
    #   copy_diverged    — normale Datei, Inhalt weicht von Repo-Version ab
    if [ -L "${CFG_TARGET}" ]; then
        local link; link="$(readlink "${CFG_TARGET}")"
        if [ "${link}" = "${CFG_SOURCE}" ]; then
            echo "legacy_symlink"
        else
            echo "wrong_symlink:${link}"
        fi
    elif [ -f "${CFG_TARGET}" ]; then
        if cmp -s "${CFG_TARGET}" "${CFG_SOURCE}"; then
            echo "copy_in_sync"
        else
            echo "copy_diverged"
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
say "${C_BOLD}BufferPLUS-klipper Installer (${CURRENT_BRANCH})${C_RESET}"
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
    sidecars:*) warn "Extension-Symlink ok, aber Sub-Modul-Symlinks fehlen/abweichen: ${S_EXT#sidecars:}" ;;
    regular_file)    warn "${EXT_TARGET} existiert als normale Datei (kein Symlink)" ;;
esac

S_CFG="$(status_cfg)"
case "${S_CFG}" in
    copy_in_sync)    ok "Config (Kopie): ${CFG_TARGET} — identisch mit Repo-Version" ;;
    copy_diverged)   warn "Config (Kopie): ${CFG_TARGET} — unterscheidet sich von Repo-Version" ;;
    legacy_symlink)  warn "Config: ${CFG_TARGET} ist Symlink — sollte Kopie sein, damit Mainsail editieren kann" ;;
    wrong_symlink:*) warn "Config-Symlink zeigt auf ${S_CFG#wrong_symlink:} (nicht aufs Repo)" ;;
    regular_file)    warn "${CFG_TARGET} existiert in unklarem Zustand (weder Datei noch Symlink)" ;;
    missing)         miss "Config fehlt: ${CFG_TARGET}" ;;
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

# Hilfsfunktion: falls AUTO_ALL und Status nicht "alles ok" → automatisch ja; sonst fragen.
want() {
    # $1 = status, $2 = prompt
    local status="$1" prompt="$2"
    case "$status" in
        installed|present|copy_in_sync) return 1 ;;  # nichts zu tun
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

# 2) Config — wird als Kopie installiert (Mainsail-editierbar).
# Verhalten je nach aktuellem Zustand:
#   missing        → kopieren
#   legacy_symlink → Symlink ersetzen durch Kopie
#   wrong_symlink  → Symlink ersetzen durch Kopie (nach Backup)
#   regular_file   → Spezialfall, Backup + neu kopieren
#   copy_diverged  → Diff zeigen, User entscheidet (k = behalten / r = Repo nehmen)
#   copy_in_sync   → nichts zu tun
case "${S_CFG}" in
    missing)
        if want "${S_CFG}" "Config ${CFG_TARGET} aus Repo kopieren"; then
            ACTIONS="${ACTIONS} cfg_copy"
        fi
        ;;
    legacy_symlink|wrong_symlink:*)
        if want "${S_CFG}" "Config ${CFG_TARGET} ist Symlink — durch Kopie ersetzen (Mainsail kann dann editieren)"; then
            ACTIONS="${ACTIONS} cfg_replace_link"
        fi
        ;;
    regular_file)
        if want "${S_CFG}" "Config ${CFG_TARGET} ist in unklarem Zustand — Backup machen und neu aus Repo kopieren"; then
            ACTIONS="${ACTIONS} cfg_replace_link"
        fi
        ;;
    copy_diverged)
        say ""
        say "${C_BOLD}Lokale ${CFG_TARGET} weicht von Repo-Version ab.${C_RESET}"
        say "Diff (lokale ↔ Repo, gekürzt auf 60 Zeilen):"
        diff -u "${CFG_TARGET}" "${CFG_SOURCE}" | head -60 || true
        say ""
        say "Optionen:"
        say "  k) ${C_BOLD}Lokale Version behalten${C_RESET} — Repo-Updates werden nicht übernommen"
        say "  r) Repo-Version übernehmen — lokale wird gesichert nach ${CFG_TARGET}.bak.<ts>"
        if [ "${AUTO_ALL}" = "1" ]; then
            say "${C_YELLOW}AUTO-Modus: lokale Version wird beibehalten (k).${C_RESET}"
        else
            CHOICE=""
            while true; do
                read -r -p "Auswahl [k/r] (default k): " CHOICE
                CHOICE="${CHOICE:-k}"
                case "${CHOICE}" in
                    k|K) break ;;
                    r|R) ACTIONS="${ACTIONS} cfg_replace_with_backup"; break ;;
                    *) say "Bitte 'k' oder 'r' eingeben." ;;
                esac
            done
        fi
        ;;
    copy_in_sync)
        : # nichts zu tun
        ;;
esac

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
            ok "Extension-Symlink gesetzt: ${EXT_TARGET}"
            for sub in "${EXT_SUB_MODULES[@]}"; do
                sub_src="${EXT_DIR}/${sub}"
                sub_dst="${EXT_TARGET_DIR}/${sub}"
                if [ ! -f "${sub_src}" ]; then
                    warn "Sub-Modul fehlt im Repo: ${sub_src} — uebersprungen"
                    continue
                fi
                if [ -e "${sub_dst}" ] && [ ! -L "${sub_dst}" ]; then
                    warn "${sub_dst} ist eine normale Datei. Backup: ${sub_dst}.bak"
                    mv "${sub_dst}" "${sub_dst}.bak"
                fi
                ln -sf "${sub_src}" "${sub_dst}"
                ok "Sub-Modul-Symlink gesetzt: ${sub_dst}"
            done
            ;;
        cfg_copy)
            mkdir -p "${PRINTER_CFG_DIR}"
            cp "${CFG_SOURCE}" "${CFG_TARGET}"
            ok "Config kopiert: ${CFG_TARGET} (editierbar via Mainsail)."
            ;;
        cfg_replace_link)
            mkdir -p "${PRINTER_CFG_DIR}"
            # Bestehender Symlink/Spezialeintrag wird gesichert (auch
            # wenn er kaputt ist — readlink/cp -a übernimmt das ohne
            # die Repo-Datei zu touchieren).
            BAK="${CFG_TARGET}.bak.$(date +%Y%m%d-%H%M%S)"
            mv "${CFG_TARGET}" "${BAK}"
            warn "Bestehende Datei/Symlink gesichert: ${BAK}"
            cp "${CFG_SOURCE}" "${CFG_TARGET}"
            ok "Config kopiert: ${CFG_TARGET} (editierbar via Mainsail)."
            ;;
        cfg_replace_with_backup)
            BAK="${CFG_TARGET}.bak.$(date +%Y%m%d-%H%M%S)"
            cp -a "${CFG_TARGET}" "${BAK}"
            warn "Lokale Version gesichert: ${BAK}"
            cp "${CFG_SOURCE}" "${CFG_TARGET}"
            ok "Repo-Version übernommen: ${CFG_TARGET}."
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
primary_branch: ${CURRENT_BRANCH}
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
        ext)                     say "  - Extension-Symlink ${EXT_TARGET} → ${EXT_SOURCE}" ;;
        cfg_copy)                say "  - Config kopiert: ${CFG_SOURCE} → ${CFG_TARGET}" ;;
        cfg_replace_link)        say "  - Config-Symlink ersetzt durch Kopie: ${CFG_TARGET} (alte Datei in ${CFG_TARGET}.bak.*)" ;;
        cfg_replace_with_backup) say "  - Config überschrieben mit Repo-Version: ${CFG_TARGET} (lokale Version in ${CFG_TARGET}.bak.*)" ;;
        inc)                     say "  - [include lll.cfg] an printer.cfg angehängt (Backup: ${PRINTER_CFG}.bak.*)" ;;
        mr)                      say "  - [update_manager buffer_feeder] in moonraker.conf ergänzt (Backup: ${MOONRAKER_CONF}.bak.*)" ;;
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
say "${C_BOLD}Config bearbeiten:${C_RESET}"
say "  ${CFG_TARGET} ist eine Kopie und kann direkt in Mainsail editiert werden."
say "  Beim nächsten ./install.sh wird ein Diff zur Repo-Version angezeigt — du"
say "  entscheidest dann, ob deine Änderungen behalten oder Repo-Updates"
say "  übernommen werden sollen."
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
