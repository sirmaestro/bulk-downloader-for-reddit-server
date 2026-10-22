# Bulk Downloader for Reddit Server

A fork of [Bulk Downloader for Reddit](https://github.com/aliparlakci/bulk-downloader-for-reddit) (BDFR) that keeps folders of Reddit media up to date. It runs as a small server, `bdfr serve`, for example in Docker on an always-on machine. A Tampermonkey userscript sends it the posts on the old.reddit.com pages you visit, and it checks a watch list of users against the [Arctic Shift](https://arctic-shift.photon-reddit.com) archive every week. Media is downloaded from where it is hosted (i.redd.it, v.redd.it, Redgifs, Imgur and so on), and each folder keeps a hash store, so nothing is downloaded twice. `bdfr download` does the same from the command line.

**Reddit's API is closed to personal scripts.** Since November 2025, Reddit has required approval for API access and rejects personal tools, so upstream BDFR gets HTTP 403 errors. This fork reads posts from Arctic Shift, or from Reddit in your browser through the userscript. Version 3.0 removed everything that needed Reddit's API; see [Removed in 3.0](#removed-in-30).

What this fork adds over upstream:

- `bdfr serve`, the [userscript](#downloading-from-the-browser) and [watched users](#watched-users)
- Reading posts from Arctic Shift or from JSON exported in the browser (`--post-json`)
- Duplicate detection that survives between runs: hashes kept in JSON files (`--keep-hashes`) or SQLite (`--keep-hashes-db`), plus the URLs already downloaded
- Stopping a listing once it reaches media you already have (`--stop-at-existing`), and downloading several posts at once (`--parallel`)
- Offline renaming of downloaded files to a new file scheme (`bdfr rename`)
- Unicode file names on Windows (`--filename-character-set`), faster failure on network errors (`--fail-fast`) and Imgur tweaks (`--imgur-originals`, `--imgur-fix404`)

## Installation

Needs Python 3.15 or later, and [ffmpeg](https://ffmpeg.org) on `PATH` for Reddit-hosted videos. To run the server in Docker instead, see [Running in Docker](#running-in-docker).

```bash
git clone https://github.com/sirmaestro/bulk-downloader-for-reddit-server.git
cd bulk-downloader-for-reddit-server
python -m pip install -e .
```


This installs the `bdfr` command (or run `python -m bdfr`). To update, run `git pull` in the folder. Don't run `bdfr/__main__.py` directly: Python then imports any other `bdfr` installed from PyPI instead of this fork, and the fork's options are ignored with "Ignoring an unknown CLI argument" warnings.

`bdfr --version` shows the installed version.

## Running the server

### Downloading from the browser

The Tampermonkey script [scripts/tampermonkey/bdfr-export.user.js](scripts/tampermonkey/bdfr-export.user.js) adds a panel to old.reddit.com user pages with a **Download** button and an **Archive** checkbox. The downloading is done by BDFR itself, running in the background with `bdfr serve`, so files go to your folders with all of its duplicate checks.

1. Start the server, giving the folder that holds your per-user or per-subreddit folders and the download options to use for every request:

    ```bash
    bdfr serve D:\red
    ```

    By default `serve` runs as if given `--no-dupes --search-existing --keep-hashes-db --folder-scheme "" --file-scheme "{POSTID}_{TITLE}" --parallel 8 --media-only`: files go straight into each user's folder, named by post ID, with every file's hash kept in that folder's `hashes.sqlite`, duplicates skipped, and self posts and crossposts left out (as the script does). Options given on the command line or in `--opts` override these (in an `--opts` file, flags can be turned off, e.g. `no_dupes: false`), and `--no-defaults` uses the `download` command's defaults instead. `bdfr download` keeps its own defaults.

    It listens on `http://127.0.0.1:7635` (change with `--port`) and prints a token, which is also saved as `serve_token.txt` in BDFR's [configuration directory](#configuration). Only programs on your computer can reach it, and every request must carry the token.

2. Install the script in Tampermonkey: with Tampermonkey installed, open [bdfr-export.user.js from the latest release](https://github.com/sirmaestro/bulk-downloader-for-reddit-server/releases/latest/download/bdfr-export.user.js). Tampermonkey checks that address for updates. The first time you click a button it asks for the server address and token (change them later with **⚙**).
3. Open a user's page, such as `old.reddit.com/user/someone/submitted`. A small panel appears at the bottom right (it brightens when you hover over it), and posts you already have are marked as the page loads, including posts added by RES's Never Ending Reddit. Click:
    - **Download** to download the posts on the page that aren't marked yet, into a folder named after the user. When the page may not have everything new, it also reads the user's posts from the Arctic Shift archive, starting just below the oldest post on the page (the page's own posts are already in hand) and going back until 5 already-downloaded posts in a row (`--stop-at-existing`). That happens when the page is empty (the user's posts are hidden or removed on Reddit, but the archive may still have them), or its oldest post isn't downloaded yet, so there may be more beyond it. Arctic Shift is hours behind Reddit, so it can't add anything newer than the page; when the page already reaches posts you have, no archive request is made.
    - Tick **Archive** (remembered) to have every Download go through the rest of the user's archive, everything older than the page's oldest post, finding posts that were removed from Reddit or are older than your last download. Posts removed from Reddit within the page's own time range aren't looked for; scroll less far, or use an empty page, to include them. Posts already in the folder, by ID, link, or as known duplicates, are recognised without any request, so this costs the listing itself: about one request per 60–200 archived posts (e.g. 11 requests for a user with 618 archived posts).

What the script sends:

- Posts linking straight to media (i.redd.it, v.redd.it, Redgifs, Imgur, ...) are sent as they are on the page; that is all BDFR needs. v.redd.it videos are downloaded from the video's full playlist, in the best quality with sound.
- Galleries: the page doesn't list a gallery's images, so before sending, the script reads them from Reddit with your browser's login (`old.reddit.com/by_id/…json`, one request for up to 100 galleries). Galleries show ⋯ and the panel says *Reading N galleries from Reddit…* meanwhile. If Reddit doesn't answer, they are sent anyway and BDFR looks them up in Arctic Shift; the panel then shows why next to the job's progress (e.g. *Reddit asked to log in*), and so does the lookup line in `serve_log.txt`. The first time, Tampermonkey asks you to allow the script to connect to old.reddit.com. Details of the failure (Reddit's answer, whether the page is logged in, the script handler) are logged as an error in the browser console (F12), starting `BDFR:`.
- Self posts, crossposts, links to other Reddit posts and posts missing details on the page are ignored: they are marked – (hover for why) and not sent.

Marks next to post titles: green ✔ downloaded now, grey ✔ already downloaded, grey ≡ duplicate (the same media as something already downloaded, re-uploaded under another link, so nothing new was written), orange ! skipped (filtered out or failed; the reason is in `serve_log.txt` in the configuration directory), – ignored, ⋯ reading the gallery from Reddit. Already-downloaded posts are found from the post IDs in the file names in the folder (this needs a file scheme containing `{POSTID}`, as serve's default does; old and new names can be mixed in one folder), and from the folder's hash store: a post linking to something already downloaded, such as a repost of the same Redgifs video, is marked too and not sent. Requests are queued and run one at a time. Don't run `bdfr download` into the same folder while the server is working on it.

The panel only appears on user pages. To use it on subreddit pages too (downloading into a folder named after the subreddit), set `subredditPages: true` in `SETTINGS` at the top of the script.

On every old.reddit page, links to a user's profile go to their submitted posts, newest first (`old.reddit.com/user/<name>/submitted/?sort=new`), however you open them (click, middle-click, "Open in new tab"). Turn this off with `userLinksToSubmitted: false` in `SETTINGS`.

**Collect** (off until you click it, and remembered after that) makes the script keep every post it sees on the pages you visit. **Export** then saves them as JSON for `--post-json`, and **Clear** forgets them; neither needs the server.

### Watched users

`bdfr serve` keeps the folders of the users listed in `watchlist.yaml` (next to BDFR's [configuration](#configuration), or the file given with `--watchlist`) up to date without any clicks. By default only the userscript's background sync feeds them (free, from Reddit); the Arctic Shift archive is read only for users with archive checks on:

```yaml
archive_checks: false  # true: read the archive for every user without their own 'archive' (default false)
interval_days: 7    # how often each user's archive is checked
overlap_days: 7     # each check starts this much before the previous one, for posts the archive got late
retry_hours: 24     # a failed check is retried after this long
hibernate_days: 60  # users with no post for this long aren't checked in the archive any more
gone_active_days: 14   # suspended or deleted accounts that posted within this long are still synced as usual
gone_recheck_days: 30  # after that they aren't synced or checked; looked at again after this long
users:
  - someone                  # into the folder "someone"
  - name: someone_else
    folder: Some folder
    archive: true            # read this user's whole archive once, then check it weekly
  - name: new_account
    aliases: [old_account]   # moved from old_account: its files belong in "new_account", and it isn't checked
```

Archive checks also decide whether posts the userscript sends (its background sync, or Download without the archive) may be looked up in Arctic Shift. Without them, nothing is requested: a gallery Reddit no longer lists the images of (usually a removed post) is skipped as removed, although the archive may still have its images. A Download that reads the user's archive listing (an empty page, or the Archive checkbox) still looks posts up. Without a `watchlist.yaml`, posts are looked up as before.

To move a user's old account into their new one, move the old folder's files into the new folder and list the old name under `aliases`. Moved files are hashed again when the next job for that folder starts (`--search-existing`), and their posts are recognised by the IDs in their file names; only the old folder's record of downloaded links is lost, unless its `hashes.sqlite` is merged too. Jobs and checks for the old name (e.g. Download on the old account's page) go to the new folder.


### Running in Docker

To run the server on an always-on machine, use the image `ghcr.io/sirmaestro/bulk-downloader-for-reddit-server` (`latest` is the newest release, and each release also has its version, such as `3.0.0`; for amd64 and arm64) with [docker-compose.yml](docker-compose.yml): set the download folder, the user ID that should own the files and `BDFR_TOKEN`, then `docker compose up -d`. To update, run `docker compose pull && docker compose up -d`; to build from your checkout with the [Dockerfile](Dockerfile) instead, `docker compose up -d --build`. The watchlist goes in `config/bdfr/watchlist.yaml`. The container listens on the network (`--host 0.0.0.0`), so in the userscript's ⚙ use the machine's LAN address, e.g. `http://192.168.1.20:7635`. The token is the only protection and isn't encrypted, so keep the port inside your home network.


Day to day, the userscript brings watched users' new posts from Reddit itself: on any old.reddit page you open, users not synced for 6 hours (`syncEveryHours` in `SETTINGS`) have their newest posts read with your login (`submitted.json`, 100 posts per request, 2 seconds apart) until 5 posts in a row are already downloaded, and the new media posts are sent to the server as a job. Galleries come with their images, so this costs Arctic Shift nothing. One tab syncs at a time, and nothing shows on the page; failures (logged out, the server unreachable, ...) are logged in the browser console (F12) as warnings starting `BDFR sync:`, and that user is tried again after an hour. A suspended or deleted account (Reddit answers 403 or 404 and the profile says so) is reported to the server and gets no archive checks. While its newest post is less than `gone_active_days` (14) old it is still synced as usual, since temporary suspensions and shadowbans get lifted; after that the server leaves it out of the sync, and every `gone_recheck_days` (30) it is looked at on Reddit once more, and any post of theirs sent to the server brings it back sooner. Reddit also answers 404 for shadowbanned accounts, so "deleted" isn't always for good. `GET /watchlist` lists these users under `gone`; hibernating users (no post for `hibernate_days`) are synced weekly, which is what notices them posting again, and aliases are never synced. For NSFW users, allow mature content in your Reddit preferences. To sync every watched user now, however recently they were synced, click Sync in the panel or pick "Sync watched users now" in Tampermonkey's menu on any old.reddit page. Clicking Sync again while it runs stops it after the current user; a Sync that didn't finish (stopped, tab closed, server down) is carried on by the next one within a day, skipping the users it already synced. The sync follows Reddit's rate limit: when Reddit says few requests are left it waits for the limit to reset (shown in the panel), and an HTTP 429 is waited out and retried up to 3 times. Its waits keep their pace in background tabs. Turn it off with `backgroundSync: false`. Reddit itself only lists a user's newest 1000 posts and nothing removed, which is what the archive checks below are for.

For users with archive checks on, the first check reads their whole archive (once; `full_archive_done` in the progress file, so turning `archive: true` on for a user who was checked before still reads it all). After that, each check reads every archived post since the previous check (plus the overlap) to its end, so posts missing between downloaded ones are found too; posts already in the folder cost no requests, so a quiet week is one request. Checks run one at a time with a minute between them, and the file is read again as it changes, so there's no need to restart. A check that fails (Arctic Shift unavailable, ...) is logged as a warning and its window is read again later, so nothing is skipped. Users whose newest post is older than `hibernate_days` (stopped posting, banned, moved account) hibernate: the archive isn't read for them until the userscript sends a newer post. Scheduled checks wait until no job from the userscript is queued, so syncs and Download clicks don't wait behind a long archive read. `GET /watchlist` (with the token) shows each user's last successful check, last error, next check, newest post and whether they hibernate (and `alias_of` for aliases); progress is in `watchlist_state.json` next to the watchlist.

## Command-line downloads

`bdfr download` runs one download and exits, with the same duplicate checks as the server but none of its defaults (see [Options](#options)).

```bash
# New posts by a user, stopping at ones already downloaded
bdfr download ./output --user someone --submitted --stop-at-existing 5 --no-dupes --search-existing --keep-hashes-db

# The latest 50 posts in two subreddits from the past week
bdfr download ./output --subreddit 'pics, aww' -L 50 -t week

# Specific posts
bdfr download ./output -l 1xa496l -l https://www.reddit.com/r/aww/comments/1xae8q4/

# Posts collected in the browser (see --post-json)
bdfr download ./output --post-json bdfr-posts-20261020-153000.json
```

Options can also come from a YAML file with `--opts my_opts.yaml`, using the option names with underscores instead of dashes (`file_scheme`, not `file-scheme`). Options given more than once on the command line are lists in YAML. Command line options take priority over the file.

### Keeping a folder up to date

For regular runs, combine:

- `--stop-at-existing 5` so each listing stops after 5 already-downloaded posts in a row, which usually means one request to Arctic Shift per user or subreddit
- `--keep-hashes-db` (or `--keep-hashes`) so downloaded media URLs and file hashes are remembered between runs
- `--no-dupes` and `--search-existing` so the same media posted again under a different URL isn't saved twice

## Options

These apply to `bdfr download` and `bdfr serve` (where they apply to every job, on top of [serve's defaults](#downloading-from-the-browser)).

### Sources

Posts are read from Arctic Shift, newest first; nothing is requested from Reddit's API. Arctic Shift keeps posts that were later deleted, so their media may no longer download. The number of requests made to Arctic Shift is logged at the end of each run.

- `directory`
    - Where downloads are saved
- `-u, --user`
    - A user to read posts from, with `--submitted`; can be given more than once
- `--submitted`
    - Use the posts submitted by each `--user`
- `-s, --subreddit`
    - A subreddit to read posts from; can be given more than once or as a comma-separated list, e.g. `-s 'pics, aww'`
- `-l, --link`
    - A post to download, as a URL or ID; can be given more than once
    - Posts are looked up in Arctic Shift, 100 per request
- `--include-id-file`
    - Files of post IDs to download, one per line; can be given more than once
- `--post-json`
    - JSON files of posts to download; can be given more than once
    - Made with **Export** in the Tampermonkey script (see [Downloading from the browser](#downloading-from-the-browser)), which collects posts from every old.reddit.com page you visit
    - Posts linking straight to media, v.redd.it videos and galleries with their `gallery_data` are downloaded from the exported data without any lookup; other galleries, text posts and crossposts are completed from Arctic Shift, 100 per request
    - Plain lists of posts, Arctic Shift responses and Reddit listing JSON are also accepted
- `-L, --limit`
    - The most posts to read from each source, e.g. `-L 10` with three subreddits reads up to 30 posts
- `-t, --time`
    - Only posts from the last `hour`, `day`, `week`, `month` or `year`; `all` (default) has no limit
- `--parallel`
    - How many posts to download at once, from 1 (default) to 16; 4 is a good start when many posts are videos (Redgifs, v.redd.it)
    - Only fetching and downloading run side by side: duplicate checks, the hash store and writing files happen one at a time, so duplicates are still caught, and results are reported (and `--stop-at-existing` counted) in listing order
    - When a listing stops, posts already started are finished, so a few more posts than with `--parallel 1` may be checked
    - Posts sharing a link (reposts, or the same post twice on a page) download it once: a post whose link another is downloading waits for it, then counts as already downloaded
- Links already downloaded: with a hash store, the media URL of every download is stored, and for posts with a single file (e.g. Redgifs, v.redd.it, Imgur images) so is the post's own link. A later post with the same link is then recognised as already downloaded without any request, not even to ask Redgifs where the video is. Existing stores gain post links as their posts come up again
- `--stop-at-existing`
    - Stops reading a listing after this many already-downloaded posts in a row, then moves on to the next one
    - A post counts as already downloaded when its files already exist at their destination or, with `--keep-hashes`/`--keep-hashes-db`, its media URLs are already stored. Posts that are filtered out, fail, or turn out to be duplicates by hash don't count either way, since a new post can repost old media
    - Posts given with `--link`, `--include-id-file` or `--post-json` are always all checked, and come before the listings. A listing skips posts already handled in the same run without counting them

### Filtering

- `--exclude-id`, `--exclude-id-file`
    - Posts to skip, by ID or from files of IDs (one per line); can be given more than once
- `--ignore-user`
    - Skip posts by this user; can be given more than once. `DELETED` skips posts by deleted accounts
- `--skip`
    - Skip media with this file extension, e.g. `--skip mp4`; can be given more than once
- `--skip-domain`
    - Skip media from this domain, e.g. `example.com` or `img.example.com`; can be given more than once
- `--skip-subreddit`
    - Skip posts from this subreddit; can be given more than once or as a comma-separated list
- `--min-score`, `--max-score`
    - Skip posts with fewer or more upvotes than this. Posts whose score isn't known (some exported posts) aren't filtered
- `--min-score-ratio`, `--max-score-ratio`
    - Skip posts with a lower or higher upvote ratio than this. Exported posts are looked up in Arctic Shift to get their ratio
- `--disable-module`
    - Don't use this downloader module; can be given more than once. See [Disabling Modules](#disabling-modules)

### Duplicates and the hash store

- `--no-dupes`
    - Don't save media whose content (MD5 hash) was already downloaded, in this run or, with `--search-existing`, `--keep-hashes` or `--keep-hashes-db`, before
- `--make-hard-links`
    - Instead of skipping such duplicates, hard link the new file to the existing one so it takes no extra space. If the existing file has gone, a copy is written
- `--search-existing`
    - Hash every file already in `directory` before downloading, so duplicates of them are recognised
    - With `--keep-hashes` or `--keep-hashes-db`, only files not already in the store are hashed
- `--keep-hashes`
    - Keep the hashes between runs in three files in `directory`: `hash_list.json`, `hash_file_list.json` and `hash_url_list.json`. They are loaded at the start and saved at the end, keeping the previous version as `.bak`
    - The store also records each downloaded media URL, so later runs skip those URLs without downloading them. URLs of files that are already in place under their expected name are recorded too, from the files' stored hashes
    - If files change outside BDFR, delete the three files to start again with a full scan
    - Don't run two instances against the same `directory` with this option
- `--keep-hashes-db`
    - Keep the same data in a SQLite database, `hashes.sqlite` in `directory`, which is written as it goes
    - Given together with `--keep-hashes` once, the JSON files are converted into the database; after that use `--keep-hashes-db` alone
- `--save-hashes-interval`
    - With `--keep-hashes`, also save the files after this many new entries (10 to 1000), in case a run is interrupted

### Downloading

- `--max-wait-time`
    - The longest to wait between retries of a rate-limited download, in seconds (default 120); see [Rate Limiting](#rate-limiting)
- `--fail-fast`
    - Wait only 5, then 10 seconds before retrying a failed download, instead of 60 seconds and more. Connection errors such as `[Errno 11001]` and `[WinError 10061]` fail at once
- `--imgur-originals`
    - Download Imgur originals (e.g. GIFs) rather than Imgur's MP4 conversions
- `--imgur-fix404`
    - When Imgur's API returns 404 for a `.gif` or `.gifv` link, try downloading the GIF or MP4 directly

### Naming and output

- `--file-scheme`
    - The file name scheme, default `{REDDITOR}_{TITLE}_{POSTID}`; see [Folder and File Name Schemes](#folder-and-file-name-schemes)
- `--rename-existing`
    - When a post's media is found to be downloaded already (by its stored URL or its hash), rename the existing file to the name the current `--file-scheme` gives it, instead of leaving the old name
    - Only files whose name contains the post's ID are renamed; an identical file belonging to another post keeps its name
    - The hash store is updated with the new names. See [Changing the file scheme](#changing-the-file-scheme)
- `--folder-scheme`
    - The folder scheme, default `{SUBREDDIT}`; `''` puts everything directly in `directory`
- `--time-format`
    - The format of `{DATE}` in names; see [Time Formatting Customisation](#time-formatting-customisation)
- `--filename-restriction-scheme`
    - `windows` or `linux`: which file system's naming rules to follow, instead of detecting them; see [Filesystem Restrictions](#filesystem-restrictions)
- `--filename-character-set`
    - Which characters file names may contain under Windows rules: `ascii` (default), `unicode`, or `unicode_no_emojis`

### General

- `--config`
    - The configuration file to use; see [Configuration](#configuration)
- `--opts`
    - A YAML file of options; see [Usage](#usage)
- `--log`
    - Where to write the log file; needed when running several instances at once, see [Multiple Instances](#multiple-instances)
- `-v, --verbose`
    - Log more detail; can be given twice

## Removed in 3.0

Reddit's API no longer grants access to personal scripts, so everything that needed it has been removed:

- The `archive` and `clone` commands (post data and comments as JSON, XML or YAML)
- `--source reddit`; posts always come from Arctic Shift, `--link`/`--include-id-file` IDs or `--post-json` files
- `--authenticate`, `--saved`, `--upvoted`, `--subscribed`, `-m, --multireddit`, `--search` and `-S, --sort` (Arctic Shift listings are always newest first)
- `bdfr completion`, the `bdfr-archive`, `bdfr-clone` and `bdfr-download` commands, and `scripts/unsaveposts.py`

Old `--opts` files that still set these are read with an "Ignoring an unknown YAML argument" warning. The `client_id`, `client_secret`, `scopes` and `user_token` keys in existing configuration files are ignored. If you still have Reddit API access and need these, use a version before 3.0 from the git history.


## Folder and File Name Schemes

File and folder names are built from schemes in which these keys, in curly brackets, are replaced with the post's details:

- `DATE`
- `FLAIR`
- `POSTID`
- `REDDITOR`
- `SUBREDDIT`
- `TITLE`
- `UPVOTES`

For example `download_{POSTID}` gives `download_aaaaaa.png`, `download_bbbbbb.png` and so on. Gallery images get `_1`, `_2`, ... added.

The file scheme must contain at least one key, and should contain `{POSTID}`: it is the only key guaranteed to be unique, and BDFR skips a download when a file with its name already exists. The folder scheme can be empty or a fixed string, which puts every file in the same folder.

### Changing the file scheme

When the folder already separates users (e.g. one folder per user under `bdfr serve D:\red`), the user name in every file name is redundant. BDFR sets each file's modified time to the post's creation time, so the date doesn't need to be in the name either. A short scheme that keeps the ID safe from title shortening is:

```bash
--file-scheme "{POSTID}_{TITLE}"
```

To move existing files to a new scheme offline, use `bdfr rename`. It reads the post ID, title and user name back from the old names (and `{DATE}` from modified times), renames the files, and updates the hash store (`hashes.sqlite` and the `hash_*.json` files). It makes no network requests.

```bash
bdfr rename D:\red\someone --file-scheme "{POSTID}_{TITLE}" --dry-run   # show what would change
bdfr rename D:\red\someone --file-scheme "{POSTID}_{TITLE}"
```

- `--from-scheme` is the scheme the old names were made with, default `{REDDITOR}_{TITLE}_{POSTID}`
- Files whose names don't match it, or whose new name is already taken, are left alone and listed
- `--no-subfolders` only renames files directly in the folder
- `--folder-is-user` only renames files starting with the folder's name, for folders named after their user. Without it, a user name containing `_` in a folder not named after that user can't be told apart from the title, and part of it may end up in the new name
- Before renaming, the hash store is copied to `<name>.before-rename-<time>`, and a list of new and old names is saved as `bdfr-rename-<time>.json` in the folder
- Modified times are kept, and gallery images keep their `_1`, `_2`, ... numbering
- `--build-hash-db` also creates `hashes.sqlite` if the folder has none and adds the content hash of every file not already in it, so `--keep-hashes-db` runs recognise them by content. It can be used without renaming by giving the folder's current scheme as `--file-scheme`. Folders that keep their hashes in `hash_*.json` files are left alone; convert those by running a download once with both `--keep-hashes` and `--keep-hashes-db`
- Media URLs can't be recovered offline, so a built hash DB starts without them. They are filled in as posts come up in later downloads: when a post's file is already in place, its URL is stored without downloading anything

Afterwards, run downloads with the same `--file-scheme` so new files match. Alternatively, `--rename-existing` on `download` or `serve` renames files as their posts come up again, using the stored URLs and hashes.

## Configuration

Configuration and log files live in the user's configuration directory:

- Windows: `C:\Users\<User>\AppData\Local\BDFR\bdfr` (with the Microsoft Store Python, under `C:\Users\<User>\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.x_<id>\LocalCache\Local\BDFR\bdfr`)
- macOS: `~/Library/Application Support/bdfr`
- Linux: `~/.config/bdfr/`

BDFR uses the first of these that exists: `--config`, `./config.cfg`, `./default_config.cfg`, then `config.cfg` and `default_config.cfg` in the configuration directory. If none exists, the default configuration is copied to `default_config.cfg` there. Each run's log is written there as `log_output.txt`, keeping `backup_log_count` (default 3) previous logs.

### Reading the logs

`bdfr serve` logs everything, every job included, to `serve_log.txt` in BDFR's [configuration directory](#configuration) (or `--log`), which is kept across restarts and rotated at 10 MB. `bdfr download` starts a new `log_output.txt` for each run, keeping the previous ones as `.1`, `.2`, .... The console (in Docker, `docker logs`) shows the same lines from INFO up; the files also have the DEBUG lines for each file, and `-vv` adds TRACE lines such as every Arctic Shift request URL and the tracebacks of errors.

Each line starts with the time and level, then which job and post it belongs to, so lines from posts downloading in parallel can be told apart. To follow one job or post, search the log for its ID (`job 3fd9e1485ded`, `post 1x9xkq0`).

```
2026-10-21 09:09:15 INFO    job 3fd9e1485ded: Started: 200 posts from a page into /data/someone
2026-10-21 09:09:15 INFO    job 3fd9e1485ded: Looking up 59 of 200 posts in Arctic Shift:
  gallery (40): 1x9xkq0, 1x9xhye, ...
  self post (15): ...
2026-10-21 09:09:16 INFO    job 3fd9e1485ded: Arctic Shift request 1: posts/ids (59 IDs) -> 59 posts in 0.9s
2026-10-21 09:09:16 DEBUG   job 3fd9e1485ded, post 1x9y297: Redgifs: https://www.redgifs.com/watch/somename
2026-10-21 09:09:17 DEBUG   job 3fd9e1485ded, post 1x9y297: Wrote /data/someone/1x9y297_Title.mp4
2026-10-21 09:09:17 INFO    job 3fd9e1485ded, post 1x9y297: new - 1 file written (#1, r/u_someone, Redgifs)
2026-10-21 09:09:18 INFO    job 3fd9e1485ded, post 1x9xkq0: new - 4 files written (#2, r/pics, Gallery)
2026-10-21 09:09:18 INFO    job 3fd9e1485ded, post 1wzgo2n: duplicate - 2 same as already downloaded 1wzgm15_Title_1.jpg, 1wzgm15_Title_2.jpg (#3, r/pics, Gallery)
2026-10-21 09:09:18 INFO    job 3fd9e1485ded, post 1wzg1ab: already downloaded - 1 already in the folder (#4, r/pics, Direct)
2026-10-21 09:09:19 WARNING job 3fd9e1485ded, post 1wzf0cd: failed - could not download https://i.imgur.com/x.jpg: HTTP 404 (#5, r/pics, Imgur)
2026-10-21 09:09:20 INFO    job 3fd9e1485ded: Finished 200 posts: 2 new, 1 already downloaded, 1 duplicate, 195 skipped, 1 failed; 5 files written
2026-10-21 09:09:20 INFO    job 3fd9e1485ded: Made 1 request to Arctic Shift (59 posts looked up: 40 gallery, 15 self post, 4 crosspost or link to another Reddit post)
2026-10-21 09:09:20 INFO    job 3fd9e1485ded: Done in 5s
```

- **Lookups**: posts from the userscript or `--post-json` that link straight to media, v.redd.it videos and galleries sent with their image list are downloaded from the page's data. Other galleries, self posts, crossposts and posts with incomplete data need the full post from Arctic Shift. These are looked up 100 per request, and the log lists them by reason.
- **Per post**, one line: `new` (something was written), `already downloaded` (its files are already in the folder, or were renamed to the current scheme with `--rename-existing`), `duplicate` (the same content as an earlier download), `skipped` (a filter, or nothing to download) or `failed` (an error, logged as a warning), with the reason, then its place in the run, subreddit and site.
- **`bdfr serve`**: each job logs when it is queued, starts and ends (`Done in 5s`, or a warning `Failed after 5s: ...` when it didn't get through everything, e.g. Arctic Shift stopped answering). Polling by the userscript isn't logged, only failed requests. The scheduler's lines have no job.

### Configuration File

All keys are optional:

- `backup_log_count`
- `max_wait_time`
- `time_format`
- `disabled_modules`
- `filename_restriction_scheme`

### Time Formatting Customisation

`time_format` (or `--time-format`, which takes priority) sets the format of `{DATE}` in names, using Python's [format codes](https://docs.python.org/3/library/datetime.html#strftime-strptime-behavior). The default is [ISO 8601](https://en.wikipedia.org/wiki/ISO_8601).

### Disabling Modules

Downloader modules can be disabled with `--disable-module` or the `disabled_modules` configuration key. This is the only way to stop the yt-dlp fallback, which handles hundreds of sites. The names, case-insensitive, are:

- `Direct`
- `DelayForReddit`
- `Erome`
- `Gallery` (Reddit galleries)
- `Gfycat`
- `Imgur`
- `PornHub`
- `Redgifs`
- `SelfPost` (Reddit text posts)
- `Vidble`
- `VReddit` (Reddit videos)
- `Youtube`
- `YtdlpFallback` (yt-dlp fallback)

### Rate Limiting

When a site rate-limits a download (or the connection fails), BDFR waits 60 seconds and retries, then 120 seconds, and so on while the wait is at most `--max-wait-time` (or `max_wait_time`, default 120): with the default, three attempts and 180 seconds of waiting for one download. A site's `Retry-After` is waited for instead, or the download fails at once if it asks for longer than `--max-wait-time`. `--fail-fast` waits 5 and then 10 seconds.

## Multiple Instances

Several `bdfr download` runs can be scripted one after another, for example with different `--config` files or sources. Runs at the same time must each use their own `--log` file, as Windows doesn't allow two processes to write the same log, and must not share a `directory` when using `--keep-hashes`.

## Filesystem Restrictions

BDFR follows the naming rules of the system it runs on. When saving to a drive with stricter rules, such as a Windows (SMB) share from Linux, set `--filename-restriction-scheme windows` or `filename_restriction_scheme` in the configuration file.

## List of currently supported sources

- Direct links to files
- Delay for Reddit
- Erome
- Gfycat (old links are fetched from Redgifs, where Gfycat's surviving clips moved)
- Imgur
- PornHub
- Reddit galleries
- Reddit text posts
- Reddit videos
- Redgifs
- Vidble
- YouTube
    - Any site supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), chosen from the link alone; links to other pages (OnlyFans, Fanvue, ...) are skipped without a request, as yt-dlp's generic page reader isn't used

## Contributing

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how a download flows through the code, and [AGENTS.md](AGENTS.md) for the layout, how to run the tests, and the invariants that keep existing folders from being downloaded again.

Pushes to `main` and pull requests run the tests. To make a release, push a version tag (`git tag v3.0.1 && git push origin v3.0.1`): [release.yml](.github/workflows/release.yml) runs the tests, publishes the image as `3.0.1`, `3.0` and `latest`, and creates a GitHub release with the userscript attached. Raise the userscript's `@version` when it changes, or Tampermonkey won't offer the update.