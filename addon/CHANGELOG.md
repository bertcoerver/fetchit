# Changelog

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
