# Architecture

How a request becomes files on disk. For the module list, test commands and the invariants that keep existing folders from being downloaded again, see [AGENTS.md](../AGENTS.md).

## Where posts come from

Reddit's API is closed to personal scripts, so BDFR never talks to it. Posts arrive in three ways:

- **The userscript** ([scripts/tampermonkey/bdfr-export.user.js](../scripts/tampermonkey/bdfr-export.user.js)) reads posts from old.reddit.com in the browser, using the browser's login, and sends them to `bdfr serve` as a job. It reads galleries' image lists from Reddit itself, so those need no lookup.
- **The watchlist scheduler** (`bdfr/scheduler.py`) queues an Arctic Shift job for each watched user once a week, covering the time since that user's last successful check.
- **The command line** (`bdfr download`): subreddit and user listings from Arctic Shift, post IDs and links, and `--post-json` files exported by the userscript.

Every post becomes a `Post` (`bdfr/post.py`): the post's JSON fields as attributes, with `author` and `subreddit` as plain names. Nothing is ever fetched for a missing field. Reading one raises `AttributeError`, so code on the download path only reads fields that Arctic Shift and the userscript provide.

## The server

`bdfr serve` (`bdfr/server.py`) is a small HTTP server that needs a token on every request:

1. `POST /jobs` validates the job (one folder directly under the served directory, at most 5000 posts) and queues it. `GET /jobs/<id>` reports per-post progress, `GET /status` and `GET /watchlist` report the queue and the watched users, and `POST /known` says which posts a folder already has (from post IDs in file names and the folder's hash store), so the userscript can mark them without downloading anything.
2. One worker thread runs jobs one at a time. Each job copies serve's base options, points them at the job's folder, writes the given posts to a temporary `--post-json` file and, for a user job, adds that user's Arctic Shift listing (`listing_before`, `listing_after`).
3. It runs a `Downloader` with a `progress_callback`. A job ends `failed` when the downloader recorded errors, for example a listing cut short by Arctic Shift, so callers never mistake an unread listing for "nothing new".

## The download

`Downloader` (`bdfr/downloader.py`) builds on `Connector` (`bdfr/connector.py`), which builds on `HashStore` (`bdfr/hash_store.py`):

1. `Connector` loads the configuration file, sets up logging, opens the folder's hash store and builds `self.listings`. These are lazy generators of posts: given posts first, then each subreddit and user listing. `ArcticShift` (`bdfr/arctic_shift.py`) fetches the next page only when the previous one is used up, and looks up posts by ID 100 at a time, only when the given data isn't enough to download from.
2. `Downloader.download()` takes each post from the listings, one at a time or in a thread pool with `--parallel`, and reports results in listing order. `--stop-at-existing` ends a listing after enough already-downloaded posts in a row, and since the listings are lazy, no further pages are requested.
3. `_download_submission()` filters the post (IDs, users, scores, `--media-only`). Before any request, it checks whether the post is already known, by the post ID in a file name or by its link in the hash store's URL table.
4. `DownloadFactory.pull_lever()` (`bdfr/site_downloaders/download_factory.py`) picks a site downloader by URL. Its `find_resources()` returns `Resource`s (`bdfr/resource.py`), each with the media URL and a download function.
5. `FileNameFormatter` (`bdfr/file_name_formatter.py`) gives each resource its path. The media URL is claimed, so that a link shared by several posts downloads once per run, and then checked against the store.
6. The resource is downloaded and hashed. Under one lock, the hash is checked for duplicates (skip or hard-link), and then the file, its hash and its URL are recorded.

The hash store keeps content hash → file, file → hash and media URL → hash, either as SQLite (`hashes.sqlite`, the server's default) or as three JSON files. Both modes must behave the same.

## `bdfr rename`

`bdfr/renamer.py` renames existing files from one file scheme to another using only what is on disk. It parses the old names with a regex built from the old scheme, takes `{DATE}` from modified times, and updates the hash store to match. It makes no network requests.

## Adding a supported site

1. Write a class in `bdfr/site_downloaders/` that inherits from `BaseDownloader`, or from a close relative. `VReddit`, for example, reuses `Youtube`'s yt-dlp handling. It is constructed as `Cls(post, args)`, and `find_resources()` returns the post's `Resource`s.
2. Keep it thread-safe: with `--parallel`, `find_resources()` and `Resource.download()` run in threads, so shared state (such as `Redgifs`'s token) goes behind a lock.
3. Add its URL pattern to `DownloadFactory.pull_lever()`, and add tests for both the downloader and the factory. Tests that reach the site are marked `online`, and CI doesn't run them. Mock the HTTP calls for anything that must stay tested.
4. Keep `Resource.url` stable once a site is supported, because existing downloads are recognised by their stored media URL.
