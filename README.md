# xtreamfs

xtreamfs makes the video-on-demand (VOD) catalog from an Xtream Codes IPTV
provider appear as ordinary movie and TV files, so Plex can scan and play them
like local media. Plex can't read the `.strm` files most IPTV tools generate;
xtreamfs fills that gap.

```
mount/
  Movies/The Matrix (1999) {tmdb-603}/The Matrix (1999).mkv
  TV/Severance (2022) {tmdb-95396}/Season 01/Severance - S01E01.mp4
```

Nothing is downloaded. When Plex reads a file, xtreamfs streams that part of it
from the provider. It reads the catalog through the Xtream API with your
provider login, includes only the categories you choose, and re-checks for new
and removed titles every 12 hours by default.

## Setup on Unraid

Put these files in a folder on the server, shown below as `/path/to/xtreamfs`.
On Unraid, use a pool path such as `/mnt/<pool>/appdata/xtreamfs`, not
`/mnt/user/...`: the `mount` folder inside it has to be on a filesystem that can
share mounts with other containers. You also need the Compose Manager plugin or
another way to run `docker compose`. The `config` and `mount` folders are
created next to `docker-compose.yml`.

**1. Create the settings file.**

```bash
cd /path/to/xtreamfs
cp .env.example .env
nano .env        # fill in XTREAM_URL, XTREAM_USER, XTREAM_PASS
```

**2. Pick your categories.**

```bash
docker compose build
docker compose run --rm xtreamfs --list-categories
```

Put the IDs you want into `MOVIE_CATEGORIES` and `SERIES_CATEGORIES` in `.env`
(for example `MOVIE_CATEGORIES=10,14,22`), or use `all` to include every
category, including ones the provider adds later. Start small if you pick
individual IDs. You can add more later.

**3. Start it.**

```bash
docker compose up -d
docker compose logs -f
```

The first start reads the catalog. Series take longer because each show needs
its own API call. When you see `Catalog ready: N files`, check it from the host:

```bash
ls /path/to/xtreamfs/mount/Movies | head
```

**4. Give Plex access.** In the Plex template, add a Path:

| Field | Value |
|---|---|
| Container Path | `/xtream` |
| Host Path | `/path/to/xtreamfs/mount` |
| Access Mode | **Read Only - Slave** |

The **Slave** setting matters. Without it, Plex sees an empty folder, especially
if xtreamfs starts after Plex. After xtreamfs restarts, restart Plex too.

**5. Create the libraries.** Add a Movies library pointing at `/xtream/Movies`
and a TV Shows library pointing at `/xtream/TV`.

## Keeping the provider happy

Plex opens every new file while scanning it. With a big catalog, that's a lot
of stream requests. To keep the load down:

- **In each of these libraries' Advanced settings:** turn off *Generate video
  preview thumbnails*, *Generate intro/credits markers*, and *Generate chapter
  thumbnails*. Also disable *Perform extensive media analysis during
  maintenance* under Settings → Scheduled Tasks.
- **Add categories a few at a time** rather than all at once.
- **Set `MAX_STREAMS`** to your plan's connection limit, minus any connections
  you need for live TV. While a scan is running and someone is watching, they
  share that limit.
- **File sizes are looked up once** with a 1-byte request and cached in
  `config/sizes.json`, so restarts and rescans don't repeat it.

## Settings

All settings go in `.env`. See `.env.example` for the full list with notes.

| Variable | Default | Purpose |
|---|---|---|
| `XTREAM_URL` / `_USER` / `_PASS` | (required) | Provider login |
| `MOVIE_CATEGORIES` | (empty) | Movie category IDs or names, comma-separated, or `all` |
| `SERIES_CATEGORIES` | (empty) | Series category IDs or names, comma-separated, or `all` |
| `MOVIE_EXCLUDE` | (empty) | Movie categories to leave out of `all`, by ID or name |
| `SERIES_EXCLUDE` | (empty) | Series categories to leave out of `all`, by ID or name |
| `MOVIE_FOLDERS` | (empty) | Movie categories that get their own folder, e.g. `874=4K Movies` |
| `SERIES_FOLDERS` | (empty) | Series categories that get their own folder, e.g. `843=Anime` |
| `SERIES_OVERRIDES` | (empty) | Shows sent to a folder by TMDB ID, e.g. `31910,46260=Anime` |
| `MOVIE_OVERRIDES` | (empty) | Movies sent to a folder by TMDB ID |
| `MOVIE_CATEGORY_FOLDERS` | `0` | `1` puts each movie in a subfolder for its provider category: `Movies/<Category>/<Title (Year)>` |
| `STRM_DIR` | (empty) | Folder that gets a `.strm` copy of the whole library, e.g. `/strm` |
| `SHRINK_GUARD_PERCENT` | `25` | Keep the previous catalog if a refresh would remove more than this share of movies or shows (0 turns it off) |
| `MAX_STREAMS` | `1` | Most connections to the provider at once |
| `REFRESH_HOURS` | `12` | How often to re-read the catalog |
| `STRIP_PREFIX` | `1` | Remove tags like `EN - ` and `\|EN\|` from titles |
| `API_DELAY` | `0.3` | Seconds between catalog API calls |
| `USER_AGENT` | VLC | User agent sent to the provider |
| `REDIRECT_CACHE_MINUTES` | `20` | How long to reuse a title's streaming URL before asking the provider again |
| `STATS_INTERVAL` | `60` | Seconds between provider stats lines in the log (0 turns them off) |
| `UNAVAILABLE_RETRY_HOURS` | `24` | How long to hide a title the provider refuses (403/404) before checking it again |
| `CACHE_MB` | `256` | Memory for caching the start and end of recently read files (0 turns it off) |
| `CACHE_EDGE_MB` | `8` | How much of each file's start and end counts as cacheable |

## Category subfolders for movies

With a large catalog, `MOVIE_CATEGORY_FOLDERS=1` splits Movies into one
subfolder per provider category:

```
Movies/Comedy/Airplane! (1980) {tmdb-813}/Airplane! (1980).mkv
Movies/4K Movies/Dune (2021) {tmdb-438631}/Dune (2021).mkv
```

Each title belongs to one provider category, so it appears in exactly one
subfolder. A title keeps its folder if the provider later moves it to another
category. Turning the setting on or off moves every movie once, so Plex
treats them as new and analyses them again; plan for a full Movies rescan.
Plex combines a 4K copy and a regular copy of the same film into one entry
even though they now sit in different subfolders.

## A .strm copy for Emby or Jellyfin

Set `STRM_DIR=/strm` and xtreamfs also writes a `.strm` file for every title
into the `strm` folder next to `docker-compose.yml`, in the same layout as the
mount (`strm/Movies/...`, `strm/TV/...`). Each file holds the title's stream
link. Point Emby or Jellyfin libraries at those folders: they read `.strm`
files without touching the provider, so scans are fast.

- These are ordinary files on disk, written after each catalog refresh. Only
  new or changed titles are written and removed ones are deleted, with dates
  kept stable, so the media server only rescans what changed.
- Titles the provider refuses are left out.
- Emby plays from the provider directly, so its streams don't count toward
  `MAX_STREAMS`. Each file contains the provider link, including your login.
- Media info (resolution, HDR) appears in Emby after a title is first played.

## When the provider reorganizes its categories

Providers sometimes replace their categories, for example swapping
streaming-service categories for genre ones while keeping the same titles.
Three things help:

- **`MOVIE_CATEGORIES=all` / `SERIES_CATEGORIES=all`** include every category,
  so new and renumbered ones are picked up automatically. Use `MOVIE_EXCLUDE` /
  `SERIES_EXCLUDE` (IDs or names) for anything you don't want. Titles are
  matched by their stream ID, so a title that only moved to another category
  keeps its folder and Plex sees no change.
- **The log reports category changes** at each refresh, e.g.
  `Provider removed movie categories: 798 Netflix` and
  `Provider added movie categories: 879 Action`.
- **The shrink guard.** If a refresh would remove more than
  `SHRINK_GUARD_PERCENT` (25%) of your movies or of your shows, and you
  haven't changed your category settings, xtreamfs keeps the previous catalog
  and logs an error explaining what would have gone. If the removal is
  genuine, accept it with `docker exec xtreamfs touch /config/accept-shrink`;
  the next refresh starts right away and applies it.

## Separate folders for some categories

To give a category its own top-level folder, and so its own Plex library, map
it in `.env`:

```
SERIES_FOLDERS=843=Anime
MOVIE_FOLDERS=874=4K Movies,806=Anime Movies
```

That produces `/xtream/Anime`, `/xtream/4K Movies` and so on, next to `Movies`
and `TV`. You can use the category's name instead of its ID
(`SERIES_FOLDERS=Crunchyroll=Anime`), which keeps working if the provider
renumbers its categories. Mapped categories are included automatically, so they don't also
need to be in `MOVIE_CATEGORIES` / `SERIES_CATEGORIES`. When a title is in a
mapped category and in a regular one, it goes in the mapped folder only. Use a
different folder name for each mapping, and don't reuse `Movies` or `TV`.
After changing a mapping, run `docker compose up -d`. Titles move to their new
folder at the next refresh.

## Sending individual titles to a folder

When a provider files a show under a general category (Naruto under Hulu, say),
send it to a folder by its TMDB ID:

```
SERIES_OVERRIDES=46260,31910,70881,43168=Anime
MOVIE_OVERRIDES=128=Anime Movies
```

Separate IDs with commas. For more than one folder, separate the groups with
semicolons: `SERIES_OVERRIDES=31910,46260=Anime;1234,5678=Kids`. An override
beats both the category and `SERIES_FOLDERS`. The TMDB ID is in each folder's
name (`{tmdb-31910}`) or in the themoviedb.org URL. After editing, run
`docker compose up -d`. The titles move right away and Plex re-scans only
those titles.

## How it behaves

- **Naming:** titles become `Title (Year)`. When the provider supplies a TMDB
  ID, the folder gets a `{tmdb-…}` tag so Plex matches it exactly. When the
  provider lists the same movie twice, both files go in one folder and Plex
  shows them as two versions.
- **Refreshing:** the catalog is re-read every `REFRESH_HOURS`, and right
  away whenever you change the categories in `.env` and restart. To refresh
  now without restarting, run `docker exec xtreamfs touch /config/refresh`.
  It starts within about 10 seconds. Then scan the library in Plex.
- **Provider offline:** the last good catalog is kept in `config/catalog.json`,
  so the library stays in place, and playback works again once the provider is
  back.
- **Removed titles** disappear at the next refresh. Plex removes them on its
  next scan if *Empty trash automatically after every scan* is on. Otherwise,
  run Empty Trash yourself.
- **Logs** mask your username and password.

## Reading the stats line

Every minute with provider activity, the log shows a line like:

```
Provider, last 60s: 42 requests for 15 files | first byte avg 1.84s, p90 3.10s |
9 redirects followed, 33 redirect-cache hits | 220 MB read | 0 errors
```

- **files** per minute is roughly how fast a Plex scan is moving.
- **first byte** is how long the provider takes to start answering. This
  sets the speed limit for scans.
- **redirect-cache hits** are requests that skipped the provider's redirect.
- **reads served from memory** are repeat reads of a file's start or end that
  never reached the provider.
- **unavailable at provider** are titles the provider refuses (403/404), usually
  broken uploads. They're hidden from Plex and checked again daily; the list is
  in `config/unavailable.json`.
- **errors** above zero during a scan usually mean throttling or a connection
  limit. Lower the load or check `MAX_STREAMS`.

## Troubleshooting

- **Plex sees an empty folder:** check that the Plex path is set to Slave, then
  restart Plex.
- **`Transport endpoint is not connected` on the host:** xtreamfs stopped
  uncleanly. Run `fusermount -uz /path/to/xtreamfs/mount`, then
  `docker compose up -d`.
- **`Category … not found`:** the name or ID doesn't match. Use the IDs from
  `--list-categories`.
- **Files show as 0 bytes or won't open:** the provider refused the size lookup.
  Look for `Size probe` lines in the logs. A 403 usually means the provider
  wants a different `USER_AGENT`, or your connection limit is in use.
