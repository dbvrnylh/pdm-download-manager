#!/usr/bin/env bash
# Installe le moteur PDM pour Chrome, Chromium, Brave, Edge et Vivaldi (Linux).
#
#   ./install.sh            installation interactive
#   ./install.sh --yes      répond oui à tout (téléchargement de yt-dlp compris)
#   ./install.sh --no-ytdlp n'installe pas yt-dlp
#
# Rien n'est installé hors de ton dossier personnel : pas besoin de sudo.
set -euo pipefail

EXT_ID="mmkfdhmkdlpmcfgbpmoidnieiphoaphd" # fixé par la clé du manifeste de l'extension
HOST_NAME="com.pdm.engine"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/pdm"
APP="$DATA/app"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
YTDLP_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"

ASSUME_YES=0
WANT_YTDLP=1
for arg in "$@"; do
  case "$arg" in
    --yes | -y) ASSUME_YES=1 ;;
    --no-ytdlp) WANT_YTDLP=0 ;;
    *) echo "Option inconnue : $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m ✗\033[0m %s\n' "$*" >&2; exit 1; }

ask() {
  [ "$ASSUME_YES" = 1 ] && return 0
  [ -t 0 ] || return 1 # pas de terminal : on ne pose pas de question
  local answer
  read -r -p "$1 [O/n] " answer
  [[ -z "$answer" || "$answer" =~ ^[OoYy] ]]
}

# ─── 1. Python ──────────────────────────────────────────────────────────────
command -v python3 >/dev/null || die "python3 est introuvable : sudo apt install python3"
# 3.10 : la version minimale exigée par yt-dlp.
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
  die "Python 3.10 ou plus récent est nécessaire ($(python3 --version))"
ok "$(python3 --version)"

# ─── 2. Moteur ──────────────────────────────────────────────────────────────
say "Installation du moteur dans $APP"
if [ -x "$APP/pdm-host" ]; then
  "$APP/pdm-host" stop >/dev/null 2>&1 || true # l'ancienne version laisse la place
fi
mkdir -p "$APP"
rm -rf "$APP/pdm_engine"
cp -r "$SRC/engine/pdm_engine" "$APP/"
cp "$SRC/engine/pdm-host" "$APP/pdm-host"
chmod 755 "$APP/pdm-host"
find "$APP" -name '__pycache__' -prune -exec rm -rf {} +
ok "moteur $("$APP/pdm-host" --version) copié"

# ─── 3. Déclaration auprès des navigateurs ──────────────────────────────────
# Chrome ne lance qu'un programme natif déclaré dans un fichier JSON, et
# seulement pour les extensions listées dans allowed_origins.
manifest() {
  cat <<EOF
{
  "name": "$HOST_NAME",
  "description": "Moteur de téléchargement PDM",
  "path": "$APP/pdm-host",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
EOF
}

say "Déclaration du moteur auprès des navigateurs"
BROWSERS=(
  "google-chrome:Google Chrome"
  "google-chrome-beta:Google Chrome Beta"
  "google-chrome-unstable:Google Chrome Dev"
  "chromium:Chromium"
  "BraveSoftware/Brave-Browser:Brave"
  "microsoft-edge:Microsoft Edge"
  "vivaldi:Vivaldi"
)
installed=0
for entry in "${BROWSERS[@]}"; do
  dir="${entry%%:*}"
  name="${entry#*:}"
  if [ -d "$CONFIG/$dir" ]; then
    mkdir -p "$CONFIG/$dir/NativeMessagingHosts"
    manifest >"$CONFIG/$dir/NativeMessagingHosts/$HOST_NAME.json"
    ok "$name"
    installed=$((installed + 1))
  fi
done
# Chromium en snap (Ubuntu) range sa configuration ailleurs.
if [ -d "$HOME/snap/chromium/common/chromium" ]; then
  mkdir -p "$HOME/snap/chromium/common/chromium/NativeMessagingHosts"
  manifest >"$HOME/snap/chromium/common/chromium/NativeMessagingHosts/$HOST_NAME.json"
  ok "Chromium (snap) — si le moteur reste introuvable, préfère Chrome ou Chromium en .deb"
  installed=$((installed + 1))
fi
if [ "$installed" = 0 ]; then
  # Aucun navigateur encore lancé : on prépare Chrome et Chromium.
  for dir in google-chrome chromium; do
    mkdir -p "$CONFIG/$dir/NativeMessagingHosts"
    manifest >"$CONFIG/$dir/NativeMessagingHosts/$HOST_NAME.json"
  done
  warn "aucun navigateur détecté : Chrome et Chromium préparés quand même"
fi

# ─── 4. yt-dlp (vidéos en streaming, YouTube…) ──────────────────────────────
# PDM utilise sa propre copie de l'exécutable officiel, même si le système en
# a déjà un : les versions des distributions sont souvent trop anciennes pour
# YouTube (erreurs 403), et l'exécutable officiel embarque le solveur
# JavaScript que YouTube exige. Le moteur la met à jour chaque jour.
say "Outils vidéo"
mkdir -p "$DATA/bin"
if [ -f "$DATA/bin/yt-dlp" ]; then
  python3 "$DATA/bin/yt-dlp" -U >/dev/null 2>&1 || warn "mise à jour de yt-dlp impossible (hors ligne ?)"
  ok "yt-dlp $(python3 "$DATA/bin/yt-dlp" --version) (copie PDM)"
elif [ "$WANT_YTDLP" = 1 ] && ask "Installer la copie de yt-dlp de PDM (recommandé : toujours à jour) ?"; then
  tmp="$(mktemp)"
  if command -v curl >/dev/null; then
    curl -fsSL "$YTDLP_URL" -o "$tmp"
  else
    wget -q "$YTDLP_URL" -O "$tmp"
  fi
  install -m 755 "$tmp" "$DATA/bin/yt-dlp"
  rm -f "$tmp"
  ok "yt-dlp $(python3 "$DATA/bin/yt-dlp" --version) (copie PDM)"
elif command -v yt-dlp >/dev/null; then
  warn "yt-dlp du système utilisé ($(yt-dlp --version)) : garde-le à jour, sinon YouTube échouera"
else
  warn "yt-dlp absent : les vidéos en streaming et YouTube ne seront pas disponibles"
fi

if command -v ffmpeg >/dev/null; then
  ok "ffmpeg"
else
  warn "ffmpeg absent : pas de fusion audio + vidéo ni de MP3 → sudo apt install ffmpeg"
fi
if command -v deno >/dev/null || command -v node >/dev/null; then
  ok "moteur JavaScript ($(command -v deno >/dev/null && echo deno || echo node))"
else
  warn "ni deno ni node : YouTube pourra proposer moins de formats → https://deno.com"
fi
command -v notify-send >/dev/null || warn "notify-send absent : pas de notification de fin (sudo apt install libnotify-bin)"

# ─── 5. Fin ─────────────────────────────────────────────────────────────────
cat <<EOF

$(printf '\033[1;32m')PDM est installé.$(printf '\033[0m')

Si ce n'est pas déjà fait, charge l'extension :
  1. Ouvre chrome://extensions (ou brave://extensions, edge://extensions)
  2. Active le « Mode développeur »
  3. « Charger l'extension non empaquetée » → $SRC/extension

Puis ouvre les Réglages de PDM : le moteur doit apparaître comme actif.
Dépannage : $APP/pdm-host status   (journal : $DATA/engine.log)
EOF
