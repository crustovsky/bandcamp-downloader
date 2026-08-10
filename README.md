# bandcamp-downloader

A small interactive CLI for downloading albums you own from your Bandcamp
collection. Browse your library in the terminal, pick items by number, and get
them extracted into a tidy `Artist/Album/` tree.

Inspired by and built on the approach in
[meeb/bandcampsync](https://github.com/meeb/bandcampsync) — this is a simpler,
interactive alternative rather than a sync daemon.

Only your own purchases are accessible. Authentication is your own browser
session, so the tool can only ever reach music already in your collection.

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/)

## Install

```bash
uv sync
```

## Authentication

The tool needs the cookies from a logged-in Bandcamp session. Export them from
your browser with any `cookies.txt` extension (Netscape format), or paste a raw
`Cookie:` header — both are accepted. Save the result as `cookies.txt`.

The file must contain an `identity` cookie; the tool errors out early if it
doesn't. `cookies.txt` is gitignored — it is a live session credential, so treat
it like a password.

## Usage

```bash
uv run bandcamp-dl -O ~/Music
```

| Flag | Effect |
| --- | --- |
| `-O <dir>` | Output directory (required) |
| `--cookies`, `-c` | Path to the cookies file (default: `cookies.txt`) |

Your collection is listed 10 items at a time:

```
=== Bandcamp Downloader ===
Library (42 items):
  1. Artist Name - Album Title
  2. Another Artist - Another Album
  ...

Showing 1-10 of 42
[number(s)] Download (e.g. 1,3,5-7)  [n/p] Next/Prev page  [q] Quit
```

| Input | Action |
| --- | --- |
| `3` | Download item 3 |
| `1,3,5-7` | Download items 1, 3, 5, 6 and 7 |
| `n` / `p` | Next / previous page |
| `q` | Quit |

Item numbers are global, so you can request anything in the collection from any
page.

## What it does

For each selected item:

1. Resolves the download page for that purchase
2. Picks the best available format — FLAC, falling back to MP3 320 then MP3 V0
3. Downloads the archive with a progress readout
4. Extracts it to `<output>/<Artist>/<Album>/`, stripping the redundant
   `Artist - Album - ` prefix from track filenames so they sort as `01 Track.flac`

Non-audio extras (artwork, PDFs, text files) are kept as-is alongside the tracks.
Single-file purchases that aren't zipped are saved directly.

If a destination folder already exists and is non-empty, you'll be asked before
it's overwritten.
