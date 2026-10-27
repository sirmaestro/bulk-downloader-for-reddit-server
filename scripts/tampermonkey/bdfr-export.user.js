// ==UserScript==
// @name         BDFR downloader
// @namespace    bdfr-saved-hashes
// @version      4.16
// @description  Downloads the posts on old.reddit pages (also www.reddit.com in the old design) through `bdfr serve`, keeps watched users synced in the background, or collects posts as JSON for `bdfr download --post-json`
// @match        https://old.reddit.com/*
// @match        https://www.reddit.com/*
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @connect      old.reddit.com
// bdfr serve on another machine (e.g. Docker on a home server): Tampermonkey asks once to allow its address
// @connect      *
// @run-at       document-idle
// @updateURL    https://github.com/sirmaestro/bulk-downloader-for-reddit-server/releases/latest/download/bdfr-export.user.js
// @downloadURL  https://github.com/sirmaestro/bulk-downloader-for-reddit-server/releases/latest/download/bdfr-export.user.js
// ==/UserScript==

(function () {
    "use strict";

    // Which pages the panel appears on (old.reddit.com, or www.reddit.com in the old design). On other pages, and on
    // www.reddit.com in the new design, there is no panel; the link rewriting and background sync still run.
    const SETTINGS = {
        userPages: true, // old.reddit.com/user/<name>/...
        subredditPages: false, // old.reddit.com/r/<name>/... and multireddits (old.reddit.com/user/<name>/m/<multi>)
        // Links to a user's profile open their submitted posts, newest first, on every old.reddit page
        userLinksToSubmitted: true, // on www.reddit.com too, in either design
        // On any old.reddit page, watched users (bdfr serve's watchlist.yaml) not synced for this many hours have their
        // newest posts read from Reddit with your login and sent to bdfr serve, in the background
        backgroundSync: true,
        syncEveryHours: 6,
    };

    if (SETTINGS.userLinksToSubmitted) redirectUserLinks();

    // Rewrites links to a user's profile (old.reddit.com/user/<name>, /u/<name>, www.reddit.com/user/<name>) to
    // old.reddit.com/user/<name>/submitted/?sort=new. Changing the link itself, rather than catching clicks, makes
    // middle-click, Ctrl+click and "Open in new tab" go there too. Links to other user pages (comments, saved, ...)
    // are left alone.
    function redirectUserLinks() {
        const profile = /^\/u(?:ser)?\/([\w-]+)\/?$/i;
        const rewrite = (link) => {
            let url;
            try {
                url = new URL(link.getAttribute("href"), location.href);
            } catch (e) {
                return;
            }
            if (!/^((old|www|np)\.)?reddit\.com$/i.test(url.hostname) || url.search || url.hash) return;
            const match = url.pathname.match(profile);
            if (match) link.href = `https://old.reddit.com/user/${match[1]}/submitted/?sort=new`;
        };
        const rewriteAll = (root) => {
            for (const link of root.querySelectorAll("a[href*='/u']")) rewrite(link);
        };
        rewriteAll(document);
        // Links added later (Never Ending Reddit, RES hover cards) are rewritten as they appear, and any link is
        // checked again as a mouse button goes down on it, before the browser opens it
        let timer = null;
        new MutationObserver(() => {
            clearTimeout(timer);
            timer = setTimeout(() => rewriteAll(document), 300);
        }).observe(document.body, { childList: true, subtree: true });
        // (composedPath: in the new design a link can be inside a component's shadow root, which the event's target
        // hides)
        document.addEventListener("mousedown", (event) => {
            const link = event.composedPath().find((node) => node instanceof Element && node.matches("a[href]"));
            if (link) rewrite(link);
        }, true);
    }

    const COLOURS = { background: "#262626", raised: "#373737", text: "#9a9a9a", textStrong: "#d0d0d0" };
    const MARKS = {
        new: ["✔", "#8bc34a", "Downloaded"],
        existing: ["✔", "#8a8a8a", "Already downloaded"],
        duplicate: ["≡", "#8a8a8a", "Same media as something already downloaded (re-uploaded under another link)"],
        skipped: ["!", "#ffb74d", "Skipped: filtered out or nothing to download (the reason is in serve_log.txt)"],
        failed: ["✖", "#e57373", "Failed (the reason is in serve_log.txt); Download tries it again"],
        unavailable: ["⊘", "#8a8a8a", "Can't be downloaded: the media is gone (deleted or removed) or no downloader "
            + "handles the link (the reason is in serve_log.txt); not sent again"],
        fetching: ["⋯", "#8a8a8a", "Reading the gallery's images from Reddit"],
        ignored: ["–", "#6a6a6a", "Ignored"],
    };
    // Posts per Reddit by_id request (Reddit's maximum)
    const BY_ID_BATCH = 100;
    const STORE_KEY = "bdfr-posts";
    const COLLECTING_KEY = "bdfr-collecting";
    const SERVER_KEY = "bdfr-server";
    const TOKEN_KEY = "bdfr-token";
    const EXTRAS_KEY = "bdfr-extras-open";
    const DEFAULT_SERVER = "http://127.0.0.1:7635";
    // How often the panel asks bdfr serve what it's doing (GET /jobs), while the tab is visible
    const ACTIVITY_EVERY_MS = 10 * 1000;
    // After bdfr serve didn't answer (each failure is logged, so not every 10 seconds)
    const ACTIVITY_RETRY_MS = 5 * 60 * 1000;

    // --- Background sync of watched users (on every old.reddit page) ---
    //
    // The users on bdfr serve's watchlist (GET /watchlist) that haven't been synced for SETTINGS.syncEveryHours have
    // their newest posts read from Reddit with your login (submitted.json, 100 per request), until 5 posts in a row
    // are already downloaded, and the new media posts are sent to bdfr serve as one job. Reddit's listing has the
    // gallery data, so nothing is read from the archive (bdfr serve's weekly baseline fills what Reddit no longer
    // shows). One tab syncs at a time. Every failure is logged to the console as a warning starting "BDFR sync:", and
    // a user that failed is tried again after an hour. A suspended or deleted account (its about.json says so after
    // its posts answer 403 or 404) is reported to bdfr serve (POST /watchlist/gone). While its newest post is under
    // the watchlist's gone_active_days old, bdfr serve still lists it, marked gone, and it's synced as usual (a
    // temporary suspension or shadowban can be lifted); after that it's left out, and listed once more every
    // gone_recheck_days for another look (an older bdfr serve without that: checked again weekly). Hibernating users (no post for the watchlist's
    // hibernate_days, by the server's or this script's newest post) are synced weekly: bdfr serve reads no weekly
    // archive windows for them, so this free check of Reddit is what notices them posting again. Aliases (old accounts) are
    // never synced. "Sync now" (the panel's Sync button, or Tampermonkey's menu on any page) syncs every watched user
    // at once, however recently they were synced. A Sync now that didn't get through every user (stopped with the Sync
    // button, the tab closed, bdfr serve down) is carried on by the next one, skipping the users it already synced.
    // Reddit's rate limit (its x-ratelimit-* headers) is followed: the sync waits for the limit to reset rather than
    // run into it, and waits and retries when Reddit answers 429 anyway. Waits use a worker's timer, which browsers
    // don't slow down in background tabs as they do the page's.
    // user (lower case) -> { lastSuccess, lastSent, newestPost }, { lastError, lastAttempt } or { gone, lastAttempt }
    const SYNC_STATE_KEY = "bdfr-sync";
    // { owner: tab, until: time, page: its address, status: its progress } while a tab syncs
    const SYNC_LOCK_KEY = "bdfr-sync-lock";
    const SYNC_TITLE_MARK = "⟳ "; // starts the syncing tab's title while it syncs a user, so it can be found
    const SYNC_GAP_KEY = "bdfr-sync-request-gap"; // milliseconds between requests to Reddit
    const SYNC_RUN_KEY = "bdfr-sync-run"; // { started } while a Sync now hasn't got through every user
    const SYNC_RUN_RESUME_MS = 24 * 3600 * 1000; // an unfinished Sync now older than this starts again from the top
    const SYNC_RATE_RESERVE = 3; // requests left in Reddit's rate limit window at which the sync waits for its reset
    const SYNC_RATE_LIMITED_RETRIES = 3; // 429 answers in a row before a user counts as failed
    const SYNC_RATE_LIMITED_WAIT_MS = 60 * 1000; // after a 429 without a reset time
    const SYNC_LOCK_MS = 2 * 60 * 1000;
    const SYNC_RETRY_MS = 60 * 60 * 1000;
    const SYNC_GONE_RETRY_MS = 7 * 24 * 3600 * 1000;
    const SYNC_HIBERNATING_MS = 7 * 24 * 3600 * 1000;
    const SYNC_START_DELAY_MS = 1500;
    const SYNC_STOP_AFTER_KNOWN = 5;
    const SYNC_PAGE_SIZE = 100; // posts per request to Reddit, its most
    const REDDIT_LISTING_MAX = 1000; // Reddit only lists a user's newest 1000 posts
    const SYNC_MAX_PAGES = Math.ceil(REDDIT_LISTING_MAX / SYNC_PAGE_SIZE);
    const syncTab = Math.random().toString(36).slice(2);
    let lastRedditRead = 0;
    let redditPausedUntil = 0; // Reddit's rate limit window is (nearly) used up until then
    let syncing = false; // this tab is syncing (the lock alone doesn't stop the same tab starting twice)
    let stopSync = false; // the Sync button was pressed again: stop after the current user
    let syncStatus = ""; // the running sync's latest report, kept in the lock for other tabs
    let syncProgress = () => {}; // the running sync's report, for waits on Reddit's rate limit
    // Where a sync started with "Sync now" reports its progress: the panel's status line on pages with the panel
    let reportSync = (text) => console.info(`BDFR sync: ${text}`);

    if (SETTINGS.backgroundSync) setTimeout(backgroundSync, SYNC_START_DELAY_MS);
    if (typeof GM_registerMenuCommand === "function") GM_registerMenuCommand("Sync watched users now", syncNow);

    // Starts a sync of every watched user, or carries on an unfinished one. While this tab syncs, stops it after the
    // current user instead.
    function syncNow() {
        if (!GM_getValue(TOKEN_KEY, "")) return reportSync("Set bdfr serve's address and token first (⚙)");
        if (syncing) {
            stopSync = true;
            return reportSync("Stopping after this user… (Sync again later carries on from there)");
        }
        backgroundSync({ force: true, report: (text) => reportSync(text) });
    }

    // With force, every watched user is synced, however recently they were synced or failed, except those an
    // unfinished earlier forced sync already synced. report gets progress and a summary.
    async function backgroundSync({ force = false, report = () => {} } = {}) {
        if (!GM_getValue(TOKEN_KEY, "")) return;
        if (syncing) return report("This tab is already syncing");
        if (!takeSyncLock()) return report(otherSyncTab());
        syncing = true;
        stopSync = false;
        const show = report;
        // Every report also goes into the lock (renewing it), where other tabs' Sync buttons read it
        report = (text) => {
            syncStatus = text;
            takeSyncLock();
            show(text);
        };
        syncProgress = report;
        try {
            let watchlist;
            try {
                watchlist = await request("GET", "/watchlist");
            } catch (error) {
                console.warn(`BDFR sync: couldn't read the watchlist: ${describeError(error)}`);
                return report(`Couldn't read the watchlist: ${describeError(error)}`);
            }
            const syncState = GM_getValue(SYNC_STATE_KEY, {});
            const users = (Array.isArray(watchlist.users) ? watchlist.users : []).filter((user) => !user.alias_of);
            const hibernateMs = (watchlist.hibernate_days || 60) * 24 * 3600 * 1000;
            // bdfr serve keeps track of suspended and deleted users (POST /watchlist/gone) and leaves them out
            const tracksGone = Array.isArray(watchlist.gone);
            const hidden = tracksGone ? watchlist.gone.length : 0;
            // A forced sync carries on an unfinished one: the users it (or a background sync since) got through are
            // skipped. Failed users are tried again.
            let run = force ? GM_getValue(SYNC_RUN_KEY, null) : null;
            if (force && !(run && Date.now() - run.started < SYNC_RUN_RESUME_MS)) {
                run = { started: Date.now() };
                GM_setValue(SYNC_RUN_KEY, run);
            }
            const doneInRun = (last) => Boolean(run) && ((last.lastSuccess >= run.started && !last.lastError)
                || (last.gone && last.lastAttempt >= run.started));
            const carriedOver = users.filter((user) => doneInRun(syncState[String(user.name).toLowerCase()] || {}))
                .length;
            let synced = 0;
            let sentTotal = 0;
            let failed = 0;
            let gone = 0;
            for (const [index, user] of users.entries()) {
                const key = String(user.name).toLowerCase();
                const last = syncState[key] || {};
                if (tracksGone && last.gone && !last.reported && !user.gone) {
                    // Found gone before bdfr serve kept track of it: told once, without asking Reddit again
                    await reportGone(user.name, last.gone);
                    syncState[key] = { ...last, reported: true };
                    GM_setValue(SYNC_STATE_KEY, syncState);
                    continue;
                }
                if (force ? doneInRun(last) : !syncDue(last, user, hibernateMs, Date.now())) continue;
                if (stopSync) {
                    return report(`Sync stopped: ${carriedOver + synced + gone} of ${users.length} users done · `
                        + `${sentTotal} new posts sent · Sync carries on from u/${user.name}`);
                }
                markSyncTab(true);
                report(`Syncing u/${user.name} (${index + 1}/${users.length})…`);
                let serverDown = null;
                try {
                    const { sent, newest } = await syncUser(user);
                    syncState[key] = { lastSuccess: Date.now(), lastSent: sent, newestPost: newest || last.newestPost };
                    synced += 1;
                    sentTotal += sent;
                    if (sent) console.info(`BDFR sync: u/${user.name}: sent ${sent} new posts to bdfr serve`);
                    if (user.gone) await reportGone(user.name, null); // listed again for a recheck: there after all
                } catch (error) {
                    const reason = [403, 404].includes(error.status) ? await accountGone(user.name) : null;
                    if (reason) {
                        gone += 1;
                        console.warn(`BDFR sync: u/${user.name} is ${reason} on Reddit; not synced until bdfr serve `
                            + "lists it again");
                        const reported = tracksGone && await reportGone(user.name, reason);
                        syncState[key] = { gone: reason, lastAttempt: Date.now(), newestPost: last.newestPost, reported };
                    } else {
                        syncState[key] = { ...last, lastError: error.message, lastAttempt: Date.now() };
                        failed += 1;
                        console.warn(`BDFR sync: u/${user.name} failed: ${describeError(error)}`, error.debug || "");
                        if (["unreachable", "unauthorised"].includes(error.message)) serverDown = error;
                    }
                }
                GM_setValue(SYNC_STATE_KEY, syncState);
                // Without the server, reading the other users from Reddit would be wasted
                if (serverDown) return report(`Sync stopped: ${describeError(serverDown)}`);
            }
            if (run) GM_setValue(SYNC_RUN_KEY, null);
            report(`Synced ${synced} watched users · ${sentTotal} new posts sent`
                + (gone ? ` · ${gone} suspended or deleted` : "")
                + (failed ? ` · ${failed} failed (console, F12)` : "")
                + (carriedOver ? ` · ${carriedOver} already synced by the unfinished sync` : "")
                + (hidden ? ` · ${hidden} suspended or deleted not synced` : ""));
        } finally {
            syncing = false;
            stopSync = false;
            syncProgress = () => {};
            syncStatus = "";
            markSyncTab(false);
            releaseSyncLock();
        }
    }

    // Waits on a worker's timer, which background tabs don't slow down (Chrome lets a hidden page's own timers run
    // once a minute), falling back to the page's timer where a worker can't be started. Long waits renew the sync lock.
    let timerWorker;
    let timerWaits = 0;
    const timerCallbacks = new Map();
    async function sleep(ms) {
        for (let end = Date.now() + ms, left = ms; left > 0; left = end - Date.now()) {
            await new Promise((resolve) => workerTimeout(resolve, Math.min(left, SYNC_LOCK_MS / 4)));
            if (syncing) takeSyncLock();
        }
    }

    function workerTimeout(callback, ms) {
        if (timerWorker === undefined) {
            try {
                const code = "onmessage = (e) => setTimeout(() => postMessage(e.data.id), e.data.ms);";
                timerWorker = new Worker(URL.createObjectURL(new Blob([code], { type: "text/javascript" })));
                timerWorker.onmessage = (e) => {
                    const done = timerCallbacks.get(e.data);
                    timerCallbacks.delete(e.data);
                    if (done) done();
                };
                // e.g. the page's content security policy refuses the worker: the waits use the page's timers
                timerWorker.onerror = () => {
                    timerWorker = null;
                    for (const done of timerCallbacks.values()) setTimeout(done, 0);
                    timerCallbacks.clear();
                };
            } catch (e) {
                timerWorker = null;
            }
        }
        if (!timerWorker) return setTimeout(callback, ms);
        const id = ++timerWaits;
        timerCallbacks.set(id, callback);
        timerWorker.postMessage({ id, ms });
    }

    // Tells bdfr serve a watched user is suspended or deleted on Reddit (reason), or there after all (null). A
    // failure is logged by request() and otherwise ignored: the user is then checked again weekly, as before.
    // Returns whether bdfr serve took it.
    async function reportGone(name, reason) {
        try {
            await request("POST", "/watchlist/gone", { user: name, gone: reason });
            return true;
        } catch (error) {
            return false; // logged
        }
    }

    // Whether a user is due for the background sync: a failed one after an hour, a suspended or deleted one after a
    // week, a hibernating one (newest post older than hibernateMs) after a week, others after syncEveryHours
    function syncDue(last, user, hibernateMs, now) {
        // Listed by bdfr serve, marked gone: they posted recently (maybe a temporary suspension or a shadowban), or
        // are due for their recheck, so they're synced as usual. Otherwise (an older bdfr serve) weekly.
        if (last.gone) {
            const wait = user.gone ? SETTINGS.syncEveryHours * 3600 * 1000 : SYNC_GONE_RETRY_MS;
            return now - (last.lastAttempt || 0) >= wait;
        }
        if (last.lastError) return now - (last.lastAttempt || 0) >= SYNC_RETRY_MS;
        if (!last.lastSuccess) return true;
        const newest = Math.max(last.newestPost || 0, user.newest_post || 0);
        const hibernating = newest > 0 && now - newest * 1000 > hibernateMs;
        return now - last.lastSuccess >= (hibernating ? SYNC_HIBERNATING_MS : SETTINGS.syncEveryHours * 3600 * 1000);
    }

    // Whether this tab may sync: no other tab holds the lock, or it has expired. Taking it again renews it.
    function takeSyncLock() {
        const lock = GM_getValue(SYNC_LOCK_KEY, null);
        if (lock && lock.owner !== syncTab && lock.until > Date.now()) return false;
        GM_setValue(SYNC_LOCK_KEY, {
            owner: syncTab, until: Date.now() + SYNC_LOCK_MS, page: location.href, status: syncStatus,
        });
        return true;
    }

    function releaseSyncLock() {
        const lock = GM_getValue(SYNC_LOCK_KEY, null);
        if (lock && lock.owner === syncTab) GM_setValue(SYNC_LOCK_KEY, null);
    }

    // Which tab holds the sync lock, and what it's doing
    function otherSyncTab() {
        const lock = GM_getValue(SYNC_LOCK_KEY, null) || {};
        if (!lock.page) return "Another tab is already syncing"; // one with an older version of this script
        const url = new URL(lock.page);
        return `Already syncing in the tab on ${url.host}${url.pathname} (its title starts with ${SYNC_TITLE_MARK.trim()})`
            + (lock.status ? `: ${lock.status}` : "");
    }

    function markSyncTab(on) {
        const marked = document.title.startsWith(SYNC_TITLE_MARK);
        if (on && !marked) document.title = SYNC_TITLE_MARK + document.title;
        if (!on && marked) document.title = document.title.slice(SYNC_TITLE_MARK.length);
    }

    // Reads the user's newest posts until SYNC_STOP_AFTER_KNOWN downloaded ones in a row, and sends the new ones.
    // When bdfr serve asks for it (read_reddit_to_end: the user's whole archive is still to be read), reads the
    // listing to its end instead (Reddit lists the newest 1000 posts) and then tells it so, so the archive is only read
    // for what Reddit can't show. Returns how many were sent, and when the user's newest post (of any kind) was made.
    async function syncUser(user) {
        const target = user.folder || user.name;
        const toEnd = Boolean(user.read_reddit_to_end);
        const pending = [];
        let after = null;
        let knownInARow = 0;
        let newest = null;
        let oldest = null;
        let listed = 0;
        let reachedEnd = false;
        for (let pageNumber = 0; pageNumber < SYNC_MAX_PAGES; pageNumber++) {
            const listing = await readSubmitted(user.name, after);
            if (pageNumber === 0 && listing.children.length) {
                newest = (listing.children[0].data || {}).created_utc || null;
            }
            listed += listing.children.length;
            for (const child of listing.children) {
                const created = (child && child.data || {}).created_utc;
                if (Number.isFinite(created) && (oldest === null || created < oldest)) oldest = created;
            }
            const posts = listing.children.map((child) => postFromListing(child && child.data)).filter(Boolean);
            let stop = false;
            if (posts.length) {
                const ids = posts.map((post) => post.id);
                const links = Object.fromEntries(posts.filter((post) => !isGallery(post))
                    .map((post) => [post.id, post.url]));
                const known = await request("POST", "/known", { folder: target, ids, links });
                const had = new Set([...(known.known || []), ...(known.duplicates || [])]);
                // Posts that can't be downloaded aren't sent, and don't count towards the known ones in a row
                const unavailable = new Set(known.unavailable || []);
                for (const post of posts) {
                    if (unavailable.has(post.id)) continue;
                    if (!had.has(post.id)) {
                        knownInARow = 0;
                        pending.push(post);
                    } else if (++knownInARow >= SYNC_STOP_AFTER_KNOWN && !toEnd) {
                        stop = true;
                        break;
                    }
                }
            }
            if (stop) break;
            // The end: no next page, or as many posts as Reddit lists. Running out of pages before either isn't, so
            // bdfr serve isn't told the listing was read when it wasn't.
            if (!listing.after || listed >= REDDIT_LISTING_MAX) {
                reachedEnd = true;
                break;
            }
            after = listing.after;
        }
        if (pending.length) await request("POST", "/jobs", { folder: target, posts: pending });
        if (toEnd && reachedEnd) {
            await request("POST", "/watchlist/reddit", { user: user.name, oldest });
            console.info(`BDFR sync: u/${user.name}: Reddit read to its end; bdfr serve reads the rest from the archive`);
        }
        return { sent: pending.length, newest };
    }

    // A post from Reddit's listing JSON in the form the page's posts are sent in, or null if it isn't downloaded
    // (self posts, crossposts, links to other Reddit posts, incomplete data)
    function postFromListing(data) {
        if (!data || data.is_self || data.crosspost_parent) return null;
        const post = {
            id: data.id,
            title: data.title,
            author: data.author,
            subreddit: data.subreddit,
            created_utc: data.created_utc,
            url: absoluteUrl(data.url_overridden_by_dest || data.url),
            domain: data.domain || null,
            permalink: data.permalink || null,
            score: Number.isFinite(data.score) ? data.score : null,
            over_18: Boolean(data.over_18),
            link_flair_text: data.link_flair_text || null,
            is_gallery: Boolean(data.is_gallery),
        };
        if (data.upvote_ratio != null) post.upvote_ratio = data.upvote_ratio;
        // Why a removed gallery has no image list, for bdfr serve's message when it isn't looked up in the archive
        if (data.removed_by_category) post.removed_by_category = data.removed_by_category;
        if (data.gallery_data) {
            post.gallery_data = data.gallery_data;
            post.media_metadata = trimMediaMetadata(data.media_metadata);
        }
        return ignoreReason(post) ? null : post;
    }

    // One page of the user's submitted posts, newest first, read with the browser's Reddit login
    async function readSubmitted(name, after) {
        const url = `https://old.reddit.com/user/${encodeURIComponent(name)}/submitted.json`
            + `?sort=new&limit=${SYNC_PAGE_SIZE}&raw_json=1` + (after ? `&after=${encodeURIComponent(after)}` : "");
        const data = (await readReddit(url)).data;
        if (!data || !Array.isArray(data.children)) {
            const error = new Error("Reddit didn't answer with a listing");
            error.debug = debugNotes(url, [], null);
            throw error;
        }
        return data;
    }

    // After a user's posts answered 403 or 404: "suspended" or "deleted" if their profile (about.json) says so,
    // otherwise null (the failure is something else, e.g. a private profile)
    async function accountGone(name) {
        let about;
        try {
            about = await readReddit(`https://old.reddit.com/user/${encodeURIComponent(name)}/about.json?raw_json=1`);
        } catch (error) {
            if (error.status === 404) return "deleted";
            console.warn(`BDFR sync: u/${name}: couldn't read the profile: ${error.message}`, error.debug || "");
            return null;
        }
        return about && about.data && about.data.is_suspended ? "suspended" : null;
    }

    // JSON from Reddit, read with the browser's login, at most one request per SYNC_GAP_KEY milliseconds, waiting
    // for Reddit's rate limit window to reset when it's (nearly) used up. A 429 is retried after the reset, up to
    // SYNC_RATE_LIMITED_RETRIES times. Errors carry Reddit's HTTP status in error.status.
    async function readReddit(url) {
        for (let attempt = 1; ; attempt++) {
            const pause = redditPausedUntil - Date.now();
            if (pause > 0) {
                console.info(`BDFR sync: waiting ${formatWait(pause)} for Reddit's rate limit to reset`);
                syncProgress(`Waiting ${formatWait(pause)} for Reddit's rate limit to reset…`);
            }
            await sleep(Math.max(pause, lastRedditRead + GM_getValue(SYNC_GAP_KEY, 2000) - Date.now()));
            lastRedditRead = Date.now();
            try {
                return await readRedditOnce(url);
            } catch (error) {
                if (error.status !== 429 || attempt > SYNC_RATE_LIMITED_RETRIES) throw error;
            }
        }
    }

    // Notes when Reddit's rate limit window is (nearly) used up, from its x-ratelimit-remaining and -reset headers
    // (seconds until the window resets). A 429 pauses until the reset, or SYNC_RATE_LIMITED_WAIT_MS without one.
    function noteRateLimit(response) {
        const headers = {};
        for (const line of String(response.responseHeaders || "").split(/\r?\n/)) {
            const colon = line.indexOf(":");
            if (colon > 0) headers[line.slice(0, colon).trim().toLowerCase()] = line.slice(colon + 1).trim();
        }
        const remaining = parseFloat(headers["x-ratelimit-remaining"]);
        const reset = parseFloat(headers["x-ratelimit-reset"] || headers["retry-after"]);
        const resetMs = Number.isFinite(reset) ? reset * 1000 : null;
        if (response.status === 429) {
            redditPausedUntil = Date.now() + (resetMs ?? SYNC_RATE_LIMITED_WAIT_MS);
            console.warn(`BDFR sync: Reddit answered HTTP 429 (rate limited); waiting ${formatWait(resetMs
                ?? SYNC_RATE_LIMITED_WAIT_MS)}`);
        } else if (Number.isFinite(remaining) && remaining <= SYNC_RATE_RESERVE && resetMs !== null) {
            redditPausedUntil = Date.now() + resetMs;
        }
    }

    function formatWait(ms) {
        const seconds = Math.ceil(ms / 1000);
        return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
    }

    function readRedditOnce(url) {
        return new Promise((resolve, reject) => {
            const fail = (message, response, extra) => {
                const error = new Error(message);
                error.status = response && response.status;
                error.debug = debugNotes(url, [], response, extra);
                reject(error);
            };
            GM_xmlhttpRequest({
                method: "GET",
                url,
                anonymous: false,
                timeout: 30000,
                onload: (response) => {
                    noteRateLimit(response);
                    const finalUrl = response.finalUrl || "";
                    if (/\/login/.test(finalUrl)) return fail("Reddit asked to log in", response);
                    if (/\/over18/.test(finalUrl)) {
                        return fail("Reddit asks to confirm you're over 18 (allow mature content in Reddit's "
                            + "preferences)", response);
                    }
                    if (response.status === 404) {
                        return fail("Reddit answered HTTP 404 (deleted or suspended user?)", response);
                    }
                    if (response.status !== 200) return fail(`Reddit answered HTTP ${response.status}`, response);
                    let json;
                    try {
                        json = JSON.parse(response.responseText);
                    } catch (e) {
                        return fail("Reddit didn't answer with a listing", response);
                    }
                    resolve(json);
                },
                onerror: (response) => fail("Reddit couldn't be reached", response),
                ontimeout: (response) => fail("Reddit didn't answer in time", response),
            });
        });
    }

    const page = newDesign() ? null : pageSource();
    if (!page) return;

    const collected = GM_getValue(STORE_KEY, {});
    const state = {}; // post ID -> "new" | "existing" | "skipped" | "fetching" | "ignored"
    const ignoredWhy = {}; // post ID -> why it isn't sent
    let collecting = GM_getValue(COLLECTING_KEY, false);
    let galleryNote = ""; // why galleries couldn't be read from Reddit, shown with the job's progress
    let saveTimer = null;
    let refreshTimer = null;
    let polling = null;

    // www.reddit.com in the new design (unless Reddit's preferences ask for the old one), whose page has no .thing
    // elements to read posts from
    function newDesign() {
        return location.hostname !== "old.reddit.com"
            && Boolean(document.querySelector("shreddit-app, #SHORTCUT_FOCUSABLE_DIV"));
    }

    function pageSource() {
        // A user's multireddit lists other people's posts, like a subreddit page with several subreddits
        if (/^\/u(?:ser)?\/[\w-]+\/m(\/|$)/i.test(location.pathname)) {
            return SETTINGS.subredditPages ? { kind: "subreddit", user: null, folder: null } : null;
        }
        const user = location.pathname.match(/^\/u(?:ser)?\/([\w-]+)/i);
        if (user) return SETTINGS.userPages ? { kind: "user", user: user[1], folder: user[1] } : null;
        const subreddit = location.pathname.match(/^\/r\/([\w+]+)/i);
        if (subreddit) {
            // Multireddit-style "a+b" pages have no single folder name
            const folder = subreddit[1].includes("+") ? null : subreddit[1];
            return SETTINGS.subredditPages ? { kind: "subreddit", user: null, folder } : null;
        }
        return null;
    }

    // --- Reading posts from the page ---

    function absoluteUrl(url) {
        if (!url) return null;
        return url.startsWith("/") ? "https://www.reddit.com" + url : url;
    }

    function readPost(thing) {
        const fullname = thing.dataset.fullname || "";
        if (!fullname.startsWith("t3_") || thing.dataset.promoted === "true") return null;
        const title = thing.querySelector("a.title");
        const flair = thing.querySelector(".linkflairlabel");
        const timestamp = Number(thing.dataset.timestamp);
        const score = Number(thing.dataset.score);
        return {
            id: fullname.slice(3),
            title: title ? title.textContent.trim() : null,
            author: thing.dataset.author || null,
            subreddit: thing.dataset.subreddit || null,
            created_utc: timestamp ? Math.floor(timestamp / 1000) : null,
            url: absoluteUrl(thing.dataset.url),
            domain: thing.dataset.domain || null,
            permalink: thing.dataset.permalink || null,
            score: Number.isFinite(score) ? score : null,
            over_18: thing.dataset.nsfw === "true",
            link_flair_text: flair ? (flair.getAttribute("title") || flair.textContent.trim()) : null,
            is_gallery: thing.dataset.isGallery === "true",
        };
    }

    // Why a post isn't downloaded, or null. Posts linking straight to media need nothing more than the page has;
    // galleries get their image list from Reddit before sending (see addGalleryData).
    function ignoreReason(post) {
        if (!post.url || !post.title || !post.author || !post.subreddit || !post.created_utc) {
            return "incomplete post data on the page";
        }
        if ((post.domain || "").startsWith("self.")) return "self post";
        if (isGallery(post)) return null;
        if (/^https:\/\/(www\.|old\.|np\.|new\.)?reddit\.com\//.test(post.url)) {
            return "crosspost or link to another Reddit post";
        }
        return null;
    }

    function isGallery(post) {
        return post.is_gallery || /reddit\.com\/gallery\//.test(post.url || "");
    }

    function ignore(post, reason) {
        state[post.id] = "ignored";
        ignoredWhy[post.id] = reason;
    }

    // Reddit's by_id listing for up to 100 posts, read with the browser's Reddit login. GM_xmlhttpRequest is used
    // rather than fetch, because a userscript's fetch doesn't send the site's cookies in every browser, and without
    // them Reddit redirects to its login page.
    function readFromReddit(ids) {
        const url = `https://old.reddit.com/by_id/${ids.map((id) => "t3_" + id).join(",")}.json?raw_json=1`;
        return new Promise((resolve, reject) => {
            const fail = (message, response, extra) => {
                const error = new Error(message);
                error.debug = debugNotes(url, ids, response, extra);
                reject(error);
            };
            GM_xmlhttpRequest({
                method: "GET",
                url,
                anonymous: false,
                timeout: 30000,
                onload: (response) => {
                    if (/\/login/.test(response.finalUrl || "")) return fail("Reddit asked to log in", response);
                    if (response.status !== 200) return fail(`Reddit answered HTTP ${response.status}`, response);
                    try {
                        resolve(JSON.parse(response.responseText).data.children.map((child) => child.data));
                    } catch (e) {
                        fail("Reddit didn't answer with post data", response, { parseError: String(e) });
                    }
                },
                onerror: (response) => fail("Reddit couldn't be reached", response),
                ontimeout: (response) => fail("Reddit didn't answer in time", response),
            });
        });
    }

    // What's useful for finding out why reading from Reddit failed, logged to the browser console
    function debugNotes(url, ids, response, extra) {
        const info = typeof GM_info !== "undefined" ? GM_info : {};
        response = response || {};
        return {
            request: url,
            ids,
            status: response.status,
            statusText: response.statusText,
            finalUrl: response.finalUrl,
            responseHeaders: response.responseHeaders,
            responseStart: (response.responseText || "").slice(0, 500),
            error: response.error,
            // old.reddit marks the page body "loggedin" when you are, so this shows whether the page itself was
            pageLoggedIn: document.body.classList.contains("loggedin"),
            pageUser: (document.querySelector("#header-bottom-right .user a") || {}).textContent || null,
            scriptHandler: `${info.scriptHandler || "?"} ${info.version || ""}`.trim(),
            scriptVersion: info.script ? info.script.version : null,
            userAgent: navigator.userAgent,
            ...extra,
        };
    }

    // Reads galleries' full data from Reddit (up to 100 per request), so the image list goes with the post and
    // bdfr serve doesn't look it up in Arctic Shift. If Reddit doesn't answer, the posts are sent as they are, with
    // the reason, and the server falls back to Arctic Shift.
    async function addGalleryData(posts) {
        galleryNote = "";
        const galleries = posts.filter(isGallery);
        if (!galleries.length) return posts;
        for (const post of galleries) state[post.id] = "fetching";
        applyMarks();
        const ignored = new Set();
        let failed = 0;
        let failure = "";
        for (let start = 0; start < galleries.length; start += BY_ID_BATCH) {
            const batch = galleries.slice(start, start + BY_ID_BATCH);
            setStatus(`Reading ${galleries.length} galleries from Reddit…`
                + (galleries.length > BY_ID_BATCH ? ` (${start + batch.length}/${galleries.length})` : ""));
            let children = [];
            try {
                children = await readFromReddit(batch.map((post) => post.id));
            } catch (error) {
                failed += batch.length;
                failure = error.message;
                for (const post of batch) post.bdfr_note = error.message;
                console.error(`BDFR: couldn't read ${batch.length} galleries from Reddit: ${error.message}`,
                    error.debug || error);
                continue;
            }
            const byId = Object.fromEntries(children.map((data) => [data.id, data]));
            for (const post of batch) {
                const data = byId[post.id];
                if (!data) {
                    post.bdfr_note = "not in Reddit's answer";
                    console.error(`BDFR: Reddit's answer had no data for gallery ${post.id}`,
                        { returned: children.map((child) => child.id), requested: batch.map((p) => p.id) });
                    continue;
                }
                if (data.crosspost_parent) {
                    ignore(post, "crosspost");
                    ignored.add(post.id);
                } else if (data.gallery_data) {
                    post.gallery_data = data.gallery_data;
                    post.media_metadata = trimMediaMetadata(data.media_metadata);
                    if (data.upvote_ratio != null) post.upvote_ratio = data.upvote_ratio;
                } else {
                    post.bdfr_note = "Reddit sent no gallery data";
                    console.error(`BDFR: Reddit sent no gallery_data for ${post.id}`, {
                        is_gallery: data.is_gallery, removed_by_category: data.removed_by_category,
                        media_metadata: !!data.media_metadata, url: data.url, keys: Object.keys(data),
                    });
                }
            }
        }
        for (const post of galleries) {
            if (state[post.id] === "fetching") delete state[post.id];
        }
        applyMarks();
        if (failed) galleryNote = `${failed} galleries looked up in Arctic Shift (${failure})`;
        return posts.filter((post) => !ignored.has(post.id));
    }

    // Only each image's type and status are kept; the preview sizes Reddit lists aren't used
    function trimMediaMetadata(metadata) {
        if (!metadata) return undefined;
        const out = {};
        for (const [id, item] of Object.entries(metadata)) out[id] = { status: item.status, e: item.e, m: item.m };
        return out;
    }

    // The posts on the page that still need downloading, with what the server needs to download them
    async function postsToSend() {
        await refreshKnown();
        const pending = [];
        for (const post of postsOnPage()) {
            if (["new", "existing", "duplicate", "unavailable", "ignored"].includes(state[post.id])) continue;
            const reason = ignoreReason(post);
            if (reason) ignore(post, reason);
            else pending.push(post);
        }
        applyMarks();
        return addGalleryData(pending);
    }

    function things() {
        return [...document.querySelectorAll(".thing.link[data-fullname^='t3_']")];
    }

    // Each post once, even if Never Ending Reddit shows it again on a later page
    function postsOnPage() {
        const seen = new Set();
        return things().map(readPost).filter((post) => post && !seen.has(post.id) && seen.add(post.id));
    }

    // --- Collecting posts across pages for Export (only while switched on) ---

    function collect() {
        let added = 0;
        for (const post of postsOnPage()) {
            if (!collected[post.id]) {
                collected[post.id] = post;
                added++;
            }
        }
        if (added) {
            clearTimeout(saveTimer);
            saveTimer = setTimeout(() => GM_setValue(STORE_KEY, collected), 500);
            updateButtons();
        }
    }

    function toggleCollecting() {
        collecting = !collecting;
        GM_setValue(COLLECTING_KEY, collecting);
        if (collecting) collect();
        updateButtons();
    }

    function exportPosts() {
        GM_setValue(STORE_KEY, collected);
        const list = Object.values(collected).sort((a, b) => (b.created_utc || 0) - (a.created_utc || 0));
        const payload = { format: "bdfr-posts", version: 1, exported_at: new Date().toISOString(), posts: list };
        const blob = new Blob([JSON.stringify(payload, null, 1)], { type: "application/json" });
        const link = document.createElement("a");
        const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
        link.href = URL.createObjectURL(blob);
        link.download = `bdfr-posts-${stamp}.json`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    }

    function clearCollected() {
        if (!confirm(`Forget all ${Object.keys(collected).length} collected posts?`)) return;
        Object.keys(collected).forEach((id) => delete collected[id]);
        GM_setValue(STORE_KEY, collected);
        updateButtons();
    }

    // --- Talking to `bdfr serve` ---

    // Every failure is also logged to the browser console (as a warning starting "BDFR:"), so it's never only a
    // passing status line. The error's message is what describeError shows.
    function request(method, path, body) {
        return new Promise((resolve, reject) => {
            const fail = (message, detail, response) => {
                response = response || {};
                console.warn(`BDFR: ${method} ${path} to bdfr serve failed: ${detail}`, {
                    server: GM_getValue(SERVER_KEY, DEFAULT_SERVER),
                    status: response.status,
                    finalUrl: response.finalUrl,
                    error: response.error,
                    responseStart: (response.responseText || "").slice(0, 300),
                });
                reject(new Error(message));
            };
            GM_xmlhttpRequest({
                method,
                url: GM_getValue(SERVER_KEY, DEFAULT_SERVER) + path,
                headers: { "Content-Type": "application/json", "X-BDFR-Token": GM_getValue(TOKEN_KEY, "") },
                data: body ? JSON.stringify(body) : undefined,
                timeout: 30000,
                onload: (response) => {
                    let data = null;
                    try {
                        data = JSON.parse(response.responseText);
                    } catch (e) {
                        // checked below
                    }
                    if (response.status === 401) return fail("unauthorised", "HTTP 401 (wrong token)", response);
                    if (response.status >= 400) {
                        const message = (data && data.error) || `HTTP ${response.status}`;
                        return fail(message, `HTTP ${response.status}: ${message}`, response);
                    }
                    // Something else answering on the address (a router page, a proxy) must not pass for bdfr serve
                    if (!data || typeof data !== "object") {
                        return fail("bdfr serve's answer was not JSON", "the answer was not JSON", response);
                    }
                    resolve(data);
                },
                onerror: (response) => fail("unreachable", "it couldn't be reached"
                    + (response && response.error ? ` (${response.error})` : ""), response),
                ontimeout: (response) => fail("unreachable", "it didn't answer in time", response),
            });
        });
    }

    function configure() {
        const server = prompt("bdfr serve address:", GM_getValue(SERVER_KEY, DEFAULT_SERVER));
        if (server === null) return false;
        GM_setValue(SERVER_KEY, server.trim().replace(/\/+$/, "") || DEFAULT_SERVER);
        const token = prompt("Token printed by `bdfr serve` (also in serve_token.txt in BDFR's config folder):",
            GM_getValue(TOKEN_KEY, ""));
        if (token === null) return false;
        GM_setValue(TOKEN_KEY, token.trim());
        refreshKnown();
        refreshActivity();
        refreshUser();
        return true;
    }

    function folder() {
        if (page.folder) return page.folder;
        const chosen = prompt("Folder to download into (under bdfr serve's directory):",
            GM_getValue("bdfr-last-folder", ""));
        if (!chosen) return null;
        GM_setValue("bdfr-last-folder", chosen.trim());
        return chosen.trim();
    }

    function describeError(error) {
        if (error.message === "unreachable") return "bdfr serve isn't running";
        if (error.message === "unauthorised") return "Wrong token (⚙)";
        return `Error: ${error.message}`;
    }

    // Asks the server which posts on the page are already downloaded, so they are marked and not sent again: posts
    // with files in the folder, and posts whose link was downloaded before (reposts of the same Redgifs, image, ...)
    async function refreshKnown() {
        if (!page.folder || !GM_getValue(TOKEN_KEY, "")) return;
        const posts = postsOnPage().filter((post) => !state[post.id]);
        if (!posts.length) return;
        const ids = posts.map((post) => post.id);
        const links = Object.fromEntries(posts.filter((post) => post.url && !isGallery(post))
            .map((post) => [post.id, post.url]));
        try {
            const result = await request("POST", "/known", { folder: page.folder, ids, links });
            for (const id of result.known) state[id] = "existing";
            for (const id of result.duplicates || []) state[id] = "duplicate";
            for (const id of result.unavailable || []) state[id] = "unavailable";
            applyMarks();
        } catch (error) {
            setStatus(describeError(error));
        }
    }

    async function startJob(body, note) {
        if (!GM_getValue(TOKEN_KEY, "") && !configure()) return;
        jobNote = note || "";
        setStatus("Sending…" + (jobNote ? ` (${jobNote})` : ""));
        try {
            const job = await request("POST", "/jobs", body);
            watchJob(job.id);
            refreshActivity();
        } catch (error) {
            setStatus(describeError(error));
            if (error.message === "unauthorised" && configure()) startJob(body, note);
        }
    }

    let preparing = false;
    let jobNote = ""; // what the running job reads besides the page, shown with its progress

    // Downloads the page's posts that aren't downloaded yet. On a user page it also reads the user's archive (Arctic
    // Shift) when the page may not have everything new: when it's empty (e.g. the user's posts are hidden or removed
    // on Reddit), or its oldest post isn't downloaded yet, so there may be more beyond it. The archive is then read
    // until 5 already-downloaded posts in a row.
    async function download() {
        if (preparing) return;
        if (!GM_getValue(TOKEN_KEY, "") && !configure()) return;
        const target = folder();
        if (!target) return;
        preparing = true;
        try {
            const posts = await postsToSend();
            const readArchive = Boolean(page.user) && !pageReachesDownloaded();
            if (!posts.length && !readArchive) return setStatus("Nothing new on this page");
            const body = { folder: target, posts };
            let note = "";
            if (readArchive) {
                body.user = page.user;
                // The page's posts are already in hand, so the archive is only read below the oldest of them
                const oldest = oldestOwnPost(false);
                if (oldest && oldest.created_utc) body.before = oldest.created_utc;
                note = "and newer posts from the archive" + (body.before ? " before it" : "");
            }
            await startJob(body, note);
        } finally {
            preparing = false;
        }
    }

    // Whether the oldest of the user's posts on the page (leaving out ignored ones) is already downloaded. Arctic
    // Shift is behind Reddit, so it can't have anything newer than the page; it can only add posts older than it.
    function pageReachesDownloaded() {
        const oldest = oldestOwnPost(true);
        return Boolean(oldest) && ["existing", "duplicate", "unavailable"].includes(state[oldest.id]);
    }

    // The oldest of the user's posts on the page, optionally leaving out ignored ones (self posts, crossposts)
    function oldestOwnPost(withoutIgnored) {
        const own = postsOnPage().filter((post) => (!withoutIgnored || state[post.id] !== "ignored")
            && post.created_utc && (post.author || "").toLowerCase() === page.user.toLowerCase());
        if (!own.length) return null;
        return own.reduce((a, b) => (a.created_utc <= b.created_utc ? a : b));
    }

    function watchJob(jobId) {
        clearInterval(polling);
        const poll = async () => {
            let job;
            try {
                job = await request("GET", `/jobs/${jobId}`);
            } catch (error) {
                clearInterval(polling);
                return setStatus(describeError(error));
            }
            const c = job.counts;
            const duplicates = c.duplicate || 0;
            const failed = c.failed || 0; // older servers count failures as skipped
            const unavailable = c.unavailable || 0;
            const done = c.new + c.existing + duplicates + c.skipped + failed + unavailable;
            const progress = job.total ? `${done}/${job.total}` : `${done}`;
            const prefix = { queued: "Queued", running: "Downloading", done: "Done", failed: "Failed" }[job.state];
            const reading = jobNote && job.state === "running" ? ` (page ${jobNote})` : "";
            setStatus(`${prefix}${reading} ${progress} · ${c.new} new · ${c.existing} had`
                + (duplicates ? ` · ${duplicates} duplicates` : "") + ` · ${c.skipped} skipped`
                + (failed ? ` · ${failed} failed` : "") + (unavailable ? ` · ${unavailable} unavailable` : "")
                + (job.error ? ` · ${job.error}` : "") + (galleryNote ? ` · ${galleryNote}` : ""));
            Object.assign(state, job.results);
            applyMarks();
            if (job.state === "failed") {
                console.warn(`BDFR: job ${jobId} failed: ${job.error || "no reason given"} (details in serve_log.txt)`,
                    job.counts);
            }
            if (job.state === "done" || job.state === "failed") {
                clearInterval(polling);
                refreshActivity();
            }
        };
        poll();
        polling = setInterval(poll, 2000);
    }

    // --- Marks next to post titles ---

    // Re-applied whenever posts are added or results change, so posts loaded later are marked too
    function applyMarks() {
        for (const thing of things()) {
            const id = thing.dataset.fullname.slice(3);
            const label = state[id];
            const title = thing.querySelector("a.title");
            let mark = thing.querySelector(".bdfr-mark");
            if (!label || !title) {
                if (mark) mark.remove();
                continue;
            }
            if (!mark) {
                mark = document.createElement("span");
                mark.className = "bdfr-mark";
                mark.style.cssText = "margin-left:6px;font-size:11px;font-weight:bold";
                title.after(mark);
            }
            // A label from a newer server than this script shows as skipped
            const [symbol, colour, tip] = MARKS[label] || MARKS.skipped;
            mark.textContent = symbol;
            mark.style.color = colour;
            mark.title = `BDFR: ${tip}` + (label === "ignored" && ignoredWhy[id] ? `: ${ignoredWhy[id]}` : "");
        }
    }

    // --- Panel ---

    function button(label, title, onClick) {
        const element = document.createElement("button");
        element.textContent = label;
        element.title = title;
        element.style.cssText = `background:${COLOURS.raised};color:${COLOURS.textStrong};border:0;border-radius:3px;`
            + "padding:2px 7px;font:11px sans-serif;cursor:pointer";
        element.addEventListener("click", onClick);
        return element;
    }

    const panel = document.createElement("div");
    panel.style.cssText = `position:fixed;bottom:10px;right:10px;z-index:2147483647;background:${COLOURS.background};`
        + `color:${COLOURS.text};border:1px solid ${COLOURS.raised};border-radius:4px;padding:4px 6px;`
        + "font:11px sans-serif;display:flex;flex-direction:column;gap:3px;max-width:380px;opacity:.45;"
        + "transition:opacity .15s";
    panel.addEventListener("mouseenter", () => (panel.style.opacity = "1"));
    panel.addEventListener("mouseleave", () => (panel.style.opacity = ".45"));

    const buttons = document.createElement("div");
    buttons.style.cssText = "display:flex;gap:4px;align-items:center;flex-wrap:wrap";
    const collectButton = button("", "Collect posts from the pages you visit, for Export", toggleCollecting);
    const exportButton = button("Export", "Save the collected posts as JSON for bdfr download --post-json",
        exportPosts);
    const clearButton = button("Clear", "Forget the collected posts", clearCollected);
    // Collecting for Export (bdfr download --post-json) is rarely needed, so it's tucked away; the row stays open
    // (remembered) until Extras is clicked again
    let extrasOpen = GM_getValue(EXTRAS_KEY, false);
    const extras = document.createElement("div");
    extras.className = "bdfr-extras";
    extras.style.cssText = "flex-basis:100%;gap:4px;align-items:center;flex-wrap:wrap";
    extras.append(collectButton, exportButton, clearButton);
    const extrasButton = button("", "Collect posts across pages and export them as JSON", () => {
        extrasOpen = !extrasOpen;
        GM_setValue(EXTRAS_KEY, extrasOpen);
        updateButtons();
    });
    buttons.append(button("Download", page.user
        ? "Download the posts on this page that aren't downloaded yet. If the page is empty or its oldest post isn't "
            + "downloaded yet, newer posts are also read from the archive until already-downloaded ones."
        : "Download the posts on this page that aren't downloaded yet", download));
    buttons.append(
        button("Sync", "Sync every watched user (bdfr serve's watchlist) from Reddit now, as the background sync does "
            + `every ${SETTINGS.syncEveryHours} hours`, syncNow),
        button("Status", "bdfr serve's recent jobs, queue, failed posts waiting for a retry and watchlist problems",
            toggleServerView),
        button("Dashboard", "Open bdfr serve's dashboard in a new tab", openDashboard),
        button("⚙", "bdfr serve address and token", configure),
        extrasButton, extras);

    // The dashboard asks for the token once; it's handed over in the address's fragment, which isn't sent to the
    // server, and the dashboard removes it from the address at once
    function openDashboard() {
        const server = GM_getValue(SERVER_KEY, DEFAULT_SERVER);
        const token = GM_getValue(TOKEN_KEY, "");
        window.open(`${server}/` + (token ? `#token=${encodeURIComponent(token)}` : ""), "_blank", "noopener");
    }

    const status = document.createElement("div");
    // What bdfr serve is doing: the running job, and how many wait behind it
    const activity = document.createElement("div");
    activity.className = "bdfr-activity";
    // The Status view: what bdfr serve is doing, refreshed while it's open
    const serverView = document.createElement("div");
    serverView.className = "bdfr-server-view";
    serverView.style.cssText = `display:none;border-top:1px solid ${COLOURS.raised};padding-top:3px;`
        + "max-height:360px;overflow-y:auto";
    // On a user page: whether bdfr serve watches the user (watchlist.yaml), and what their folder holds
    const watch = document.createElement("div");
    watch.className = "bdfr-watch";
    const downloads = document.createElement("div");
    downloads.className = "bdfr-downloads";
    panel.append(buttons, status, activity, watch, downloads, serverView);
    document.body.appendChild(panel);

    let serverViewTimer = null;
    let activityTimer = null;
    let activityBusy = false;

    // Shows what bdfr serve is doing, again every ACTIVITY_EVERY_MS while the tab is visible
    async function refreshActivity() {
        clearTimeout(activityTimer);
        activityTimer = null;
        if (activityBusy) return;
        if (!GM_getValue(TOKEN_KEY, "")) return (activity.textContent = "");
        activityBusy = true;
        let wait = ACTIVITY_EVERY_MS;
        try {
            const jobs = await request("GET", "/jobs");
            activity.textContent = describeActivity(Array.isArray(jobs.jobs) ? jobs.jobs : []);
        } catch (error) {
            // An older bdfr serve without a job list (404) shows nothing
            activity.textContent = ["unreachable", "unauthorised"].includes(error.message) ? describeError(error) : "";
            wait = ACTIVITY_RETRY_MS;
        } finally {
            activityBusy = false;
        }
        if (document.visibilityState === "visible") activityTimer = setTimeout(refreshActivity, wait);
    }

    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible" && !activityTimer && !activityBusy) refreshActivity();
    });

    // Shows whether the page's user is on bdfr serve's watchlist, and what their folder holds, read once as the page
    // loads (GET /user). A failure shows nothing here (request() logs it, and the activity line says when bdfr serve
    // is down); an older bdfr serve without it (404) neither.
    async function refreshUser() {
        if (!page.user || !GM_getValue(TOKEN_KEY, "")) return;
        let info;
        try {
            info = await request("GET", `/user?name=${encodeURIComponent(page.user)}`);
        } catch (error) {
            watch.textContent = downloads.textContent = "";
            return;
        }
        const [text, colour, tip] = describeWatch(info);
        watch.textContent = text;
        watch.style.color = colour;
        watch.title = tip;
        downloads.textContent = describeDownloads(info);
        downloads.title = `In the folder ${info.folder} under bdfr serve's directory`;
    }

    // e.g. "Watched · folder other · hibernating · whole archive read", "Watched as an old account of u/new", or
    // "Not watched", with its colour and tooltip
    function describeWatch(info) {
        const none = "Add them to watchlist.yaml in BDFR's config folder to keep their folder up to date";
        if (!info.watchlist) return ["Not watched: bdfr serve has no watchlist", COLOURS.text, none];
        const user = info.watched;
        if (!user) return ["Not watched", COLOURS.text, none];
        const tip = "On bdfr serve's watchlist: synced from Reddit in the background, and read from the archive";
        // Left out of the sync until its recheck
        if (user.recheck) return [`Watched · ${user.gone} on Reddit, not synced for now`, COLOURS.text, tip];
        const parts = [user.alias_of ? `Watched as an old account of u/${user.alias_of}` : "Watched"];
        if (user.folder && user.folder.toLowerCase() !== info.name.toLowerCase()) parts.push(`folder ${user.folder}`);
        if (!user.alias_of) {
            if (user.gone) parts.push(`${user.gone} on Reddit`);
            else if (user.hibernating) parts.push("hibernating (no recent posts)");
            if (user.full_archive_done) parts.push("whole archive read");
            else if (user.full_archive_stopped) parts.push("whole archive not read (stopped)");
            else if (user.full_archive_capped) parts.push("archive read stopped at its storage limit");
            else parts.push("archive not read in full yet");
            if (user.last_error) parts.push(`last check failed: ${user.last_error}`);
        }
        return [parts.join(" · "), user.last_error ? MARKS.failed[1] : MARKS.new[1], tip];
    }

    // e.g. "312 posts · 1,204 files (980 images, 224 videos) · 41.2 GB"
    function describeDownloads(info) {
        const d = info.downloads;
        if (!d || !d.file_count) return "Nothing downloaded yet";
        const count = (n, word) => `${Number(n || 0).toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
        return `${count(d.posts, "post")} · ${count(d.file_count, "file")} `
            + `(${count(d.images, "image")}, ${count(d.videos, "video")}) · ${formatBytes(d.bytes)}`;
    }

    // As the dashboard shows sizes
    function formatBytes(bytes) {
        const units = ["B", "KB", "MB", "GB", "TB"];
        let value = bytes || 0;
        let unit = 0;
        for (; value >= 1000 && unit < units.length - 1; unit++) value /= 1000;
        return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
    }

    function describeActivity(jobs) {
        const running = jobs.find((job) => job.state === "running");
        const queued = jobs.filter((job) => job.state === "queued");
        const scheduled = queued.filter((job) => job.scheduled).length;
        const parts = [running ? `Server running ${describeJobTarget(running)} · ${jobProgress(running)} done`
            : "Server idle"];
        if (queued.length > scheduled) parts.push(`${queued.length - scheduled} queued`);
        if (scheduled) parts.push(`${scheduled} scheduled`);
        return parts.join(" · ");
    }

    // e.g. "someone: archive (scheduled)" or "other: 5 posts (retry)"
    function describeJobTarget(job) {
        return `${job.folder}: ${describeJobWhat(job)}`;
    }

    // e.g. "archive (scheduled)" (the folder's user's archive), "archive of u/oldname" or "5 posts (retry)"
    function describeJobWhat(job) {
        const archive = job.user && (job.user.toLowerCase() === job.folder.toLowerCase() ? "archive"
            : `archive of u/${job.user}`);
        const what = archive || plural(job.total, "post");
        return what + (job.retry ? " (retry)" : job.scheduled ? " (scheduled)" : "");
    }

    function plural(count, word) {
        return `${count} ${word}${count === 1 ? "" : "s"}`;
    }

    function jobProgress(job) {
        const done = Object.values(job.counts || {}).reduce((sum, count) => sum + count, 0);
        return job.total ? `${done}/${job.total}` : `${done}`;
    }

    function toggleServerView() {
        const open = serverView.style.display === "none";
        serverView.style.display = open ? "" : "none";
        // The job table needs more room than the buttons
        panel.style.maxWidth = open ? "520px" : "380px";
        clearInterval(serverViewTimer);
        if (!open) return;
        refreshServerView();
        serverViewTimer = setInterval(refreshServerView, 5000);
    }

    async function refreshServerView() {
        let serverStatus;
        try {
            serverStatus = await request("GET", "/status");
        } catch (error) {
            serverView.textContent = describeError(error);
            return;
        }
        // Older servers have no job list (404) or watchlist; the rest is still shown
        const [jobs, watchlist] = await Promise.all([
            request("GET", "/jobs").catch(() => null),
            request("GET", "/watchlist").catch(() => null),
        ]);
        serverView.replaceChildren(...describeServer(serverStatus, jobs, watchlist));
    }

    // How a job's state shows in the Status view
    const JOB_STATES = {
        queued: ["◷", COLOURS.text, "Queued"],
        running: ["⋯", COLOURS.textStrong, "Running"],
        done: ["✔", MARKS.new[1], "Done"],
        failed: ["✖", MARKS.failed[1], "Failed (the reason is below it, and in serve_log.txt)"],
    };
    // How each count of a job reads in the Status view, coloured like the marks next to post titles
    const COUNT_NAMES = {
        new: "new", existing: "had", duplicate: "duplicate", skipped: "skipped", failed: "failed",
        unavailable: "unavailable",
    };

    function element(tag, text, style, title) {
        const node = document.createElement(tag);
        if (text) node.textContent = text;
        if (style) node.style.cssText = style;
        if (title) node.title = title;
        return node;
    }

    function describeServer(serverStatus, jobs, watchlist) {
        const nodes = [];
        let line = `bdfr serve ${serverStatus.version} · ${serverStatus.running ? "running a job" : "idle"}`
            + ` · ${serverStatus.queued} queued`
            + (serverStatus.scheduled ? ` (${serverStatus.scheduled} scheduled)` : "");
        const retries = serverStatus.retries;
        if (retries) line += ` · failed posts: ${retries.waiting} to retry, ${retries.given_up} given up`;
        nodes.push(element("div", line, `color:${COLOURS.textStrong}`));
        if (!jobs) {
            nodes.push(element("div", "This bdfr serve doesn't list its jobs; update it to see them"));
        } else if (!jobs.jobs.length) {
            nodes.push(element("div", "No jobs since bdfr serve started"));
        } else {
            nodes.push(jobTable(jobs.jobs));
        }
        const users = (watchlist && watchlist.users) || [];
        const problems = users.filter((user) => user.last_error);
        if (problems.length) {
            nodes.push(sectionTitle("Watchlist problems"));
            for (const user of problems) {
                const row = element("div", "", "padding-left:14px");
                row.append(element("span", `u/${user.name}`, `color:${COLOURS.textStrong}`), `: ${user.last_error}`);
                nodes.push(row);
            }
        }
        const gone = users.filter((user) => user.gone).length + ((watchlist && watchlist.gone) || []).length;
        if (gone) nodes.push(element("div", `${gone} watched users suspended or deleted`, "margin-top:4px"));
        return nodes;
    }

    function sectionTitle(text) {
        return element("div", text, `color:${COLOURS.textStrong};font-weight:bold;margin-top:5px`);
    }

    // The running job, the queued ones in the order they'll run, and the finished ones, one row each
    function jobTable(jobs) {
        const running = jobs.filter((job) => job.state === "running");
        // The userscript's jobs run first, then serve's own (archive checks, retries)
        const waiting = jobs.filter((job) => job.state === "queued")
            .sort((a, b) => Boolean(a.scheduled) - Boolean(b.scheduled) || a.created - b.created);
        const finished = jobs.filter((job) => !["running", "queued"].includes(job.state));
        const table = element("table", "", "border-collapse:collapse;width:100%");
        table.className = "bdfr-jobs";
        for (const [title, list] of [["Running", running], ["Waiting", waiting], ["Recent jobs", finished]]) {
            if (!list.length) continue;
            const heading = table.insertRow();
            heading.className = "bdfr-section";
            const cell = heading.insertCell();
            cell.colSpan = 6;
            cell.append(sectionTitle(list.length > 15 ? `${title} (newest 15 of ${list.length})` : title));
            for (const job of list.slice(0, 15)) addJobRows(table, job);
        }
        return table;
    }

    function addJobRows(table, job) {
        const row = table.insertRow();
        row.className = "bdfr-job";
        row.dataset.state = job.state;
        const add = (node, style = "") => {
            const cell = row.insertCell();
            cell.style.cssText = "padding:1px 8px 1px 0;vertical-align:top;white-space:nowrap;" + style;
            cell.append(node);
            return cell;
        };
        const [symbol, colour, tip] = JOB_STATES[job.state] || [job.state, COLOURS.text, job.state];
        add(element("span", symbol, `color:${colour}`, tip), "padding-right:5px");
        add(element("span", formatJobTime(job.created)));
        add(element("span", job.folder, `color:${COLOURS.textStrong}`, job.folder),
            "max-width:130px;overflow:hidden;text-overflow:ellipsis");
        add(element("span", describeJobWhat(job)));
        add(jobCounts(job), "white-space:normal;width:100%");
        add(element("span", jobDuration(job), "", "How long it ran"), "text-align:right;padding-right:0");
        if (job.error) {
            const errorRow = table.insertRow();
            errorRow.className = "bdfr-job-error";
            const cell = errorRow.insertCell();
            cell.colSpan = 6;
            cell.style.cssText = `padding:0 0 2px 14px;color:${MARKS.failed[1]}`;
            cell.textContent = `↳ ${job.error}`;
        }
    }

    // e.g. "17 new · 7 had · 9 duplicate", each number in its mark's colour
    function jobCounts(job) {
        const counts = element("span");
        for (const [label, name] of Object.entries(COUNT_NAMES)) {
            const count = (job.counts || {})[label];
            if (!count) continue;
            if (counts.childNodes.length) counts.append(" · ");
            const part = element("span", "", "white-space:nowrap", MARKS[label] ? MARKS[label][2] : "");
            part.append(element("span", String(count), `color:${(MARKS[label] || MARKS.skipped)[1]};font-weight:bold`),
                ` ${name}`);
            counts.append(part);
        }
        return counts;
    }

    // The time a job was queued, with the day when it isn't today
    function formatJobTime(seconds) {
        const date = new Date(seconds * 1000);
        const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        if (date.toDateString() === new Date().toDateString()) return time;
        return `${date.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
    }

    // How long a job ran (so far, while it runs); empty for one that hasn't started, or from an older bdfr serve
    function jobDuration(job) {
        if (!job.started) return "";
        const seconds = Math.max(0, Math.round((job.finished || Date.now() / 1000) - job.started));
        if (seconds < 60) return `${seconds}s`;
        if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
        return `${Math.floor(seconds / 3600)}h ${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}m`;
    }

    function setStatus(text) {
        status.textContent = text;
    }
    reportSync = setStatus;

    function updateButtons() {
        const count = Object.keys(collected).length;
        collectButton.textContent = collecting ? `Collecting (${count})` : "Collect";
        collectButton.style.color = collecting ? MARKS.new[1] : COLOURS.textStrong;
        exportButton.style.display = clearButton.style.display = count ? "" : "none";
        extras.style.display = extrasOpen ? "flex" : "none";
        extrasButton.textContent = (extrasOpen ? "Extras ▾" : "Extras ▸") + (collecting && !extrasOpen ? " ●" : "");
        extrasButton.title = collecting && !extrasOpen ? `Collecting posts (${count}) for Export`
            : "Collect posts across pages and export them as JSON";
    }

    updateButtons();
    if (collecting) collect();
    refreshKnown();
    refreshActivity();
    refreshUser();
    // Never Ending Reddit and RES add posts after load
    new MutationObserver((mutations) => {
        const addedPosts = mutations.some((mutation) => [...mutation.addedNodes].some((node) =>
            node.nodeType === Node.ELEMENT_NODE && !panel.contains(node) && !node.classList.contains("bdfr-mark")
            && (node.matches(".thing") || node.querySelector(".thing"))));
        if (!addedPosts) return;
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => {
            if (collecting) collect();
            applyMarks();
            refreshKnown();
        }, 500);
    }).observe(document.body, { childList: true, subtree: true });
})();
