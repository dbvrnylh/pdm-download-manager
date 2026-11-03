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

# Les .torrent et liens magnet : rendus à ce qui les ouvrait avant PDM (noté par install.sh).
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
if [ -f "$APPS/pdm.desktop" ]; then
  if command -v xdg-mime >/dev/null; then
    for type in application/x-bittorrent x-scheme-handler/magnet; do
      [ "$(xdg-mime query default "$type" 2>/dev/null || true)" = pdm.desktop ] || continue
      before="$(sed -n "s|^$type=||p" "$DATA/previous-handlers" 2>/dev/null | head -n 1)"
      if [ -n "$before" ]; then
        xdg-mime default "$before" "$type"
        echo "$type : rendu à ${before%.desktop}"
      elif [ -f "$CONFIG/mimeapps.list" ]; then
        sed -i "\|^$type=pdm\.desktop;\{0,1\}\$|d" "$CONFIG/mimeapps.list"
      fi
    done
  fi
  rm -f "$APPS/pdm.desktop" "$DATA/previous-handlers"
  if command -v update-desktop-database >/dev/null; then
    update-desktop-database -q "$APPS" 2>/dev/null || true
  fi
  echo "supprimé : $APPS/pdm.desktop"
fi

rm -rf "$DATA/app"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$DATA"
  echo "Historique, réglages et yt-dlp supprimés."
fi
echo "Moteur PDM désinstallé. Retire l'extension depuis chrome://extensions."
