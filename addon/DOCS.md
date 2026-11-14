# fetchit — Home Assistant add-on

Downloads the feeds in `fetchit.json` into `/media/fetchit` on a schedule, laid
out for Jellyfin, and for the other two add-ons that read it:

| Add-on | Reads `/media/fetchit` to… |
|---|---|
| [rssignal](https://github.com/bertcoerver/rssignal) | send what is new to Signal groups |
| [db-podcast](https://github.com/bertcoerver/db-podcast) | publish chosen feeds as podcasts through Dropbox |
| Jellyfin (e.g. from `alexbelgium/hassio-addons`) | show the videos as series and the audio as albums |

fetchit is the only one that downloads anything. The others never touch the
network for content, and never write to the library, so each runs on its own
schedule without coordinating.

## What you need

- Home Assistant OS on aarch64 (Raspberry Pi 4 or 5) or amd64.
- **A USB SSD rather than an SD card.** Everything is written to disk in full,
  and kept for `keep_days`.
- The **File Editor** or **Samba share** add-on, to put `fetchit.json` on the host.

## Installing

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, and add
   `https://github.com/bertcoerver/fetchit`.
2. Install **fetchit**. Nothing is built on the host: GitHub Actions publishes the
   image for each architecture, and installing is a download.

## Setting up

1. Write your `fetchit.json` in the panel's **Configuration** editor (see below),
   or put it in the add-on's config folder, which the host shows as
   `/addon_configs/<hash>_fetchit/fetchit.json`. Start from `fetchit.example.json`
   in the repository; the README describes every key.
2. Set `on_start` to `dry-run` and start the add-on. The log shows what each feed
   would download, at what quality, and to where. Set it back to `wait`.
3. Start it. The next scheduled run downloads for real.

You can edit `fetchit.json` at any time, in the panel or elsewhere; the next run reads it.

If YouTube starts refusing with "confirm you're not a bot", put a `cookies.txt`
export from a (throwaway) logged-in account next to `fetchit.json` as
`youtube-cookies.txt`. The add-on uses it when it is there.

## The panel

The add-on is **fetchit** in Home Assistant's sidebar — switch on **Show in
sidebar** on its Info page — or **Open Web UI**. It shows the library and the
free disk, when the next run is, and every feed with what the library holds of
it (items, size, newest), when it last downloaded, and its settings. Buttons run
everything, or one feed, for real or as a dry run, and prune now; what a run
prints is shown as it goes. Below that: recent downloads and the log.

**Add Bayam magazines** takes the link printed in a new issue (or several, one
per line), adds it to the feed you name, and with **Add and download** fetches
it straight away. A feed name that does not exist yet makes a new feed, kept for
good. It edits `fetchit.json` for you, with the same check and `.bak` as below.

At the bottom is `fetchit.json` itself, to edit. **Save** (or ⌘S / Ctrl+S)
checks it the way a run reads it and refuses one that a run would refuse, saying
what is wrong; a saved file keeps the previous one beside it as
`fetchit.json.bak`. If the file was changed elsewhere (the File Editor, Samba)
after the panel loaded it, the save is refused rather than overwrite that edit:
**Revert** loads the current file. The panel is behind your Home Assistant login.

## Configuration

| Option | Default | |
|---|---|---|
| `on_start` | `wait` | `wait`, `dry-run` or `run` — what to do at startup, before the schedule. The panel's buttons do the same any time. |
| `schedule` | `01:00`, `06:00`, `14:30` | Local times to run at. |
| `keep_days` | `30` | How long downloads stay, unless a feed says otherwise. `0` keeps everything. The config file's own `keep_days` wins over this. |
| `update_yt_dlp` | `true` | Upgrade yt-dlp once a day, before a run. |
| `log_level` | `info` | |

### Scheduling the three together

fetchit's runs should come a little before the others', so they find what it
downloaded — for example fetchit at 01:00, 06:00 and 14:30, rssignal at 01:30,
06:30 and 15:00, and db-podcast at 02:00, 07:00 and 15:30. Nothing breaks if they
overlap: fetchit moves each item into place with its record last, so a reader sees
either all of an item or none of it, and it picks up the rest next time.

## Jellyfin

Give Jellyfin's add-on access to `/media`, then add two libraries: **Shows** at
`/media/fetchit/shows` (keep **Nfo** enabled as a metadata reader) and **Music** at
`/media/fetchit/podcasts`. Infuse can then connect to Jellyfin and shows the same
series, seasons and synopses.

## What lives where

| Path (in the container) | On the host | |
|---|---|---|
| `/config/fetchit.json` | `/addon_configs/<hash>_fetchit/` | the feeds — yours |
| `/media/fetchit/` | `/media/fetchit/` | the library |
| `/media/fetchit/.fetchit/state.json` | | what each feed is done with |
| `/data/cache.json`, `/data/fetchit.log` | add-on volume | speed cache; the log of every run |

`state.json` is what stops a feed starting over: keep it with the library. The cache
can be deleted at the cost of one slow run.
