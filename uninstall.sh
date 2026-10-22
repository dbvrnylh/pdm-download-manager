#!/usr/bin/env bash
# Désinstalle le moteur PDM. Les fichiers téléchargés ne sont jamais touchés.
#
#   ./uninstall.sh          garde l'historique et les réglages
#   ./uninstall.sh --purge  supprime aussi l'historique, les réglages et yt-dlp
set -euo pipefail

HOST_NAME="com.pdm.engine"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/pdm"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"

if [ -x "$DATA/app/pdm-host" ]; then
  "$DATA/app/pdm-host" stop || true
fi

for manifest in "$CONFIG"/*/NativeMessagingHosts/"$HOST_NAME".json \
  "$CONFIG"/BraveSoftware/*/NativeMessagingHosts/"$HOST_NAME".json \
  "$HOME"/snap/chromium/common/chromium/NativeMessagingHosts/"$HOST_NAME".json; do
  if [ -f "$manifest" ]; then
    rm -f "$manifest"
    echo "supprimé : $manifest"
  fi
done

rm -rf "$DATA/app"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$DATA"
  echo "Historique, réglages et yt-dlp supprimés."
fi
echo "Moteur PDM désinstallé. Retire l'extension depuis chrome://extensions."
