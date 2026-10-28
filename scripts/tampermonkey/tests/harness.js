// Runs bdfr-export.user.js in Chromium as Tampermonkey would, without the network: Reddit pages are fixture
// HTML, every other page request is blocked, and GM_xmlhttpRequest (Reddit's JSON, bdfr serve) is answered by
// routes defined in the test, which can also fail (HTTP errors, redirects to the login page, network errors,
// timeouts). Requests nothing answers are recorded and fail the test.
const fs = require("fs");
const path = require("path");

const SCRIPT = fs.readFileSync(path.join(__dirname, "..", "bdfr-export.user.js"), "utf8");
const FIXTURES = path.join(__dirname, "fixtures");
const SERVER = "http://127.0.0.1:7635";

function fixture(name) {
    return fs.readFileSync(path.join(FIXTURES, name), "utf8");
}

class Harness {
    constructor(context, { gm = {} } = {}) {
        this.context = context;
        this.routes = [];
        this.requests = [];
        this.unmatched = [];
        this.logs = [];
        this.pages = {
            "/user/someone/submitted/": fixture("user-page.html"),
            "/": "<!doctype html><html><head><title>reddit</title></head><body class='front-page loggedin'></body></html>",
        };
        // No pause between the background sync's Reddit requests
        this.gm = { "bdfr-token": "test-token", "bdfr-server": SERVER, "bdfr-sync-request-gap": 0, ...gm };
    }

    async setUp() {
        await this.context.exposeBinding("__bdfrGm", (_source, details) => this._answer(details));
        await this.context.addInitScript(({ initial }) => {
            // Also runs in the blank page a new tab starts with, which has no storage
            if (!["old.reddit.com", "www.reddit.com"].includes(location.hostname)) return;
            // GM_* values live in the origin's localStorage, so tabs share them as they do in Tampermonkey
            if (!localStorage.getItem("__gmSeeded")) {
                for (const [key, value] of Object.entries(initial)) localStorage.setItem(`gm:${key}`, JSON.stringify(value));
                localStorage.setItem("__gmSeeded", "1");
            }
            window.GM_getValue = (key, fallback) => {
                const stored = localStorage.getItem(`gm:${key}`);
                return stored === null ? fallback : JSON.parse(stored);
            };
            window.GM_setValue = (key, value) => localStorage.setItem(`gm:${key}`, JSON.stringify(value));
            // Tampermonkey's menu: tests run a command with page.evaluate(() => __gmMenu["..."]())
            window.__gmMenu = {};
            window.GM_registerMenuCommand = (name, onClick) => (window.__gmMenu[name] = onClick);
            window.GM_info = { scriptHandler: "Playwright", version: "test", script: { version: "test" } };
            window.GM_xmlhttpRequest = (details) => {
                const { onload, onerror, ontimeout, ...plain } = details;
                window.__bdfrGm(plain).then((result) => {
                    if (result.kind === "load") onload && onload(result.response);
                    else if (result.kind === "timeout") ontimeout && ontimeout(result.response);
                    else onerror && onerror(result.response);
                });
            };
        }, { initial: this.gm });
        await this.context.route("**/*", (route) => {
            const url = new URL(route.request().url());
            // Pages are keyed by path on old.reddit.com, and by origin and path on www.reddit.com
            const html = url.hostname === "old.reddit.com" ? this.pages[url.pathname] : this.pages[url.origin + url.pathname];
            if (html && route.request().resourceType() === "document") {
                return route.fulfill({ status: 200, contentType: "text/html", body: html });
            }
            return route.abort();
        });
        this.context.on("page", (page) => this._watch(page));
    }

    _watch(page) {
        page.on("console", (message) => this.logs.push({ type: message.type(), text: message.text() }));
        page.on("pageerror", (error) => this.logs.push({ type: "pageerror", text: String(error) }));
    }

    // A route answers requests whose method matches and whose URL starts with `url` (or matches it, for a RegExp).
    // Later routes win, so tests override the defaults. reply: { status, json | body, finalUrl } or
    // { network: "error" | "timeout" }, or a function of the request returning one; `headers` adds response headers.
    // `times` limits how often.
    on(method, url, reply, { times } = {}) {
        this.routes.unshift({ method, url, reply, times });
        return this;
    }

    serve(method, urlPath, reply, options) {
        return this.on(method, SERVER + urlPath, reply, options);
    }

    // bdfr serve with nothing downloaded yet, accepting jobs that finish at once
    defaultServe() {
        this.serve("POST", "/known", { json: { known: [], known_by_link: [], duplicates: [], unavailable: [], supported: true } });
        this.serve("POST", "/jobs", (request) => ({ status: 202, json: { id: "job1", state: "queued", ...counts(), total: (request.data.posts || []).length } }));
        // Only GET /jobs itself: the panel's line on what bdfr serve is doing
        this.on("GET", new RegExp(`^${SERVER}/jobs$`), { json: { jobs: [] } });
        this.serve("GET", "/jobs/job1", { json: { id: "job1", state: "done", ...counts(), results: {} } });
        this.serve("GET", "/watchlist", { json: { enabled: true, users: [] } });
        // The panel's line on the page's user
        this.on("GET", new RegExp(`^${SERVER}/user\\?`), (request) => ({
            json: { name: new URL(request.url).searchParams.get("name"), watchlist: true, watched: null, folder: "someone", downloads: null },
        }));
        this.serve("POST", "/watchlist/gone", (request) => ({ json: request.data }));
        this.serve("POST", "/sync", (request) => ({ json: request.data }));
        this.serve("POST", "/watchlist/hidden", (request) => ({ json: { user: request.data.user, hidden_on_reddit: request.data.hidden ? 1 : null } }));
        this.serve("POST", "/watchlist/reddit", (request) => ({ json: { user: request.data.user, reddit_read: 1 } }));
        return this;
    }

    async _answer(details) {
        const request = {
            method: (details.method || "GET").toUpperCase(),
            url: details.url,
            headers: details.headers || {},
            data: parse(details.data),
            anonymous: details.anonymous,
        };
        this.requests.push(request);
        for (const route of this.routes) {
            if (route.times === 0) continue;
            if (route.method && route.method !== request.method) continue;
            if (route.url instanceof RegExp ? !route.url.test(request.url) : !request.url.startsWith(route.url)) continue;
            if (route.times) route.times -= 1;
            const reply = typeof route.reply === "function" ? await route.reply(request) : route.reply;
            if (reply.network === "timeout") return { kind: "timeout", response: { status: 0, finalUrl: request.url } };
            if (reply.network) return { kind: "error", response: { status: 0, error: "net::ERR_CONNECTION_REFUSED", finalUrl: request.url } };
            const status = reply.status || 200;
            return {
                kind: "load",
                response: {
                    status,
                    statusText: String(status),
                    finalUrl: reply.finalUrl || request.url,
                    responseHeaders: [reply.json !== undefined ? "content-type: application/json" : "content-type: text/html",
                        ...Object.entries(reply.headers || {}).map(([name, value]) => `${name}: ${value}`)].join("\r\n"),
                    responseText: reply.json !== undefined ? JSON.stringify(reply.json) : reply.body || "",
                },
            };
        }
        this.unmatched.push(`${request.method} ${request.url}`);
        return { kind: "error", response: { status: 0, error: "no route in the test", finalUrl: request.url } };
    }

    async open(pathname = "/user/someone/submitted/?sort=new", origin = "https://old.reddit.com") {
        const page = await this.context.newPage();
        await page.goto(`${origin}${pathname}`);
        await page.addScriptTag({ content: SCRIPT });
        return page;
    }

    requestsTo(method, urlStart) {
        return this.requests.filter((r) => r.method === method && (urlStart instanceof RegExp ? urlStart.test(r.url) : r.url.startsWith(urlStart)));
    }

    // Console messages the script logged as warnings or errors, starting "BDFR"
    warnings() {
        return this.logs.filter((log) => ["warning", "error"].includes(log.type) && log.text.startsWith("BDFR")).map((log) => log.text);
    }
}

function counts(over = {}) {
    return { counts: { new: 0, existing: 0, duplicate: 0, skipped: 0, failed: 0, unavailable: 0, ...over } };
}

function parse(data) {
    if (data === undefined || data === null) return undefined;
    try {
        return JSON.parse(data);
    } catch (e) {
        return data;
    }
}

// GET /watchlist itself, not /watchlist/gone or /watchlist/reddit
const WATCHLIST = new RegExp(`^${SERVER}/watchlist$`);

module.exports = { Harness, fixture, counts, SERVER, WATCHLIST };
