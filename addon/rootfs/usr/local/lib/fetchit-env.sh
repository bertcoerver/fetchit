# shellcheck shell=bash
#
# The environment every fetchit command in this add-on runs with.
#
# Sourced rather than duplicated, so that `fetchit doctor` on start reports
# exactly the paths a run uses. Expects bashio to be loaded — every caller is a
# `with-contenv bashio` script.

# /data is the add-on's persistent volume: it survives restarts and updates.
# The cache and the log go there. What fetchit has downloaded — its state —
# lives in the library itself, under /media/fetchit/.fetchit, so it travels
# with the files it describes.
export HOME="/data/home"
mkdir -p "${HOME}"
export FETCHIT_CACHE="/data/cache.json"
export FETCHIT_LOG="/data/fetchit.log"

# The config lives on the host at /addon_configs/<slug>/, so it can be edited
# with the File Editor or over Samba.
export FETCHIT_CONFIG="/config/fetchit.json"

# /media is the folder Home Assistant's Media browser shows. Jellyfin,
# rssignal and db-podcast are all pointed at this.
export FETCHIT_LIBRARY="/media/fetchit"

# `fetchit serve`: when to run, the default keep_days (the config file's own
# wins), the daily yt-dlp upgrade, and who may open the GUI — only Home
# Assistant's ingress proxy, which has signed you in.
FETCHIT_SCHEDULE="$(jq -r '(.schedule // []) | join(",")' /data/options.json)"
FETCHIT_KEEP_DAYS="$(bashio::config 'keep_days')"
FETCHIT_UPDATE_YT_DLP="$(bashio::config 'update_yt_dlp')"
export FETCHIT_SCHEDULE FETCHIT_KEEP_DAYS FETCHIT_UPDATE_YT_DLP
export FETCHIT_GUI_FROM="172.30.32.2"

# A cookies.txt beside fetchit.json, for when YouTube asks to "confirm you're
# not a bot". Off unless the file is there.
if [ -f /config/youtube-cookies.txt ]; then
    export FETCHIT_YOUTUBE_COOKIES="/config/youtube-cookies.txt"
fi
