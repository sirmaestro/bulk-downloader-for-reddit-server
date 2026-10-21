# Bulk Downloader for Reddit (saved-hashes fork)

A fork of [Bulk Downloader for Reddit](https://github.com/aliparlakci/bulk-downloader-for-reddit) (BDFR) built for keeping folders of Reddit media up to date: run it regularly and it fetches what's new, skipping what you already have.

**Reddit's API is closed to personal scripts.** Since November 2025, Reddit has required approval for API access and rejects personal tools, so upstream BDFR gets HTTP 403 errors. This fork reads posts from the [Arctic Shift](https://arctic-shift.photon-reddit.com) archive of Reddit instead, and downloads the media from where it is hosted (i.redd.it, v.redd.it, Redgifs, Imgur and so on). Features that only work through Reddit's API are [deprecated](#deprecated-reddit-api-features).

What this fork adds over upstream:

- Reading posts from Arctic Shift (`--source`, the default) or from JSON exported in the browser (`--post-json`)
- Duplicate detection that survives between runs: hashes kept in JSON files (`--keep-hashes`) or SQLite (`--keep-hashes-db`), plus the URLs already downloaded
- Stopping a listing once it reaches media you already have (`--stop-at-existing`)
- Unicode file names on Windows (`--filename-character-set`), faster failure on network errors (`--fail-fast`) and Imgur tweaks (`--imgur-originals`, `--imgur-fix404`)

## Installation

Needs Python 3.9 or later, and [ffmpeg](https://ffmpeg.org) on `PATH` for Reddit-hosted videos.

```bash
git clone https://github.com/sirmaestro/bulk-downloader-for-reddit_with-saved-hashes.git
cd bulk-downloader-for-reddit_with-saved-hashes
python -m pip install -e .
```

This installs the `bdfr` command (or run `python -m bdfr`). To update, run `git pull` in the folder. Don't run `bdfr/__main__.py` directly: Python then imports any other `bdfr` installed from PyPI instead of this fork, and the fork's options are ignored with "Ignoring an unknown CLI argument" warnings.

`bdfr --version` shows the installed version.

## Usage

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

Options can also come from a YAML file with `--opts my_opts.yaml`, using the option names with underscores instead of dashes (`file_scheme`, not `file-scheme`); see [opts_example.yaml](opts_example.yaml). Options given more than once on the command line are lists in YAML. Command line options take priority over the file.

### Keeping a folder up to date

For regular runs, combine:

- `--stop-at-existing 5` so each listing stops after 5 already-downloaded posts in a row, which usually means one request to Arctic Shift per user or subreddit
- `--keep-hashes-db` (or `--keep-hashes`) so downloaded media URLs and file hashes are remembered between runs
- `--no-dupes` and `--search-existing` so the same media posted again under a different URL isn't saved twice

### Downloading from the browser

The Tampermonkey script [scripts/tampermonkey/bdfr-export.user.js](scripts/tampermonkey/bdfr-export.user.js) adds a panel to old.reddit.com user pages with a **Download** button and an **Archive** checkbox. The downloading is done by BDFR itself, running in the background with `bdfr serve`, so files go to your folders with all of its duplicate checks.

1. Start the server, giving the folder that holds your per-user or per-subreddit folders and the download options to use for every request:

    ```bash
    bdfr serve D:\red
    ```

    By default `serve` runs as if given `--no-dupes --search-existing --keep-hashes-db --folder-scheme "" --file-scheme "{POSTID}_{TITLE}" --parallel 4 --media-only`: files go straight into each user's folder, named by post ID, with every file's hash kept in that folder's `hashes.sqlite`, duplicates skipped, and self posts and crossposts left out (as the script does). Options given on the command line or in `--opts` override these (in an `--opts` file, flags can be turned off, e.g. `no_dupes: false`), and `--no-defaults` uses the `download` command's defaults instead. `bdfr download` keeps its own defaults.

    It listens on `http://127.0.0.1:7635` (change with `--port`) and prints a token, which is also saved as `serve_token.txt` in BDFR's [configuration directory](#configuration). Only programs on your computer can reach it, and every request must carry the token.

2. Install the script in Tampermonkey. The first time you click a button it asks for the server address and token (change them later with **⚙**).
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

`bdfr serve` keeps the folders of the users listed in `watchlist.yaml` (next to BDFR's [configuration](#configuration), or the file given with `--watchlist`) complete from Arctic Shift, without any clicks:

```yaml
interval_days: 7    # how often each user's archive is checked
overlap_days: 7     # each check starts this much before the previous one, for posts the archive got late
retry_hours: 24     # a failed check is retried after this long
users:
  - someone                  # into the folder "someone"
  - name: someone_else
    folder: Some folder
```

A user's first check reads their whole archive. After that, each check reads every archived post since the previous check (plus the overlap) to its end, so posts missing between downloaded ones are found too; posts already in the folder cost no requests, so a quiet week is one request. Checks run one at a time with a minute between them, and the file is read again as it changes, so there's no need to restart. A check that fails (Arctic Shift unavailable, ...) is logged as a warning and its window is read again later, so nothing is skipped. `GET /watchlist` (with the token) shows each user's last successful check, last error and next check; progress is in `watchlist_state.json` next to the watchlist.

## Options

### Sources

- `directory`
    - Where downloads are saved
- `--source`
    - Where posts are read from: `arctic-shift` (default) or `reddit` (deprecated)
    - With `arctic-shift`, listings are always newest first, and nothing is requested from Reddit's API
    - Arctic Shift keeps posts that were later deleted, so their media may no longer download
    - The number of requests made to Arctic Shift is logged at the end of each run
- `-u, --user`
    - A user to read posts from, with `--submitted`; can be given more than once
- `--submitted`
    - Use the posts submitted by each `--user`
- `-s, --subreddit`
    - A subreddit to read posts from; can be given more than once or as a comma-separated list, e.g. `-s 'pics, aww'`
- `-l, --link`
    - A post to download, as a URL or ID; can be given more than once
    - With `arctic-shift`, posts are looked up 100 per request
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
    - With `arctic-shift`: only posts from the last `hour`, `day`, `week`, `month` or `year`; `all` (default) has no limit
    - With `reddit`: the time window for `top` and `controversial` sorts
- `--parallel`
    - How many posts to download at once, from 1 (default) to 16; 4 is a good start when many posts are videos (Redgifs, v.redd.it)
    - Only fetching and downloading run side by side: duplicate checks, the hash store and writing files happen one at a time, so duplicates are still caught, and results are reported (and `--stop-at-existing` counted) in listing order
    - When a listing stops, posts already started are finished, so a few more posts than with `--parallel 1` may be checked
    - Posts sharing a link (reposts, or the same post twice on a page) download it once: a post whose link another is downloading waits for it, then counts as already downloaded
- Links already downloaded: with a hash store, the media URL of every download is stored, and for posts with a single file (e.g. Redgifs, v.redd.it, Imgur images) so is the post's own link. A later post with the same link is then recognised as already downloaded without any request, not even to ask Redgifs where the video is. Existing stores gain post links as their posts come up again
- `--stop-at-existing`
    - Stops reading a listing after this many already-downloaded posts in a row, then moves on to the next one
    - A post counts as already downloaded when its files already exist at their destination or, with `--keep-hashes`/`--keep-hashes-db`, its media URLs are already stored. Posts that are filtered out, fail, or turn out to be duplicates by hash don't count either way, since a new post can repost old media
    - Listings must be newest first, which they always are with `arctic-shift`
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
    - Wait only 5 seconds and retry once after a download error, instead of waiting 60 seconds and more. Connection errors such as `[Errno 11001]` and `[WinError 10061]` fail at once
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

## Deprecated: Reddit API features

These only work through Reddit's API, which no longer grants access to personal scripts, so they are deprecated. They still exist and log a deprecation warning, and will work if your Reddit app still has API access.

- `--source reddit` and running `archive` or `clone` (which always use Reddit's API, since only Reddit has comments)
- `--authenticate`: logs in to Reddit with OAuth2. On first use BDFR shows a Reddit URL to approve, then saves the token as `user_token` in the configuration file; delete that line to log in again. It needs `client_id`, `client_secret` and `scopes` in the configuration file
- `--saved`, `--upvoted`: a user's saved or upvoted posts; need `--authenticate` and `--user me`
- `--subscribed`: the subreddits the logged in user subscribes to
- `-m, --multireddit`: a multireddit of the single `--user`
- `--search`: search subreddits or multireddits for a term
- `-S, --sort`: `hot` (default), `new`, `top`, `controversial`, `rising` or `relevance` (with `--search`). Arctic Shift listings are always newest first
- `archive`: saves post data and comments as `json` (default), `xml` or `yaml` (`-f, --format`), with `--all-comments` (a user's comments, with `--user`), `--comment-context` (the whole post a comment belongs to), `--no-comments` and `--ignore-score` (don't count score changes as updates to saved data)
- `clone`: `archive` and `download` in one run, taking the options of both
- [scripts/unsaveposts.py](scripts/unsaveposts.py): unsaves posts listed in a log file from your account

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

The console shows one line per post, and the log file (`log_output.txt`, one run per file, or `serve_log.txt` for `bdfr serve` jobs, which all go in one file that is rotated at 10 MB) also has the debug lines for each file. With `-vv` both also get fine-grained tracing, such as every Arctic Shift request URL.

```
Looking up 59 of 200 posts in Arctic Shift:
  gallery (40): 1x9xkq0, 1x9xhye, ...
  self post (15): ...
Arctic Shift request 1: posts/ids (59 IDs) -> 59 posts in 0.9s
#1 1x9y297 in r/u_someone [Redgifs]: new - 1 file written
#2 1x9xkq0 in r/pics [Gallery]: new - 4 files written
#3 1wzgo2n in r/pics [Gallery]: skipped - 2 same as already downloaded 1wzgm15_Title_1.jpg, 1wzgm15_Title_2.jpg
#4 1wzg1ab in r/pics [Direct]: already downloaded - 1 already in the folder
Finished 200 posts: 2 new (5 files written), 1 already downloaded, 1 skipped
Made 1 request to Arctic Shift (59 posts looked up: 40 gallery, 15 self post, 4 crosspost or link to another Reddit post)
```

- **Lookups**: posts from the userscript or `--post-json` that link straight to media, v.redd.it videos and galleries sent with their image list are downloaded from the page's data. Other galleries, self posts, crossposts and posts with incomplete data need the full post from Arctic Shift. These are looked up 100 per request, and the log lists them by reason.
- **Per post**: `new` means something was written, `already downloaded` means its files are already in the folder (or were renamed to the current scheme with `--rename-existing`), and `skipped` gives the reason (a filter, a failure, or the same content as an earlier download).
- **`bdfr serve`**: the console shows when each job starts and finishes. Polling by the userscript isn't logged, only failed requests.

### Configuration File

Required keys:

- `client_id`, `client_secret`: a Reddit app's credentials. Arctic Shift doesn't use them, but BDFR still needs them to build its post objects; the defaults work
- `scopes`: the OAuth2 permissions requested by the deprecated `--authenticate`

Optional keys:

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

When a site rate-limits a download, BDFR waits 60 seconds and retries, then 120 seconds, and so on up to `--max-wait-time` (or `max_wait_time`, default 120). With the default that is 180 seconds of waiting for one download; larger values make waits grow quickly. `--fail-fast` shortens this to one retry after 5 seconds.

## Multiple Instances

Several runs can be scripted one after another, for example with different `--config` files or sources. Runs at the same time must each use their own `--log` file, as Windows doesn't allow two processes to write the same log, and must not share a `directory` when using `--keep-hashes`.

## Filesystem Restrictions

BDFR follows the naming rules of the system it runs on. When saving to a drive with stricter rules, such as a Windows (SMB) share from Linux, set `--filename-restriction-scheme windows` or `filename_restriction_scheme` in the configuration file.

## Manipulating Logfiles

The [scripts](scripts) folder has Bash and PowerShell scripts that read BDFR logs, e.g. to list the IDs of successful or failed downloads.

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
    - Any site supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)

## Contributing

See [AGENTS.md](AGENTS.md) for how the code is organised, how to run the tests, and the fork's conventions. Upstream's [contributing guide](docs/CONTRIBUTING.md) and [Code of Conduct](docs/CODE_OF_CONDUCT.md) still apply.
