# bandcamp-downloader

A small interactive CLI for downloading albums you own from your Bandcamp
collection. Browse your library in the terminal, pick items by number, and get
them extracted into a tidy `Artist/Album/` tree.

Inspired by and built on the approach in
[meeb/bandcampsync](https://github.com/meeb/bandcampsync), but as a simpler,
interactive alternative rather than a sync daemon.

Only your own purchases are accessible. Authentication is your own browser
session, so the tool can only ever reach music already in your collection.

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/)
- OpenSSH (`ssh` and `sftp`), only if you upload to an SFTP destination

## Install

```bash
uv sync
```

## Authentication

The tool needs the cookies from a logged-in Bandcamp session. Export them from
your browser with any `cookies.txt` extension (Netscape format), or paste a raw
`Cookie:` header; both are accepted. Save the result as `cookies.txt`.

The file must contain an `identity` cookie; the tool errors out early if it
doesn't. `cookies.txt` is gitignored, since it is a live session credential.
Treat it like a password.

## Usage

```bash
uv run bandcamp-dl -O ~/Music
```

| Flag | Effect |
| --- | --- |
| `-O <output>` | Output directory, or an `sftp://` URL (required) |
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

### Uploading over SFTP

`-O` also accepts `sftp://[user@]host[:port]/path`, which sends albums straight
to another machine (a NAS, for example) instead of a local folder:

```bash
uv run bandcamp-dl -O sftp://me@nas/mnt/media/Music
```

The path is absolute on the remote host; use `sftp://host/~/Music` for a path
relative to your home directory. The remote directory is created if it is
missing.

Transfers go through the system `ssh` and `sftp` binaries, so `~/.ssh/config`
aliases, keys, agents and `known_hosts` behave as usual. One connection is
opened at startup and reused for every album, so a password or key passphrase
is asked for only once.

Each album is downloaded and extracted to a temporary directory, uploaded, then
removed locally. If an upload fails, the temporary copy is kept and its path is
printed, so nothing has to be downloaded again.

## Unattended sync

`--sync` downloads new purchases without asking anything, for use from a timer or
a container:

```bash
uv run bandcamp-dl --sync -O ~/Music
```

| Flag | Effect |
| --- | --- |
| `--sync` | Download purchases made since the first sync, then exit |
| `--state <file>` | Where the list of handled purchases is kept (default: `state.json`) |
| `--every <hours>` | Keep running and sync again every so many hours |
| `--require-existing` | Do nothing if a local output directory is missing or empty, so an unmounted disk is not written to |

The first sync downloads nothing. It records what is in your collection at that
moment, and only purchases made after it are downloaded. This keeps a library
that already holds your older purchases, perhaps under different folder names,
from being filled with duplicates. Use the interactive mode for older items.

A sync never overwrites: if an album folder already exists and is not empty, it
is left alone and the purchase is marked as handled. A download that fails is
tried again at the next sync. `state.json` lists your purchases, so it is
gitignored along with the cookies.

If a sync logs that Bandcamp answered with a bot challenge page, the cookies are
not the problem: Bandcamp turns away some releases of the HTTP library this tool
uses. The Docker image pins a release known to work (`CURL_CFFI_VERSION` in the
`Dockerfile`).

When the cookies expire, each sync logs `Sync failed: ... Check your cookies
file.` and nothing else happens until you replace them.

### Docker

`compose.yaml` runs the sync as a small container that stays up and syncs once
a day:

```bash
cp .env.example .env        # set MUSIC_DIR
mkdir config
cp /path/to/cookies.txt config/cookies.txt
docker compose up -d --build
docker logs bandcamp-sync
```

`config/` holds `cookies.txt` and the sync state and is gitignored. After
replacing the cookies, `docker restart bandcamp-sync` syncs straight away
instead of at the next interval.

## What it does

For each selected item:

1. Resolves the download page for that purchase
2. Picks the best available format: FLAC, falling back to MP3 320 then MP3 V0
3. Downloads the archive with a progress readout
4. Extracts it to `<output>/<Artist>/<Album>/`, stripping the redundant
   `Artist - Album - ` prefix from track filenames so they sort as `01 Track.flac`

Non-audio extras (artwork, PDFs, text files) are kept as-is alongside the tracks.
Single-file purchases that aren't zipped are saved directly.

If a destination folder already exists and is non-empty, you'll be asked before
it's overwritten.
