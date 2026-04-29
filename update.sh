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
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

EXT_SOURCE="${REPO_DIR}/klipper_extras/buffer_feeder.py"
EXT_TARGET="${KLIPPER_DIR}/klippy/extras/buffer_feeder.py"
CFG_SOURCE="${REPO_DIR}/lll.cfg"
CFG_TARGET="${PRINTER_CFG_DIR}/lll.cfg"

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

# ---------- 0) Drucker-Modus: tests/ aus Working-Tree ausblenden ----------
# Sparse-checkout sorgt dafuer, dass git pull tests/ gar nicht erst auf
# die Drucker-SD legt. Idempotent: beim ersten Aufruf aktivieren, sonst
# no-op. Zum Re-Aktivieren von Tests (Dev-Maschine):
#   git config --unset core.sparseCheckout
#   git read-tree -m -u HEAD
SPARSE_FILE=".git/info/sparse-checkout"
if [ "$(git config --get core.sparseCheckout 2>/dev/null || true)" != "true" ]; then
    echo "[update] Aktiviere sparse-checkout (Drucker-Modus, ohne tests/)"
    git config core.sparseCheckout true
    mkdir -p .git/info
    cat > "${SPARSE_FILE}" <<'EOF'
/*
!/tests/
EOF
    git read-tree -m -u HEAD
fi

# ---------- 1) git pull ----------
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "[update] git fetch + pull --ff-only auf ${BRANCH}"
git fetch --quiet origin
git pull --ff-only origin "${BRANCH}"

# ---------- 2) Extension-Symlink ----------
echo "[update] Extension-Symlink: ${EXT_TARGET} -> ${EXT_SOURCE}"
ln -sfn "${EXT_SOURCE}" "${EXT_TARGET}"

# ---------- 3) lll.cfg kopieren (mit Rolling-Backup) ----------
mkdir -p "${PRINTER_CFG_DIR}"
if [ -e "${CFG_TARGET}" ] && ! [ -L "${CFG_TARGET}" ]; then
    cp "${CFG_TARGET}" "${CFG_TARGET}.dev.bak"
fi
# falls noch alter Symlink: erst entfernen, sonst schreibt cp die Repo-Datei
[ -L "${CFG_TARGET}" ] && rm "${CFG_TARGET}"
cp "${CFG_SOURCE}" "${CFG_TARGET}"
echo "[update] lll.cfg überschrieben: ${CFG_TARGET} (Backup: ${CFG_TARGET}.dev.bak falls vorhanden)"

# ---------- 4) Klipper-Restart ----------
echo "[update] Klipper-Service neustarten (${KLIPPER_SERVICE})"
${SUDO} systemctl restart "${KLIPPER_SERVICE}"

LAST="$(git log -1 --oneline)"
echo "[update] fertig — HEAD: ${LAST}"
