# AGENTS.md

A personal fork of BDFR (Bulk Downloader for Reddit) for keeping folders of Reddit media up to date. Reddit's API has been closed to personal scripts since Nov 2025, so posts come from the Arctic Shift archive by default. Anything that needs Reddit's API (`--source reddit`, `archive`, `clone`, `--authenticate`, `--saved`, ...) is deprecated: keep it working, but don't build on it. User docs are in README.md; read it only for behaviour questions.

## Commands

```bash
python -m pip install -e . pytest flake8                     # once; then use `python -m bdfr ...` or `bdfr ...`
python -m pytest tests -q -m "not online and not reddit and not authenticated and not slow" --ignore=tests/integration_tests
python -m flake8 bdfr tests --select=F,E9                    # unused imports, undefined names, syntax
```

- Only offline tests are expected to pass (this is what CI runs). Online tests hit Reddit (403) or third-party hosts; don't run them in bulk.
- Never run `bdfr/__main__.py` as a script: it imports a PyPI `bdfr` if one is installed.
- Manual runs: pass `--config <copy of bdfr/default_config.cfg>`, download into a scratch directory, use small `-L`, and space out requests to Arctic Shift.

## Layout

- `bdfr/__main__.py`: click CLI. Options are lists of decorators (`_common_options`, `_downloader_options`, `_archiver_options`); a new option also needs a default in `configuration.py`.
- `bdfr/configuration.py`: `Configuration`, the defaults for every option (`args` everywhere).
- `bdfr/connector.py`: `RedditConnector` base. Loads config, builds `self.reddit_lists` (lazy generators of submissions per source), and owns the hash store (JSON files or SQLite) and the existing-file scan.
- `bdfr/arctic_shift.py`: Arctic Shift client, `--post-json` reader, link-to-ID parsing.
- `bdfr/downloader.py`: `RedditDownloader.download()` loops over listings; `_download_submission()` filters, picks a site downloader, downloads, then de-duplicates by URL and hash.
- `bdfr/site_downloaders/`: one class per host, chosen by URL regex in `download_factory.py`. `find_resources()` returns `Resource`s (`bdfr/resource.py` does the HTTP download and hashing).
- `bdfr/file_name_formatter.py`: file and folder names from schemes.
- `bdfr/renamer.py`: `bdfr rename`, offline renaming between file schemes: parses old names with a regex built from the old scheme, takes `{DATE}` from modified times, formats new names with `FileNameFormatter`, and updates the hash store; `--build-hash-db` creates/completes `hashes.sqlite` from file contents (no URLs). It must stay network-free (tests block sockets).
- `bdfr/server.py`: `bdfr serve`, a 127.0.0.1-only HTTP server (token in `X-BDFR-Token`) that queues jobs from the userscript and runs them one at a time with `RedditDownloader`, reporting per-post results via `progress_callback`, and answers `/known` (which post IDs already have files in a folder, read from file names containing `{POSTID}`, plus posts whose link (`links`: {ID: URL}, normalised with `downloader.post_link`) is in the folder's hash store, read with a separate read-only SQLite connection). `serve` applies `SERVE_DEFAULTS` from `__main__.py` before `--opts` and command line options; `--no-defaults` skips them. Jobs only write to one folder directly under the served directory.
- Deprecated, Reddit API only: `archiver.py`, `cloner.py`, `archive_entry/`, `oauth2.py`.
- `scripts/`: log parsing scripts (with bats tests in `scripts/tests`), `tampermonkey/bdfr-export.user.js` (old.reddit panel on user pages, configurable in `SETTINGS`: Download (plus the user's Arctic Shift listing when the page is empty or its oldest post isn't downloaded, or always and all of it with the Archive checkbox) via `bdfr serve`, Collect + Export for `--post-json`; check with `node --check`).
- `tests/`: pytest; `test_arctic_shift.py` (mocked HTTP session) and `test_server.py` (fake downloader) cover the fork's additions.

## Invariants (breaking these re-downloads or loses users' data)

- Existing downloads are recognised by file path and by stored media URL. Keep `Resource.url`, file names and gallery URL probing (`.jpg` first) unchanged; e.g. `VReddit` downloads from the HLS URL but keeps `post.url` as `Resource.url`.
- Highest quality: v.redd.it uses the unsigned `https://v.redd.it/<id>/HLSPlaylist.m3u8` (same variants as the signed `hls_url`, which expires); yt-dlp downloaders use `bestvideo*+bestaudio/best` (needs ffmpeg); galleries download originals from i.redd.it.
- The userscript sends galleries with `gallery_data` read from Reddit in the browser (`/by_id/*.json`), so `ArcticShift.needs_lookup` skips them; it doesn't send self posts, crossposts or incomplete posts. A Download that reads the archive sends `user` (and `archive: "all"` for the whole history) with the page's `posts`, and `before`: the oldest page post's time, so the listing (`args.listing_before`, Arctic Shift's exclusive `before`) starts below the page; given posts (`self.post_list`) come first, are exempt from `--stop-at-existing`, and listings skip IDs already handled in the run.
- Posts are recognised before any request by `_post_id_known` (their ID in the folder's file names, or a `post:<id>` key in the URL table for posts whose media was all duplicates) and `_post_link_known`; both are skipped with `--rename-existing`. `serve`'s `/known` answers from the same three sources. `--media-only` (a serve default) skips self posts and crossposts.
- For posts with a single file, the post's own link (`_post_link`, one form per Redgifs video) is also stored in the URL table with the media's hash (`_remember_post_link`), and checked before any request (`_post_link_known`, skipped with `--rename-existing`). Never store it for multi-file posts.
- When a destination file already exists, `_record_url_of_existing_file` stores its media URL from the file's stored hash, so offline-built stores gain URLs over time.
- `--rename-existing` (`RedditDownloader._rename_existing`) renames a stored file only when its name contains the submission ID, then updates the store with `_move_hashed_file`. Anything reading post IDs from file names (`server.known_posts`) must accept the ID anywhere in the name, since old and new schemes share folders.
- Arctic Shift posts are real `praw.models.Submission` objects with `_fetched = True`: a missing attribute raises `AttributeError` rather than fetching from Reddit. Code on the download path must only read fields present in post data (not `subreddit.title`, `comments`, ...).
- Keep requests to Arctic Shift minimal: listings are generators that fetch the next page only when the previous one is used up, so breaking out of the loop stops requests. No per-post requests; batch ID lookups (`/posts/ids`, 100 per request).
- `_download_submission()` returns `True` (already downloaded), `False` (wrote something new) or `None` (filtered, failed, hash duplicate); `--stop-at-existing` counts on this.
- The hash store has two modes that must behave the same: JSON (`master_*_list` dicts, saved by `_save_hash_list`) and SQLite (`hashes`, `hashes_file`, `hashes_url` tables; paths stored `os.fsencode`d). Change both, via `_check_hash_exists_or_add` / `_check_url_exists_or_add`.
- Site downloaders are constructed as `Cls(post, args)`. With `--parallel`, `find_resources()` and `Resource.download()` run in threads, so site downloaders must be thread-safe (shared state behind a lock, like `Redgifs._token`).
- `--parallel`: `RedditDownloader.download()` submits `_run_one` to a thread pool and reports results in listing order. Everything touching the hash store or the download folder runs under `RedditDownloader._store_lock` (`_check_before_download`, and everything after `res.download()`); the SQLite connection is opened with `check_same_thread=False` for this. Per-submission outcome state is thread-local (`_files`, `_skip_reason`, ... are properties). A resource URL is claimed with `_claim` before downloading and released with `_release`; others wanting the same URL wait, so a link is downloaded once per run (`_run_urls` covers runs without a hash store).
- Logging: each submission gets one INFO line from `RedditDownloader._log_outcome` (set `self._skip_reason` via `self._skip(...)` on early returns, count files in `self._files`); per-file lines are DEBUG. The scripts in `scripts/` grep the per-file and filter debug lines ("Downloaded submission", "Resource hash", "already exists, continuing", "Hard link made", "filtered due to score", ...), so keep their wording and word positions.
- Never write to the packaged `bdfr/default_config.cfg`; only OAuth writes a config file, the one in use.
- Python 3.9 compatible (CI runs 3.9 and 3.12).

## Style

Match the surrounding code. Line length 120; black/isort settings are in `pyproject.toml`, but the code was never fully formatted, so don't reformat lines you aren't changing.

## Known issues (not yet fixed)

- Hash store: JSON mode mixes `str` and `Path` values; converting JSON to SQLite isn't atomic (a failure leaves a partial DB that blocks the next conversion); SQLite commits once per row.
- `cloner.py` skips writing archive entries when the download directory contains `Reddit_imgur` (a personal hack from the original fork).
- `archiver.py` and `cloner.py` log `submission.id` in their listing-level `except`, which is unbound if the first item fails.
- `docs/` is upstream's documentation and partly out of date.
- Files shortened before `format_path` protected the post ID at the end of a name (any length) may lack their ID; they keep that name (it is reused when it exists), so `/known` only recognises them by link.
