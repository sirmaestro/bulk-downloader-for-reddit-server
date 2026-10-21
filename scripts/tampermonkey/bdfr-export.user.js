// ==UserScript==
// @name         BDFR downloader
// @namespace    bdfr-saved-hashes
// @version      4.2
// @description  Downloads the posts on old.reddit pages through a local `bdfr serve`, or collects them as JSON for `bdfr download --post-json`
// @match        https://old.reddit.com/*
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @connect      old.reddit.com
// @run-at       document-idle
// ==/UserScript==

(function () {
    "use strict";

    // Which pages the panel appears on. On other pages the script does nothing.
    const SETTINGS = {
        userPages: true, // old.reddit.com/user/<name>/...
        subredditPages: false, // old.reddit.com/r/<name>/...
        // Links to a user's profile open their submitted posts, newest first, on every old.reddit page
        userLinksToSubmitted: true,
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
        document.addEventListener("mousedown", (event) => {
            const link = event.target.closest && event.target.closest("a[href]");
            if (link) rewrite(link);
        }, true);
    }

    const COLOURS = { background: "#262626", raised: "#373737", text: "#9a9a9a", textStrong: "#d0d0d0" };
    const MARKS = {
        new: ["✔", "#8bc34a", "Downloaded"],
        existing: ["✔", "#8a8a8a", "Already downloaded"],
        duplicate: ["≡", "#8a8a8a", "Same media as something already downloaded (re-uploaded under another link)"],
        skipped: ["!", "#ffb74d", "Skipped: filtered out or failed (the reason is in serve_log.txt)"],
        fetching: ["⋯", "#8a8a8a", "Reading the gallery's images from Reddit"],
        ignored: ["–", "#6a6a6a", "Ignored"],
    };
    // Posts per Reddit by_id request (Reddit's maximum)
    const BY_ID_BATCH = 100;
    const STORE_KEY = "bdfr-posts";
    const COLLECTING_KEY = "bdfr-collecting";
    const SERVER_KEY = "bdfr-server";
    const TOKEN_KEY = "bdfr-token";
    const ARCHIVE_KEY = "bdfr-full-archive";
    const DEFAULT_SERVER = "http://127.0.0.1:7635";

    const page = pageSource();
    if (!page) return;

    const collected = GM_getValue(STORE_KEY, {});
    const state = {}; // post ID -> "new" | "existing" | "skipped" | "fetching" | "ignored"
    const ignoredWhy = {}; // post ID -> why it isn't sent
    let collecting = GM_getValue(COLLECTING_KEY, false);
    let fullArchive = GM_getValue(ARCHIVE_KEY, false);
    let galleryNote = ""; // why galleries couldn't be read from Reddit, shown with the job's progress
    let saveTimer = null;
    let refreshTimer = null;
    let polling = null;

    function pageSource() {
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
            if (["new", "existing", "duplicate", "ignored"].includes(state[post.id])) continue;
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
    // until 5 already-downloaded posts in a row; with Archive ticked, always and all of it.
    async function download() {
        if (preparing) return;
        if (!GM_getValue(TOKEN_KEY, "") && !configure()) return;
        const target = folder();
        if (!target) return;
        preparing = true;
        try {
            const posts = await postsToSend();
            const readArchive = Boolean(page.user) && (fullArchive || !pageReachesDownloaded());
            if (!posts.length && !readArchive) return setStatus("Nothing new on this page");
            const body = { folder: target, posts };
            let note = "";
            if (readArchive) {
                body.user = page.user;
                if (fullArchive) body.archive = "all";
                // The page's posts are already in hand, so the archive is only read below the oldest of them
                const oldest = oldestOwnPost(false);
                if (oldest && oldest.created_utc) body.before = oldest.created_utc;
                note = (fullArchive ? "and the whole archive" : "and newer posts from the archive")
                    + (body.before ? " before it" : "");
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
        return Boolean(oldest) && ["existing", "duplicate"].includes(state[oldest.id]);
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
            const done = c.new + c.existing + duplicates + c.skipped;
            const progress = job.total ? `${done}/${job.total}` : `${done}`;
            const prefix = { queued: "Queued", running: "Downloading", done: "Done", failed: "Failed" }[job.state];
            const reading = jobNote && job.state === "running" ? ` (page ${jobNote})` : "";
            setStatus(`${prefix}${reading} ${progress} · ${c.new} new · ${c.existing} had`
                + (duplicates ? ` · ${duplicates} duplicates` : "") + ` · ${c.skipped} skipped`
                + (job.error ? ` · ${job.error}` : "") + (galleryNote ? ` · ${galleryNote}` : ""));
            Object.assign(state, job.results);
            applyMarks();
            if (job.state === "failed") {
                console.warn(`BDFR: job ${jobId} failed: ${job.error || "no reason given"} (details in serve_log.txt)`,
                    job.counts);
            }
            if (job.state === "done" || job.state === "failed") clearInterval(polling);
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
            const [symbol, colour, tip] = MARKS[label];
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
    buttons.append(button("Download", page.user
        ? "Download the posts on this page that aren't downloaded yet. If the page is empty or its oldest post isn't "
            + "downloaded yet, newer posts are also read from the archive until already-downloaded ones."
        : "Download the posts on this page that aren't downloaded yet", download));
    if (page.user) buttons.append(archiveToggle());
    buttons.append(collectButton, exportButton, clearButton, button("⚙", "bdfr serve address and token", configure));

    // Ticked: every Download also goes through the user's whole archive (Arctic Shift), finding posts removed from
    // Reddit or older than the last download. Already-downloaded posts cost no requests, so this costs the listing
    // itself, about one request per 60-200 posts.
    function archiveToggle() {
        const label = document.createElement("label");
        label.className = "bdfr-archive";
        label.title = "Also read this user's whole archive on Arctic Shift, including posts removed from Reddit. "
            + "About one request per 60-200 archived posts.";
        label.style.cssText = `display:flex;align-items:center;gap:3px;cursor:pointer;color:${COLOURS.textStrong}`;
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = fullArchive;
        box.style.cssText = "margin:0;cursor:pointer";
        box.addEventListener("change", () => {
            fullArchive = box.checked;
            GM_setValue(ARCHIVE_KEY, fullArchive);
        });
        label.append(box, "Archive");
        return label;
    }
    const status = document.createElement("div");
    panel.append(buttons, status);
    document.body.appendChild(panel);

    function setStatus(text) {
        status.textContent = text;
    }

    function updateButtons() {
        const count = Object.keys(collected).length;
        collectButton.textContent = collecting ? `Collecting (${count})` : "Collect";
        collectButton.style.color = collecting ? MARKS.new[1] : COLOURS.textStrong;
        exportButton.style.display = clearButton.style.display = count ? "" : "none";
    }

    updateButtons();
    if (collecting) collect();
    refreshKnown();
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
