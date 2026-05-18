#!/bin/bash
# update.sh — non-interactive Dev-Update.
#
# Macht stumpf:
#   1. git pull --ff-only auf dem aktuellen Branch
#   2. Extension-Symlink setzen (idempotent)
#   3. lll.cfg aus dem Repo überkopieren (mit kurzem Rolling-Backup)
#   4. Klipper neu starten
#
# Pfade per Umgebungsvariable überschreibbar (Defaults wie install.sh):
#   KLIPPER_DIR       (default: ~/klipper)
#   PRINTER_CFG_DIR   (default: ~/printer_data/config)
#   KLIPPER_SERVICE   (default: klipper)
#   MOONRAKER_URL     (default: http://127.0.0.1:7125)
#   ROLLOVER_KLIPPY_LOG (default: 1)
#
# Verwendung:
#   ./update.sh
#
# Hinweis: lll.cfg-Edits gehen verloren — vorher in den Repo committen,
# wenn sie behalten werden sollen. Für interaktive Diff-Behandlung gibt's
# install.sh.

set -eu

KLIPPER_DIR="${KLIPPER_DIR:-${HOME}/klipper}"
PRINTER_CFG_DIR="${PRINTER_CFG_DIR:-${HOME}/printer_data/config}"
KLIPPER_SERVICE="${KLIPPER_SERVICE:-klipper}"
MOONRAKER_URL="${MOONRAKER_URL:-http://127.0.0.1:7125}"
ROLLOVER_KLIPPY_LOG="${ROLLOVER_KLIPPY_LOG:-1}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

EXT_SOURCE="${REPO_DIR}/klipper_extras/buffer_feeder.py"
EXT_TARGET="${KLIPPER_DIR}/klippy/extras/buffer_feeder.py"
EXT_DIR="${REPO_DIR}/klipper_extras"
EXT_TARGET_DIR="${KLIPPER_DIR}/klippy/extras"
ENABLE_SPARSE_CHECKOUT="${ENABLE_SPARSE_CHECKOUT:-0}"
CFG_SOURCE="${REPO_DIR}/lll.cfg"
CFG_TARGET="${PRINTER_CFG_DIR}/lll.cfg"

collect_ext_sub_modules() {
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

moonraker_rollover_klippy_log() {
    local response
    if [ "${ROLLOVER_KLIPPY_LOG}" != "1" ]; then
        echo "[update] Klippy-Log-Rollover deaktiviert (ROLLOVER_KLIPPY_LOG=${ROLLOVER_KLIPPY_LOG})"
        return 0
    fi
    if ! command -v curl >/dev/null 2>&1; then
        echo "[update] curl nicht gefunden — ueberspringe Moonraker-Log-Rollover"
        return 0
    fi
    response="$(
        curl -fsS \
            -X POST \
            -H 'Content-Type: application/json' \
            --data '{"application":"klipper"}' \
            "${MOONRAKER_URL%/}/server/logs/rollover" \
            2>/dev/null || true
    )"
    case "${response}" in
        *'"rolled_over"'*'"klipper"'*)
            echo "[update] Klippy-Log via Moonraker gerollt"
            ;;
        "")
            echo "[update] Moonraker-Log-Rollover nicht verfuegbar oder nicht erreichbar — ueberspringe"
            ;;
        *)
            echo "[update] Moonraker-Log-Rollover lieferte keine Klipper-Bestaetigung — ueberspringe"
            echo "[update] Moonraker-Antwort: ${response}"
            ;;
    esac
}

# ---------- Sanity ----------
[ -d "${KLIPPER_DIR}/klippy/extras" ] || {
    echo "[update] FEHLER: ${KLIPPER_DIR}/klippy/extras nicht gefunden — KLIPPER_DIR setzen" >&2
    exit 1
}
[ -f "${EXT_SOURCE}" ] || {
    echo "[update] FEHLER: ${EXT_SOURCE} nicht im Repo gefunden" >&2
    exit 1
}
[ -f "${CFG_SOURCE}" ] || {
    echo "[update] FEHLER: ${CFG_SOURCE} nicht im Repo gefunden" >&2
    exit 1
}

# sudo nur ausserhalb von root
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

cd "${REPO_DIR}"

# ---------- 0) Optional: Drucker-Modus ohne tests/ ----------
# Repo-Mutationen wie sparse-checkout sind opt-in. Default: update.sh
# veraendert den Git-Checkout des Users NICHT. Aktivierung explizit via:
#   ENABLE_SPARSE_CHECKOUT=1 ./update.sh
if [ "${ENABLE_SPARSE_CHECKOUT}" = "1" ]; then
    SPARSE_FILE=".git/info/sparse-checkout"
    if [ "$(git config --get core.sparseCheckout 2>/dev/null || true)" != "true" ]; then
        echo "[update] Aktiviere sparse-checkout (opt-in, ohne tests/)"
        git config core.sparseCheckout true
        mkdir -p .git/info
        cat > "${SPARSE_FILE}" <<'EOF'
/*
!/tests/
EOF
        git read-tree -m -u HEAD
    fi
else
    echo "[update] Sparse-checkout bleibt unveraendert (opt-in via ENABLE_SPARSE_CHECKOUT=1)"
fi

# ---------- 1) git pull ----------
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "[update] git fetch + pull --ff-only auf ${BRANCH}"
git fetch --quiet origin
git pull --ff-only origin "${BRANCH}"

# ---------- 2) Extension-Symlinks ----------
echo "[update] Extension-Symlink: ${EXT_TARGET} -> ${EXT_SOURCE}"
ln -sfn "${EXT_SOURCE}" "${EXT_TARGET}"
for sub in "${EXT_SUB_MODULES[@]}"; do
    sub_src="${EXT_DIR}/${sub}"
    sub_dst="${EXT_TARGET_DIR}/${sub}"
    if [ -f "${sub_src}" ]; then
        ln -sfn "${sub_src}" "${sub_dst}"
        echo "[update] Sub-Modul-Symlink: ${sub_dst} -> ${sub_src}"
    fi
done

# ---------- 3) lll.cfg kopieren (mit Rolling-Backup) ----------
mkdir -p "${PRINTER_CFG_DIR}"
if [ -e "${CFG_TARGET}" ] || [ -L "${CFG_TARGET}" ]; then
    CFG_BACKUP="${CFG_TARGET}.dev.bak.$(date +%Y%m%d-%H%M%S)"
    cp -a "${CFG_TARGET}" "${CFG_BACKUP}"
fi
# falls noch alter Symlink: erst entfernen, sonst schreibt cp die Repo-Datei
[ -L "${CFG_TARGET}" ] && rm "${CFG_TARGET}"
cp "${CFG_SOURCE}" "${CFG_TARGET}"
if [ -n "${CFG_BACKUP:-}" ]; then
    echo "[update] lll.cfg überschrieben: ${CFG_TARGET} (Backup: ${CFG_BACKUP})"
else
    echo "[update] lll.cfg überschrieben: ${CFG_TARGET}"
fi

# ---------- 4) Klipper-Restart ----------
moonraker_rollover_klippy_log

# ---------- 5) Klipper-Restart ----------
echo "[update] Klipper-Service neustarten (${KLIPPER_SERVICE})"
${SUDO} systemctl restart "${KLIPPER_SERVICE}"

LAST="$(git log -1 --oneline)"
echo "[update] fertig — HEAD: ${LAST}"
