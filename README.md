# fetchit

Download feeds into a media library that other tools can read.

fetchit reads the feeds named in a config file (RSS and podcast feeds, ARTE
collections, NPO Start series, YouTube channels, Bayam magazine links) and
downloads what is new into one folder, laid out the way a media server expects:

```
/media/fetchit/
    shows/Le Dessous des Images/        a Jellyfin "Shows" library
        tvshow.nfo  poster.jpg  fanart.jpg  feed.json
        Season 2026/
            Le Dessous des Images - S2026E101901 - La bataille du drapeau.mp4
            Le Dessous des Images - S2026E101901 - La bataille du drapeau.nfo
            Le Dessous des Images - S2026E101901 - La bataille du drapeau-thumb.jpg
            Le Dessous des Images - S2026E101901 - La bataille du drapeau.json
    podcasts/Bureau Buitenland/         a Jellyfin "Music" library
        cover.jpg  feed.json
        2026-10-19 - Europa protesteert.mp3
        2026-10-19 - Europa protesteert.json
    articles/Wait But Why/              items with nothing to download
        feed.json
        2025-11-25 - The sights and sounds of Bhutan.json
```

Two other tools read the same folder and download nothing themselves:

- [rssignal](https://github.com/bertcoerver/rssignal) sends what is new to Signal.
- [db-podcast](https://github.com/bertcoerver/db-podcast) publishes chosen feeds
  as podcasts through Dropbox.

They share nothing but [the folder's layout](#the-library), so each runs on its own
schedule. All three run as Home Assistant add-ons on a Raspberry Pi — see
[`addon/DOCS.md`](addon/DOCS.md).

## Installation

```bash
pip install "fetchit[video]"   # yt-dlp, for YouTube feeds
brew install ffmpeg            # for ARTE, and for joining YouTube's video and audio
```

Python 3.13+. Without `[video]` and ffmpeg, RSS and podcast feeds still work;
`fetchit doctor` says what is missing.

## Usage

```bash
cp fetchit.example.json fetchit.json   # and edit it
fetchit run --dry-run                  # what would be downloaded, at what quality, to where
fetchit run                            # download it
fetchit run --feed "The Daily Show"    # one feed only
fetchit fields --feed "The Daily Show" # the fields an item has, for writing filters
fetchit prune                          # delete what has outlived its keep_days (runs do this too)
fetchit doctor                         # check ffmpeg, yt-dlp, the config and the library
```

A run reads every feed at once, then downloads each feed's new items one at a
time, oldest first. A feed that fails is reported on stderr and in `fetchit.log`,
and the others carry on. Only one run happens at a time per library.

### As a service, with a GUI

```bash
FETCHIT_SCHEDULE=01:00,06:00,14:30 fetchit serve     # http://127.0.0.1:8099/
```

`fetchit serve` stays running, runs at those times, and serves a page with every
feed and what the library holds of it, run / dry run / prune buttons with their
output, recent downloads and the log. Only this machine may open it, unless
`FETCHIT_GUI_FROM` lists other addresses. It is what the Home Assistant add-on
runs, as its sidebar panel. `FETCHIT_UPDATE_YT_DLP=1` upgrades yt-dlp before a
scheduled run, once a day; `FETCHIT_KEEP_DAYS` is the default `keep_days` for a
config that doesn't say.

## Configuration

```json
{
  "library": "~/fetchit",
  "keep_days": 30,
  "quality": {"max_height": 1080},
  "feeds": [
    {"name": "Le Dessous des Images",
     "url": "https://www.arte.tv/fr/videos/RC-023176/le-dessous-des-images/"}
  ]
}
```

| Key | | |
|---|---|---|
| `library` | optional | Where the library is. `$FETCHIT_LIBRARY` and `--library` override it; the default is `~/fetchit`. |
| `keep_days` | optional | How long a download stays, counted from when it was downloaded. `0`, the default, keeps everything. |
| `quality` | optional | The default for every feed; see [quality](#quality). |
| `feeds` | required | The feeds. |

Each feed:

| Key | | |
|---|---|---|
| `name` | required | The feed's folder in the library, and what rssignal and db-podcast call it. Unique. |
| `url` / `urls` | required | Where it is read from. `urls` is a list, for a feed made of several links (Bayam). |
| `since` | optional | Backfill: everything published since this date, oldest first. See [what is new](#what-is-new). |
| `max_per_run` | optional | At most this many downloads per run (default 10); the rest wait for the next run. |
| `max_per_day` | optional | At most this many in any 24 hours — to keep a backfill in step with something sending it on at a pace. |
| `quality` | optional | Overrides the library's, key by key. |
| `keep_days` | optional | Overrides the library's. |
| `refresh_image` | optional | `false` keeps the feed's poster as first downloaded, for a source whose artwork changes every episode. |
| `language` | optional | Written into `feed.json` where the source does not say (a YouTube channel). |
| `extract` | optional | New fields cut out of existing ones by regex: `{"episode_id": {"from": "link", "pattern": "/(\\d+)/"}}`. |
| `<field>_contains` / `_excludes` / `_matches` | optional | Filters: keep items whose field contains / doesn't contain any of these terms, or matches any of these regexes. Case-insensitive. |
| `<field>_min` / `_max` | optional | Filters on a number or a duration: `"duration_seconds_min": "7:00"`. |

Filters decide what is *downloaded*. They are all ANDed, read the fields
`fetchit fields` lists, and are applied before anything is recorded — so changing
one later applies to new items. Keys that are about sending (`message_template`,
`episodic`, `link_preview`, …) belong in rssignal's `feeds.json`, and fetchit says
so if it finds one.

## What is new

fetchit keeps, per feed, the items it is done with (downloaded, or stepped over for
good) and the moment the feed starts from. Both live in the library, in
`.fetchit/state.json`, so nothing is downloaded twice — not even after it has been
pruned.

- **A feed seen for the first time takes only its newest item**, and everything
  published after it from then on. Following a channel starts with its latest
  video, not its last fifteen.
- **`since` makes a feed a backfill**: everything published since that date,
  oldest first. For a YouTube channel that includes the whole back catalogue,
  read from its uploads playlist (no API key), dated 25 videos a run. Pair it with
  `max_per_day` when something downstream walks the series at a pace — an
  `episodic` feed in rssignal — so the library stays a step ahead of it rather than
  racing ahead and having episodes pruned before they are sent.
- **Items arrive in publication order.** An item that can't be downloaded right
  now stops its feed for this run, and the next run starts with it. A newer item
  is never filed while an older one is owed — whatever reads the library by date
  would otherwise move past it.
- **Three answers are permanent**, and those items are stepped over: a YouTube
  upload under 90 seconds (a Short), a video too big for the feed's size budget,
  and a video its source has taken down (an expired ARTE rights window, or one
  listed for a week without a video).
- **Undated items are never downloaded.** There is no telling whether they are new.

## Kinds of item

Decided per item, with nothing to configure:

- **audio** — an item with an audio enclosure (a podcast episode), or a Bayam track.
  Filed under `podcasts/`, tagged for a music library.
- **video** — an item that links to an ARTE programme, an NPO Start episode or a
  YouTube video, or carries a video enclosure. Filed under `shows/`.
- **text** — anything else. Filed under `articles/` as a record only, so a blog can
  still be sent on by rssignal.

A feed that mixes them gets a folder of each kind.

## Sources

### RSS and podcast feeds

Any RSS or Atom feed. The validators a feed hands out (ETag, Last-Modified) go back
with the next request, so a feed with nothing new costs one tiny answer — and
they are only remembered once the feed's new items have been dealt with, so an
episode whose download failed is asked about again in full.

### ARTE

ARTE publishes no RSS: point `url` at a show's collection page. Each episode
arrives with its own title, synopsis, artwork, length and the date it became
available. The newest dozen are dated individually; the rest of the back catalogue
stays undated, and therefore is not downloaded.

### NPO Start (via Downloadgemist)

Point `url` at a series, `https://npo.nl/start/serie/<series>/afleveringen` — it
follows the newest season — or at one season, `…/afleveringen/seizoen-2`. Episodes
are numbered by season, and filed as such. The video comes through
[downloadgemist.nl](https://downloadgemist.nl), a free, hobby-run site, so fetchit
asks it for as little as it can: only for a new episode, with the site's own pause
between downloads respected, and an episode that fails backed off for an hour, then
two, up to a day, and given up on after three days.

### YouTube

Point `url` at a channel, in any form (`/@handle`, `/channel/UC…`, `/c/…`, `/user/…`).
No API key: the channel's Atom feed carries its last 15 uploads, and yt-dlp fills in
how long each is, so `duration_seconds_min` is an ordinary filter. When the feed
endpoint has an outage, the same uploads are read from the channel's playlist.

Before a YouTube feed is read, `https://www.youtube.com/` is probed with one HEAD
request. If a DNS filter or firewall has it closed, the feed is skipped with one
line — not a failure — and picks up everything it missed on the first run inside
the open window.

If YouTube asks you to "confirm you're not a bot", hand yt-dlp a signed-in session:

```bash
FETCHIT_YOUTUBE_COOKIES_FROM_BROWSER=firefox
# or
FETCHIT_YOUTUBE_COOKIES=~/.config/fetchit/youtube-cookies.txt
```

Use a throwaway Google account: the cookies are a live session.

### Bayam

The audio stories behind the links printed in Bayard Jeunesse magazines. A feed of
magazines is a list of those links; add the new one each month:

```json
{"name": "Mes premières Belles Histoires",
 "urls": ["https://flyer.bayam.tv/4sYB/dj8", "https://flyer.bayam.tv/4sYB/pmo"]}
```

Each track becomes an episode, dated with the issue's release, numbered with the
issue number, and described with the magazine it came with.

## Quality

Every video source offers several renditions. Which one is right depends on where
it is going, so it is a setting, for the library and per feed:

| Key | Default | |
|---|---|---|
| `max_height` | `1080` | The tallest picture worth having. `null` for no limit. |
| `part_size_mb` | off | Switches to rssignal's rule: **the fewest parts of this size possible, then the best quality that fits in that many.** `95` for a feed that is also sent to Signal, which refuses attachments over 100 MB. |
| `max_parts` | `4` | With `part_size_mb`: a video needing more parts than this isn't downloaded at all. |

The choice is made before downloading, from each source's own size estimates, and
`fetchit run --dry-run` shows it. The picture is never re-encoded: streams are
copied (H.264 and AAC, which every player handles).

A feed that goes to both Jellyfin and Signal has to pick one: the sharpest picture,
which rssignal then sends in more parts, or a picture that fits Signal, which
Jellyfin shows as it is.

## The library

The layout is the contract between fetchit and whatever reads it.

- **An item exists when its record does.** Everything an item consists of is put
  together in `.fetchit/staging` (on the same disk, so moving it is a rename) and
  moved into place with its `.json` record last. A reader never sees half an item.
  Pruning removes the record first.
- **The record** (`<name>.json`, beside the media) holds `format` (the layout
  version, now 1), `id`, `feed`, `kind`, `title`, `description`, `published` (ISO
  8601), `link`, `media` and `image` (file names beside the record), `season` and
  `episode` for a video, `number` where the source numbers its episodes, `duration`
  in seconds, `author`, `categories`, `fetched`, and `fields` — every field the
  source gave, as the strings a filter sees.
- **`feed.json`** in each feed folder: `format`, `name`, `title`, `description`,
  `url`, `link`, `author`, `language`, `explicit`, and `image`, the file name of its
  artwork.
- **Videos** are episodes of a series. A source that numbers its episodes (NPO) gets
  `Season 02/… - S02E28 - …`; everything else is numbered by date, the season being
  the year and the episode the month, day and a counter: `S2026E101901` is the first
  of 19 October. Beside each: a Kodi `.nfo` and a `-thumb.jpg`; above them
  `tvshow.nfo`, `poster.jpg` and `fanart.jpg`. The `.nfo` files are locked, so a
  media server takes them as they are rather than looking the series up online and
  finding somebody else's programme.
- **Audio** is an album: tagged with the feed as album and album artist, the episode
  as title, and its date. `cover.jpg` is the album's artwork.
- **Artwork** is checked against the source about once a week. A poster or cover
  you replaced by hand is left alone; one fetchit put there is brought up to date
  unless the feed says `"refresh_image": false`.
- **Names** keep accents, spaces and capitals, and lose only what a filesystem or a
  Samba share refuses.

### In Jellyfin

Add two libraries: one of type **Shows** pointing at `<library>/shows`, and one of
type **Music** pointing at `<library>/podcasts`. Keep **Nfo** enabled as a metadata
reader for the first. Folders starting with a dot are ignored, so `.fetchit` never
shows up.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The suite fakes every network request and every yt-dlp and ffmpeg call. One test
makes a real m4a with ffmpeg, and is skipped without it.

## Release

```bash
./tag_release.sh patch        # or minor / major: the Python package
make addon-release VERSION=x.y.z   # the Home Assistant add-on
```
