// bdfr-export.user.js on a user page: what it sends, and that every network failure is logged to the console as a
// "BDFR..." warning (or error) besides the panel's status, never only swallowed.
const { test, expect } = require("@playwright/test");
const { Harness, fixture, counts, SERVER } = require("./harness");

const BY_ID = /^https:\/\/old\.reddit\.com\/by_id\//;

let harness;

test.beforeEach(async ({ context }) => {
    harness = new Harness(context);
    await harness.setUp();
    harness.defaultServe();
});

test.afterEach(async () => {
    expect(harness.unmatched, "requests no route answered").toEqual([]);
    expect(harness.logs.filter((log) => log.type === "pageerror")).toEqual([]);
});

function status(page) {
    return page.locator("body > div[style*='position: fixed'] > div").nth(1);
}

async function download(page) {
    await page.getByRole("button", { name: "Download", exact: true }).click();
}

function sentJob() {
    const jobs = harness.requestsTo("POST", `${SERVER}/jobs`);
    expect(jobs).toHaveLength(1);
    return jobs[0].data;
}

test.describe("downloading a user page", () => {
    test("sends the page's media posts with gallery data, skipping known and self posts", async () => {
        harness.serve("POST", "/known", { json: { known: ["old0001"], known_by_link: [], duplicates: [], supported: true } });
        harness.on("GET", BY_ID, { json: JSON.parse(fixture("by_id-gallery.json")) });
        harness.serve("GET", "/jobs/job1", {
            json: { id: "job1", state: "done", ...counts({ new: 3 }), results: { img0001: "new", gal0001: "new", vid0001: "new" } },
        });
        const page = await harness.open();
        await expect(page.locator("#thing_t3_old0001 .bdfr-mark")).toHaveText("✔");
        await download(page);
        await expect(status(page)).toHaveText(/^Done 3 · 3 new/);
        const job = sentJob();
        expect(job.folder).toBe("someone");
        expect(job.user).toBeUndefined(); // the page reaches an already-downloaded post
        expect(job.posts.map((post) => post.id)).toEqual(["img0001", "gal0001", "vid0001"]);
        const gallery = job.posts.find((post) => post.id === "gal0001");
        expect(gallery.gallery_data.items).toHaveLength(2);
        expect(Object.keys(gallery.media_metadata)).toEqual(["m1aaaaaaaaaa", "m2aaaaaaaaaa"]);
        expect(harness.requestsTo("GET", BY_ID)[0].url).toContain("t3_gal0001");
        expect(harness.warnings()).toEqual([]);
    });
});

test.describe("job results", () => {
    test("failed posts are marked and counted apart from skipped ones; unknown labels show as skipped", async () => {
        harness.on("GET", BY_ID, { json: JSON.parse(fixture("by_id-gallery.json")) });
        harness.serve("GET", "/jobs/job1", {
            json: { id: "job1", state: "done", ...counts({ new: 1, skipped: 1, failed: 1 }), results: { img0001: "new", gal0001: "failed", vid0001: "skipped", old0001: "something-new" } },
        });
        const page = await harness.open();
        await download(page);
        await expect(status(page)).toHaveText(/^Done 3 · 1 new · 0 had · 1 skipped · 1 failed/);
        await expect(page.locator("#thing_t3_gal0001 .bdfr-mark")).toHaveText("✖");
        await expect(page.locator("#thing_t3_vid0001 .bdfr-mark")).toHaveText("!");
        await expect(page.locator("#thing_t3_old0001 .bdfr-mark")).toHaveText("!");
        expect(harness.warnings()).toEqual([]);
    });
});

test.describe("reading galleries from Reddit fails", () => {
    const cases = [
        ["logged out (redirected to the login page)", {
            status: 200, body: fixture("reddit-login.html"),
            finalUrl: "https://old.reddit.com/login/?reason=lor2&dest=https%3A%2F%2Fold.reddit.com%2Fby_id%2Ft3_gal0001.json",
        }, "Reddit asked to log in"],
        ["rate limited", { status: 429, body: fixture("reddit-429.html") }, "Reddit answered HTTP 429"],
        ["unreachable", { network: "error" }, "Reddit couldn't be reached"],
        ["too slow", { network: "timeout" }, "Reddit didn't answer in time"],
        ["not JSON", { status: 200, body: "<html>maintenance</html>" }, "Reddit didn't answer with post data"],
    ];
    for (const [name, reply, note] of cases) {
        test(`${name}: logged, and the gallery is still sent for the archive to look up`, async () => {
            harness.on("GET", BY_ID, reply);
            const page = await harness.open();
            await download(page);
            await expect(status(page)).toHaveText(/^Done/);
            const gallery = sentJob().posts.find((post) => post.id === "gal0001");
            expect(gallery.bdfr_note).toBe(note);
            expect(gallery.gallery_data).toBeUndefined();
            expect(harness.warnings().some((text) => text.includes("galleries") && text.includes(note))).toBe(true);
        });
    }
});

test.describe("talking to bdfr serve fails", () => {
    test("unreachable when the page loads", async () => {
        harness.serve("POST", "/known", { network: "error" });
        const page = await harness.open();
        await expect(status(page)).toHaveText("bdfr serve isn't running");
        await expect.poll(() => harness.warnings()).toContainEqual(expect.stringMatching(/POST \/known.*couldn't be reached/));
    });

    test("too slow when the page loads", async () => {
        harness.serve("POST", "/known", { network: "timeout" });
        const page = await harness.open();
        await expect(status(page)).toHaveText("bdfr serve isn't running");
        await expect.poll(() => harness.warnings()).toContainEqual(expect.stringMatching(/POST \/known.*didn't answer in time/));
    });

    test("wrong token", async () => {
        harness.serve("POST", "/known", { status: 401, json: { error: "Missing or wrong X-BDFR-Token" } });
        const page = await harness.open();
        await expect(status(page)).toHaveText("Wrong token (⚙)");
        await expect.poll(() => harness.warnings()).toContainEqual(expect.stringMatching(/POST \/known.*401/));
    });

    test("an answer that isn't JSON (e.g. something else on the port)", async () => {
        harness.serve("POST", "/known", { status: 200, body: "<html>router login</html>" });
        const page = await harness.open();
        await expect(status(page)).toHaveText(/^Error: .*not JSON/);
        await expect.poll(() => harness.warnings()).toContainEqual(expect.stringMatching(/POST \/known.*not JSON/));
    });

    test("the job is rejected", async () => {
        harness.on("GET", BY_ID, { json: JSON.parse(fixture("by_id-gallery.json")) });
        harness.serve("POST", "/jobs", { status: 400, json: { error: "'folder' must be a single folder name" } });
        const page = await harness.open();
        await download(page);
        await expect(status(page)).toHaveText("Error: 'folder' must be a single folder name");
        expect(harness.warnings()).toContainEqual(expect.stringMatching(/POST \/jobs.*400.*single folder name/));
    });

    test("following the job's progress fails", async () => {
        harness.on("GET", BY_ID, { json: JSON.parse(fixture("by_id-gallery.json")) });
        harness.serve("GET", "/jobs/job1", { network: "error" });
        const page = await harness.open();
        await download(page);
        await expect(status(page)).toHaveText("bdfr serve isn't running");
        expect(harness.warnings()).toContainEqual(expect.stringMatching(/GET \/jobs\/job1.*couldn't be reached/));
    });

    test("the job fails on the server (e.g. the archive was unavailable)", async () => {
        harness.on("GET", BY_ID, { json: JSON.parse(fixture("by_id-gallery.json")) });
        harness.serve("GET", "/jobs/job1", {
            json: { id: "job1", state: "failed", error: "Arctic Shift request failed after 5 attempts: HTTP 503", ...counts({ new: 1 }), results: { img0001: "new" } },
        });
        const page = await harness.open();
        await download(page);
        await expect(status(page)).toHaveText(/^Failed 1 · 1 new .* HTTP 503/);
        expect(harness.warnings()).toContainEqual(expect.stringMatching(/job1 failed.*HTTP 503/));
    });
});

test.describe("the line on what bdfr serve is doing", () => {
    function activity(page) {
        return page.locator(".bdfr-activity");
    }

    test("shows the running job and how many are queued and scheduled", async () => {
        harness.serve("GET", "/jobs", {
            json: {
                jobs: [
                    { id: "a", folder: "x", user: null, total: 3, state: "queued", scheduled: false, retry: false, created: 3, ...counts() },
                    { id: "b", folder: "y", user: "y", total: null, state: "queued", scheduled: true, retry: false, created: 2, ...counts() },
                    { id: "c", folder: "z", user: null, total: 1, state: "queued", scheduled: true, retry: true, created: 2, ...counts() },
                    { id: "d", folder: "someone", user: "someone", total: null, state: "running", scheduled: true, retry: false, created: 1, ...counts({ new: 2, existing: 5 }) },
                ],
            },
        });
        const page = await harness.open();
        await expect(activity(page)).toHaveText("Server running someone: archive (scheduled) · 7 done · 1 queued · 2 scheduled");
        expect(harness.warnings()).toEqual([]);
    });

    test("idle, and updated as jobs start", async () => {
        const page = await harness.open();
        await expect(activity(page)).toHaveText("Server idle");
        harness.on("GET", BY_ID, { json: JSON.parse(fixture("by_id-gallery.json")) });
        harness.on("GET", new RegExp(`^${SERVER}/jobs$`), {
            json: { jobs: [{ id: "job1", folder: "someone", user: null, total: 4, state: "running", scheduled: false, retry: false, created: 1, ...counts({ new: 1 }) }] },
        });
        await download(page);
        await expect(activity(page)).toHaveText("Server running someone: 4 posts · 1/4 done");
    });

    test("bdfr serve down", async () => {
        harness.serve("GET", "/jobs", { network: "error" });
        harness.serve("POST", "/known", { network: "error" });
        const page = await harness.open();
        await expect(activity(page)).toHaveText("bdfr serve isn't running");
        expect(harness.warnings()).toContainEqual(expect.stringMatching(/GET \/jobs.*couldn't be reached/));
    });

    test("an older bdfr serve without a job list shows nothing", async () => {
        harness.serve("GET", "/jobs", { status: 404, json: { error: "Not found" } });
        const page = await harness.open();
        await expect.poll(() => harness.requestsTo("GET", `${SERVER}/jobs`).length).toBe(1);
        await expect(activity(page)).toHaveText("");
    });
});

test.describe("unavailable posts", () => {
    test("are marked from /known and not sent again", async () => {
        harness.serve("POST", "/known", { json: { known: ["old0001"], known_by_link: [], duplicates: [], unavailable: ["vid0001"], supported: true } });
        harness.on("GET", BY_ID, { json: JSON.parse(fixture("by_id-gallery.json")) });
        const page = await harness.open();
        await expect(page.locator("#thing_t3_vid0001 .bdfr-mark")).toHaveText("⊘");
        await expect(page.locator("#thing_t3_vid0001 .bdfr-mark")).toHaveAttribute("title", /Can't be downloaded/);
        await download(page);
        await expect(status(page)).toHaveText(/^Done/);
        expect(sentJob().posts.map((post) => post.id)).toEqual(["img0001", "gal0001"]);
    });

    test("are marked and counted from a job's results", async () => {
        harness.serve("GET", "/jobs/job1", {
            json: { id: "job1", state: "done", ...counts({ new: 2, unavailable: 1 }), results: { img0001: "new", gal0001: "new", vid0001: "unavailable" } },
        });
        harness.on("GET", BY_ID, { json: JSON.parse(fixture("by_id-gallery.json")) });
        const page = await harness.open();
        await download(page);
        await expect(status(page)).toHaveText(/^Done 3 · 2 new .* · 1 unavailable/);
        await expect(page.locator("#thing_t3_vid0001 .bdfr-mark")).toHaveText("⊘");
    });
});

test.describe("the Status view", () => {
    function serverView(page) {
        return page.locator(".bdfr-server-view");
    }

    // The job table, one string per row: section titles, jobs as "state | folder | what | counts | duration" (the
    // time, which depends on the time zone, left out) and error lines
    function tableRows(page) {
        return serverView(page).locator("tr").evaluateAll((rows) => rows.map((row) => (row.classList.contains("bdfr-job")
            ? [...row.cells].filter((_, index) => index !== 1).map((cell) => cell.textContent).join(" | ")
            : row.textContent)));
    }

    test("shows the queue, failed posts, recent jobs and watchlist problems", async () => {
        harness.serve("GET", "/status", { json: { version: "3.1.0", running: "job2", queued: 3, retries: { waiting: 4, given_up: 1 } } });
        harness.serve("GET", "/jobs", {
            json: {
                jobs: [
                    { id: "job2", folder: "someone", user: "someone", total: null, state: "running", error: null, retry: false, created: 1790000000, ...counts({ new: 2 }) },
                    { id: "job1", folder: "other", user: null, total: 5, state: "failed", error: "Arctic Shift unavailable", retry: true, created: 1789990000, started: 1789990005, finished: 1789990070, ...counts({ existing: 3, failed: 2, unavailable: 1 }) },
                ],
            },
        });
        harness.serve("GET", "/watchlist", { json: { enabled: true, users: [{ name: "broken", last_error: "HTTP 422" }, { name: "fine" }], gone: [{ name: "left" }] } });
        const page = await harness.open();
        await expect(serverView(page)).toBeHidden();
        await page.getByRole("button", { name: "Status" }).click();
        const view = serverView(page);
        await expect(view).toContainText("bdfr serve 3.1.0 · running a job · 3 queued · failed posts: 4 to retry, 1 given up");
        await expect.poll(() => tableRows(page)).toEqual([
            "Running",
            "⋯ | someone | archive | 2 new | ",
            "Recent jobs",
            "✖ | other | 5 posts (retry) | 3 had · 2 failed · 1 unavailable | 1m 05s",
            "↳ Arctic Shift unavailable",
        ]);
        await expect(view.locator(".bdfr-job").first().locator("td").nth(1)).toHaveText(/\d:\d\d/);
        await expect(view).toContainText("Watchlist problems");
        await expect(view).toContainText("u/broken: HTTP 422");
        await expect(view).toContainText("1 watched users suspended or deleted");
        await page.getByRole("button", { name: "Status" }).click();
        await expect(view).toBeHidden();
        expect(harness.warnings()).toEqual([]);
    });

    test("lists running, waiting and finished jobs apart, waiting ones in the order they run", async () => {
        harness.serve("GET", "/status", { json: { version: "3.2.0", running: "run", queued: 3, scheduled: 2 } });
        harness.serve("GET", "/jobs", {
            json: {
                jobs: [
                    { id: "mine", folder: "clicked", user: null, total: 4, state: "queued", scheduled: false, retry: false, created: 1790000300, ...counts() },
                    { id: "retry", folder: "other", user: null, total: 2, state: "queued", scheduled: true, retry: true, created: 1790000200, ...counts() },
                    { id: "check", folder: "watched", user: "oldname", total: null, state: "queued", scheduled: true, retry: false, created: 1790000100, ...counts() },
                    { id: "run", folder: "someone", user: "someone", total: null, state: "running", scheduled: true, retry: false, created: 1790000000, ...counts({ new: 1 }) },
                    { id: "old", folder: "someone", user: null, total: 1, state: "done", scheduled: false, retry: false, created: 1789990000, started: 1789990001, finished: 1789990010, ...counts({ new: 1 }) },
                ],
            },
        });
        const page = await harness.open();
        await page.getByRole("button", { name: "Status" }).click();
        await expect(serverView(page)).toContainText("· 3 queued (2 scheduled)");
        await expect.poll(() => tableRows(page)).toEqual([
            "Running",
            "⋯ | someone | archive (scheduled) | 1 new | ",
            "Waiting",
            "◷ | clicked | 4 posts |  | ",
            "◷ | watched | archive of u/oldname (scheduled) |  | ",
            "◷ | other | 2 posts (retry) |  | ",
            "Recent jobs",
            "✔ | someone | 1 post | 1 new | 9s",
        ]);
    });

    test("an older bdfr serve without a job list", async () => {
        harness.serve("GET", "/status", { json: { version: "3.0.0", running: null, queued: 0 } });
        harness.serve("GET", "/jobs", { status: 404, json: { error: "Not found" } });
        const page = await harness.open();
        await page.getByRole("button", { name: "Status" }).click();
        await expect(serverView(page)).toContainText("bdfr serve 3.0.0 · idle · 0 queued");
        await expect(serverView(page)).toContainText("doesn't list its jobs");
        expect(harness.warnings()).toContainEqual(expect.stringMatching(/GET \/jobs.*404/));
    });

    test("bdfr serve down", async () => {
        harness.serve("GET", "/status", { network: "error" });
        const page = await harness.open();
        await page.getByRole("button", { name: "Status" }).click();
        await expect(serverView(page)).toHaveText("bdfr serve isn't running");
        expect(harness.warnings()).toContainEqual(expect.stringMatching(/GET \/status.*couldn't be reached/));
    });
});

test.describe("the panel's other buttons", () => {
    test("Collect, Export and Clear are under Extras, which stays open", async () => {
        let page = await harness.open();
        const collect = page.getByRole("button", { name: "Collect", exact: true });
        await expect(collect).toBeHidden();
        await page.getByRole("button", { name: "Extras ▸" }).click();
        await collect.click();
        await expect(page.getByRole("button", { name: /^Collecting \(\d+\)$/ })).toBeVisible();
        await expect(page.getByRole("button", { name: "Export" })).toBeVisible();
        await page.getByRole("button", { name: "Extras ▾" }).click();
        await expect(page.getByRole("button", { name: "Extras ▸ ●" })).toBeVisible(); // still collecting
        await page.getByRole("button", { name: "Extras ▸ ●" }).click();
        page = await harness.open();
        await expect(page.getByRole("button", { name: /^Collecting/ })).toBeVisible();
    });

    test("Dashboard opens the server's dashboard with the token in the fragment", async () => {
        const page = await harness.open();
        await page.evaluate(() => {
            window.__opened = [];
            window.open = (...args) => window.__opened.push(args);
        });
        await page.getByRole("button", { name: "Dashboard" }).click();
        expect(await page.evaluate(() => window.__opened)).toEqual([[`${SERVER}/#token=test-token`, "_blank", "noopener"]]);
    });
});

test.describe("www.reddit.com and multireddits", () => {
    const panel = (page) => page.locator("body > div[style*='position: fixed']");
    const multireddit = (body) => "<!doctype html><html><head><title>stuff</title></head>"
        + `<body class="listing-page loggedin">${body}</body></html>`;

    test("a user page in the old design gets the panel", async () => {
        harness.pages["https://www.reddit.com/user/someone/submitted/"] = fixture("user-page.html");
        harness.on("GET", BY_ID, { json: JSON.parse(fixture("by_id-gallery.json")) });
        const page = await harness.open("/user/someone/submitted/?sort=new", "https://www.reddit.com");
        await download(page);
        await expect(status(page)).toContainText("Done");
        const job = sentJob();
        expect(job.folder).toBe("someone");
        expect(job.posts.find((post) => post.id === "gal0001").gallery_data).toBeTruthy();
    });

    test("the new design gets no panel, but user links are rewritten, also inside shadow roots", async () => {
        harness.pages["https://www.reddit.com/user/expron/m/stuff/"] = multireddit(
            "<shreddit-app><a id='author' href='/user/poster/'>poster</a>"
            + "<a id='multi' href='/user/expron/m/stuff/'>stuff</a><div id='host'></div></shreddit-app>");
        const page = await harness.open("/user/expron/m/stuff/", "https://www.reddit.com");
        await expect(page.locator("#author")).toHaveAttribute("href",
            "https://old.reddit.com/user/poster/submitted/?sort=new");
        await expect(page.locator("#multi")).toHaveAttribute("href", "/user/expron/m/stuff/");
        const shadowHref = await page.evaluate(() => {
            const link = document.createElement("a");
            link.href = "/u/hidden";
            document.getElementById("host").attachShadow({ mode: "open" }).append(link);
            link.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, composed: true }));
            return link.href;
        });
        expect(shadowHref).toBe("https://old.reddit.com/user/hidden/submitted/?sort=new");
        await page.waitForTimeout(300);
        await expect(panel(page)).toHaveCount(0);
    });

    test("a multireddit isn't taken for its owner's user page", async () => {
        for (const path of ["/user/expron/m/stuff/", "/user/expron/m"]) {
            harness.pages[path] = multireddit("<div id='siteTable'><a id='author' "
                + "href='https://old.reddit.com/user/poster'>poster</a></div>");
            const page = await harness.open(path);
            await expect(page.locator("#author")).toHaveAttribute("href",
                "https://old.reddit.com/user/poster/submitted/?sort=new");
            await page.waitForTimeout(300);
            await expect(panel(page)).toHaveCount(0);
        }
    });
});

test.describe("the page's user on bdfr serve", () => {
    const watch = (page) => page.locator(".bdfr-watch");
    const downloads = (page) => page.locator(".bdfr-downloads");
    const USER = `${SERVER}/user?name=someone`;
    const info = (watched, over = {}) => ({
        json: { name: "someone", watchlist: true, watched, folder: (watched && watched.folder) || "someone", downloads: null, ...over },
    });

    for (const [name, reply, expected] of [
        ["not on the watchlist", info(null), /^Not watched$/],
        ["no watchlist at all", info(null, { watchlist: false }), "Not watched: bdfr serve has no watchlist"],
        ["watched", info({ name: "Someone", folder: "Someone", full_archive_done: 1 }), /^Watched · whole archive read$/],
        ["watched, whole archive stopped", info({ name: "someone", folder: "someone", full_archive_stopped: 1 }),
            /^Watched · whole archive not read \(stopped\)$/],
        ["watched into another folder, hibernating, last check failed", info({
            name: "someone", folder: "main", hibernating: true, last_error: "HTTP 422",
        }), "Watched · folder main · hibernating (no recent posts) · archive not read in full yet · last check failed: HTTP 422"],
        ["an old account", info({ name: "someone", folder: "newname", alias_of: "newname" }),
            "Watched as an old account of u/newname · folder newname"],
        ["suspended and left out of the sync", info({ name: "someone", folder: "someone", gone: "suspended", recheck: 1 }),
            "Watched · suspended on Reddit, not synced for now"],
    ]) {
        test(name, async () => {
            harness.on("GET", USER, reply);
            const page = await harness.open();
            await expect(watch(page)).toHaveText(expected);
            await expect(downloads(page)).toHaveText("Nothing downloaded yet");
        });
    }

    test("what the folder holds", async () => {
        harness.on("GET", USER, info({ name: "someone", folder: "someone" }, {
            downloads: { posts: 312, file_count: 1204, images: 980, videos: 1, bytes: 41_234_000_000 },
        }));
        const page = await harness.open();
        await expect(downloads(page)).toHaveText("312 posts · 1,204 files (980 images, 1 video) · 41.2 GB");
    });

    test("not asked on other pages", async () => {
        harness.pages["/r/pics/"] = harness.pages["/user/someone/submitted/"];
        await harness.open("/r/pics/");
        await new Promise((resolve) => setTimeout(resolve, 500));
        expect(harness.requestsTo("GET", `${SERVER}/user`)).toEqual([]);
    });

    test("an older bdfr serve without it, or one that's down, shows nothing there", async () => {
        harness.on("GET", USER, { status: 404, json: { error: "Not found" } });
        const page = await harness.open();
        await expect.poll(() => harness.warnings().some((text) => text.startsWith("BDFR: GET /user"))).toBe(true);
        await expect(watch(page)).toHaveText("");
        await expect(downloads(page)).toHaveText("");
    });
});
