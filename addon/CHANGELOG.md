# Changelog

## 0.2.1

- Give yt-dlp a JavaScript runtime for signed-in YouTube sessions

## 0.2.0

- Read a school blog on e-Primo, signed in through ÉduConnect

## 0.1.16

- Say where the add-on wants YouTube cookies, and when they have expired

## 0.1.15

- Point episodic and start at since and max_per_day

## 0.1.14

- Tell other programs when a run downloaded something

## 0.1.13

- Forget an episode so the next run fetches it again

## 0.1.12

- Show a Les Mini Mondes feed by the magazine's logo
- List workflow changes in the add-on changelog

## 0.1.11

- Move the workflows' actions onto Node 24

## 0.1.10

- Send fetchit's own User-Agent with every download

## 0.1.9

- Name artwork for what it is, and show a Bayam magazine by its logo

## 0.1.8

- Fix the panel's "+ New list" tab once a link list exists
- Take a Bayam track the magazine lists twice as one episode

## 0.1.7

- Read Les Mini Mondes magazine links, and take only readable links into a link list

## 0.1.6

- Take everything behind a link list's links, however old

## 0.1.5

- Keep a feed's links in a file of their own, one tab per list in the panel

## 0.1.4

- Retry feeds that time out while being read instead of failing the run with a traceback

## 0.1.3

- Add functionality to manage Bayam magazine links in the UI and backend

## 0.1.2

- Edit fetchit.json from the panel, checked the way a run reads it

## 0.1.1

- First version: the downloading that rssignal and db-podcast used to do
  themselves (RSS and podcast feeds, ARTE, NPO Start, YouTube, Bayam), filed
  in /media/fetchit for Jellyfin, rssignal and db-podcast to read.
- A panel in Home Assistant's sidebar: every feed and what the library holds of
  it, run / dry run / prune buttons, recent downloads, the log.
