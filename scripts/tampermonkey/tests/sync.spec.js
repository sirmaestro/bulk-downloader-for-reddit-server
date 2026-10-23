// The background sync: on any old.reddit page, watched users (bdfr serve's /watchlist) that are due have their newest
// posts read from Reddit with the browser's login and sent to bdfr serve. Every failure is logged as a warning.
const { test, expect } = require("@playwright/test");
const { Harness, fixture, SERVER } = require("./harness");

const SUBMITTED = (user) => new RegExp(`^https://old\\.reddit\\.com/user/${user}/submitted\\.json\\?`);
const HOUR = 3600 * 1000;

let harness;

async function setUp(context, gm = {}) {
    harness = new Harness(context, { gm });
    await harness.setUp();
    harness.defaultServe();
    // Profiles exist and aren't suspended unless a test says otherwise
    harness.on("GET", /^https:\/\/old\.reddit\.com\/user\/[\w-]+\/about\.json/, { json: { kind: "t2", data: { name: "x" } } });
}

function watch(...users) {
    harness.serve("GET", "/watchlist", {
        json: { enabled: true, users: users.map((name) => ({ name, folder: `${name} folder` })), gone: [] },
    });
}

// Reddit has page 1 and 2 of someone's posts; bdfr serve knows the kno* posts
function someoneOnReddit() {
    harness.on("GET", SUBMITTED("someone"), (request) => ({
        json: JSON.parse(fixture(request.url.includes("after=t3_new0002") ? "submitted-page2.json" : "submitted-page1.json")),
    }));
    harness.serve("POST", "/known", (request) => ({
        json: { known: request.data.ids.filter((id) => id.startsWith("kno")), known_by_link: [], duplicates: [], supported: true },
    }));
}

function jobsFor(folder) {
    return harness.requestsTo("POST", `${SERVER}/jobs`).map((r) => r.data).filter((job) => job.folder === folder);
}

// Waits for a sync to have started (it reads the watchlist first) and for no tab to be syncing
async function syncFinished(page) {
    await expect.poll(() => harness.requestsTo("GET", `${SERVER}/watchlist`).length, { timeout: 10000 }).toBeGreaterThan(0);
    await expect.poll(() => page.evaluate(() => GM_getValue("bdfr-sync-lock", null)), { timeout: 10000 }).toBeNull();
}

test.afterEach(async () => {
    expect(harness.unmatched, "requests no route answered").toEqual([]);
    expect(harness.logs.filter((log) => log.type === "pageerror")).toEqual([]);
});

test("doesn't send posts bdfr serve can't download", async ({ context }) => {
    await setUp(context);
    watch("someone");
    someoneOnReddit();
    harness.serve("POST", "/known", (request) => ({
        json: {
            known: request.data.ids.filter((id) => id.startsWith("kno")), known_by_link: [], duplicates: [],
            unavailable: request.data.ids.filter((id) => id === "new0002"), supported: true,
        },
    }));
    const page = await harness.open("/");
    await expect.poll(() => jobsFor("someone folder").length, { timeout: 10000 }).toBe(1);
    await syncFinished(page);
    expect(jobsFor("someone folder")[0].posts.map((post) => post.id)).toEqual(["new0001", "gal0002"]);
});

test("sends a watched user's new media posts, from any page, stopping at 5 known posts in a row", async ({ context }) => {
    await setUp(context);
    watch("someone");
    someoneOnReddit();
    const page = await harness.open("/");
    await expect.poll(() => jobsFor("someone folder").length, { timeout: 10000 }).toBe(1);
    await syncFinished(page);

    const job = jobsFor("someone folder")[0];
    expect(job.user).toBeUndefined(); // posts only: nothing is read from the archive
    expect(job.posts.map((post) => post.id)).toEqual(["new0001", "gal0002", "new0002"]);
    const gallery = job.posts[1];
    expect(gallery.gallery_data.items).toHaveLength(2);
    expect(gallery.media_metadata.g1bbbbbbbbbb).toEqual({ status: "valid", e: "Image", m: "image/jpg" });
    expect(job.posts[0]).toMatchObject({
        title: "Newest image", author: "someone", subreddit: "pics", created_utc: 1792600000,
        url: "https://i.redd.it/nnnnnnnnnnnn1.jpeg", upvote_ratio: 0.99,
    });

    const reads = harness.requestsTo("GET", SUBMITTED("someone"));
    expect(reads).toHaveLength(2); // page 3 isn't read: page 2 reached 5 known posts in a row
    expect(reads[0].url).toContain("sort=new");
    expect(reads[0].anonymous).toBe(false); // with the browser's login
    expect(reads[1].url).toContain("after=t3_new0002");
    expect(harness.warnings()).toEqual([]);

    // Synced now: another page load doesn't read Reddit again
    await harness.open("/");
    await expect.poll(() => harness.requestsTo("GET", `${SERVER}/watchlist`).length, { timeout: 10000 }).toBe(2);
    await syncFinished(page);
    expect(harness.requestsTo("GET", SUBMITTED("someone"))).toHaveLength(2);
});

test("nothing new: no job is sent", async ({ context }) => {
    await setUp(context);
    watch("someone");
    harness.on("GET", SUBMITTED("someone"), { json: JSON.parse(fixture("submitted-page2.json")) });
    harness.serve("POST", "/known", (request) => ({
        json: { known: request.data.ids, known_by_link: [], duplicates: [], supported: true },
    }));
    const page = await harness.open("/");
    await syncFinished(page);
    expect(harness.requestsTo("GET", SUBMITTED("someone"))).toHaveLength(1);
    expect(jobsFor("someone folder")).toEqual([]);
    expect(harness.warnings()).toEqual([]);
});

test("users synced within the interval are skipped", async ({ context }) => {
    await setUp(context, { "bdfr-sync": { someone: { lastSuccess: Date.now() - HOUR } } });
    watch("someone");
    const page = await harness.open("/");
    await syncFinished(page);
    expect(harness.requestsTo("GET", SUBMITTED("someone"))).toEqual([]);
});

test("not configured yet: no sync and no warnings", async ({ context }) => {
    await setUp(context, { "bdfr-token": "" });
    const page = await harness.open("/");
    await page.waitForTimeout(3000);
    expect(harness.requests).toEqual([]);
    expect(harness.warnings()).toEqual([]);
});

test("another tab is syncing: this one doesn't", async ({ context }) => {
    await setUp(context, { "bdfr-sync-lock": { owner: "other-tab", until: Date.now() + 10 * 60 * 1000 } });
    watch("someone");
    const page = await harness.open("/");
    await page.waitForTimeout(3000);
    expect(harness.requestsTo("GET", `${SERVER}/watchlist`)).toEqual([]);
});

test("another tab is syncing: Sync says which tab and how far it got", async ({ context }) => {
    await setUp(context, {
        "bdfr-sync-lock": {
            owner: "other-tab", until: Date.now() + 10 * 60 * 1000,
            page: "https://old.reddit.com/r/pics/?count=25", status: "Syncing u/someone (2/5)…",
        },
    });
    watch("someone");
    const page = await harness.open();
    await page.getByRole("button", { name: "Sync", exact: true }).click();
    await expect(page.getByText(
        "Already syncing in the tab on old.reddit.com/r/pics/ (its title starts with ⟳): Syncing u/someone (2/5)…",
    )).toBeVisible();
    expect(harness.requestsTo("GET", `${SERVER}/watchlist`)).toEqual([]);
});

test("the syncing tab's title is marked and its lock says where and how far, until it's done", async ({ context }) => {
    await setUp(context, { "bdfr-sync": { first: { lastSuccess: Date.now() - HOUR } } });
    watch("first");
    nothingNewOnReddit("first", 1500);
    harness.serve("POST", "/known", (request) => ({ json: { known: request.data.ids, known_by_link: [], duplicates: [], supported: true } }));
    const page = await harness.open();
    await syncFinished(page);
    await page.getByRole("button", { name: "Sync", exact: true }).click();
    await expect(page).toHaveTitle("⟳ overview for someone");
    const lock = await page.evaluate(() => GM_getValue("bdfr-sync-lock", null));
    expect(lock.page).toBe("https://old.reddit.com/user/someone/submitted/?sort=new");
    expect(lock.status).toBe("Syncing u/first (1/1)…");
    await syncFinished(page);
    await expect(page).toHaveTitle("overview for someone");
});

test("two tabs opened together sync once", async ({ context }) => {
    await setUp(context);
    watch("someone");
    someoneOnReddit();
    const first = await harness.open("/");
    await harness.open("/user/someone/submitted/?sort=new");
    await expect.poll(() => jobsFor("someone folder").length, { timeout: 10000 }).toBe(1);
    await syncFinished(first);
    await first.waitForTimeout(2500);
    expect(harness.requestsTo("GET", SUBMITTED("someone"))).toHaveLength(2);
    expect(jobsFor("someone folder")).toHaveLength(1);
});

test.describe("suspended or deleted accounts: recognised, checked again weekly", () => {
    const ABOUT = (user) => new RegExp(`^https://old\\.reddit\\.com/user/${user}/about\\.json`);
    const cases = [
        ["suspended", { status: 403, json: { reason: "suspended", message: "Forbidden", error: 403 } },
            { json: { kind: "t2", data: { name: "broken", is_suspended: true } } }],
        ["deleted", { status: 404, json: { message: "Not Found", error: 404 } }, { status: 404, json: { message: "Not Found", error: 404 } }],
    ];
    for (const [reason, posts, about] of cases) {
        test(reason, async ({ context }) => {
            await setUp(context);
            watch("broken", "someone");
            harness.on("GET", SUBMITTED("broken"), posts);
            harness.on("GET", ABOUT("broken"), about);
            someoneOnReddit();
            const page = await harness.open("/");
            await expect.poll(() => jobsFor("someone folder").length, { timeout: 10000 }).toBe(1);
            await syncFinished(page);
            expect(harness.warnings()).toContainEqual(`BDFR sync: u/broken is ${reason} on Reddit; not synced until bdfr serve lists it again`);
            // bdfr serve is told, so it leaves the user out of the watchlist
            expect(harness.requestsTo("POST", `${SERVER}/watchlist/gone`).map((r) => r.data)).toEqual([{ user: "broken", gone: reason }]);
            const state = await page.evaluate(() => GM_getValue("bdfr-sync", {}));
            expect(state.broken.gone).toBe(reason);
            expect(state.broken.lastError).toBeUndefined();

            // A bdfr serve that still lists it (an older one): not after an hour (as a failure would be), but after a week
            await page.evaluate(() => {
                const s = GM_getValue("bdfr-sync", {});
                s.broken.lastAttempt -= 2 * 3600 * 1000;
                s.someone.lastSuccess = 0;
                GM_setValue("bdfr-sync", s);
            });
            await harness.open("/");
            await expect.poll(() => harness.requestsTo("GET", `${SERVER}/watchlist`).length, { timeout: 10000 }).toBe(2);
            await syncFinished(page);
            expect(harness.requestsTo("GET", SUBMITTED("broken"))).toHaveLength(1);
            await page.evaluate(() => {
                const s = GM_getValue("bdfr-sync", {});
                s.broken.lastAttempt -= 7 * 24 * 3600 * 1000;
                GM_setValue("bdfr-sync", s);
            });
            await harness.open("/");
            await expect.poll(() => harness.requestsTo("GET", SUBMITTED("broken")).length, { timeout: 10000 }).toBe(2);
        });
    }

    test("listed again by bdfr serve for a recheck and there after all: bdfr serve is told", async ({ context }) => {
        await setUp(context);
        harness.serve("GET", "/watchlist", {
            json: { enabled: true, users: [{ name: "someone", folder: "someone folder", gone: "deleted" }], gone: [] },
        });
        someoneOnReddit();
        const page = await harness.open("/");
        await expect.poll(() => jobsFor("someone folder").length, { timeout: 10000 }).toBe(1);
        await syncFinished(page);
        expect(harness.requestsTo("POST", `${SERVER}/watchlist/gone`).map((r) => r.data)).toEqual([{ user: "someone", gone: null }]);
    });

    test("users bdfr serve leaves out as gone aren't read, and the summary counts them", async ({ context }) => {
        await setUp(context);
        harness.serve("GET", "/watchlist", {
            json: { enabled: true, users: [], gone: [{ name: "broken", gone: "suspended", recheck: Date.now() / 1000 + 86400 }] },
        });
        const page = await harness.open();
        await syncFinished(page);
        await page.getByRole("button", { name: "Sync", exact: true }).click();
        await expect(page.getByText("Synced 0 watched users · 0 new posts sent · 1 suspended or deleted not synced")).toBeVisible({ timeout: 10000 });
        expect(harness.requestsTo("GET", /old\.reddit\.com/)).toEqual([]);
    });

    test("users found gone before bdfr serve kept track are reported once, without reading Reddit", async ({ context }) => {
        await setUp(context, { "bdfr-sync": { broken: { gone: "deleted", lastAttempt: Date.now() - HOUR } } });
        watch("broken");
        const page = await harness.open("/");
        await syncFinished(page);
        expect(harness.requestsTo("POST", `${SERVER}/watchlist/gone`).map((r) => r.data)).toEqual([{ user: "broken", gone: "deleted" }]);
        expect(harness.requestsTo("GET", /old\.reddit\.com/)).toEqual([]);
        await harness.open("/");
        await expect.poll(() => harness.requestsTo("GET", `${SERVER}/watchlist`).length, { timeout: 10000 }).toBe(2);
        await syncFinished(page);
        expect(harness.requestsTo("POST", `${SERVER}/watchlist/gone`)).toHaveLength(1);
    });

    for (const [hoursAgo, read] of [[7, true], [1, false]]) {
        test(`listed by bdfr serve as gone (posted recently): synced every 6 hours; last look ${hoursAgo}h ago: ${read ? "read" : "skipped"}`, async ({ context }) => {
            await setUp(context, { "bdfr-sync": { someone: { gone: "deleted", reported: true, lastAttempt: Date.now() - hoursAgo * HOUR } } });
            harness.serve("GET", "/watchlist", {
                json: { enabled: true, users: [{ name: "someone", folder: "someone folder", gone: "deleted" }], gone: [] },
            });
            someoneOnReddit();
            const page = await harness.open("/");
            await syncFinished(page);
            expect(harness.requestsTo("GET", SUBMITTED("someone")).length > 0).toBe(read);
        });
    }

    test("a 403 from a profile that isn't suspended (e.g. private) stays a failure", async ({ context }) => {
        await setUp(context);
        watch("broken");
        harness.on("GET", SUBMITTED("broken"), { status: 403, json: { reason: "private", message: "Forbidden", error: 403 } });
        const page = await harness.open("/");
        await syncFinished(page);
        const state = await page.evaluate(() => GM_getValue("bdfr-sync", {}));
        expect(state.broken.gone).toBeUndefined();
        expect(state.broken.lastError).toContain("HTTP 403");
        expect(harness.requestsTo("GET", ABOUT("broken"))).toHaveLength(1);
    });
});

test.describe("hibernating users (no post for hibernate_days) are synced weekly; aliases never", () => {
    const DAY = 24 * HOUR;
    const cases = [
        // newest post days ago (this script's, the server's), last synced hours ago, synced again?
        [[1, null], 7, true], // active: every 6 hours
        [[61, null], 7, false], // hibernating: weekly
        [[61, null], 7 * 24 + 1, true],
        [[null, 90], 7, false], // the server's newest post counts too
        [[90, 2], 7, true], // the newer of the two
    ];
    for (const [[mine, servers], syncedHoursAgo, synced] of cases) {
        test(`newest post ${mine} / ${servers} days ago, synced ${syncedHoursAgo} hours ago: ${synced ? "synced" : "skipped"}`, async ({ context }) => {
            const now = Date.now();
            const last = { lastSuccess: now - syncedHoursAgo * HOUR };
            if (mine) last.newestPost = (now - mine * DAY) / 1000;
            await setUp(context, { "bdfr-sync": { someone: last } });
            const user = { name: "someone", folder: "someone folder" };
            if (servers) user.newest_post = (now - servers * DAY) / 1000;
            harness.serve("GET", "/watchlist", { json: { enabled: true, hibernate_days: 60, users: [user] } });
            someoneOnReddit();
            const page = await harness.open("/");
            await syncFinished(page);
            expect(harness.requestsTo("GET", SUBMITTED("someone")).length > 0).toBe(synced);
        });
    }

    test("the newest post's time is remembered", async ({ context }) => {
        await setUp(context);
        watch("someone");
        someoneOnReddit();
        const page = await harness.open("/");
        await syncFinished(page);
        const state = await page.evaluate(() => GM_getValue("bdfr-sync", {}));
        expect(state.someone.newestPost).toBe(1792600000);
    });

    test("aliases (old accounts) are never read, not even by Sync now", async ({ context }) => {
        await setUp(context);
        harness.serve("GET", "/watchlist", {
            json: { enabled: true, users: [{ name: "oldone", folder: "someone folder", alias_of: "someone" }] },
        });
        const page = await harness.open();
        await syncFinished(page);
        await page.getByRole("button", { name: "Sync", exact: true }).click();
        await expect(page.getByText("Synced 0 watched users · 0 new posts sent")).toBeVisible({ timeout: 10000 });
        expect(harness.requestsTo("GET", /old\.reddit\.com\/user\/oldone/)).toEqual([]);
    });
});

test.describe("Sync now", () => {
    test("the panel's Sync button syncs users synced recently or failed recently, and shows a summary", async ({ context }) => {
        await setUp(context, {
            "bdfr-sync": {
                someone: { lastSuccess: Date.now() - HOUR },
                broken: { lastError: "Reddit answered HTTP 429", lastAttempt: Date.now() - 60 * 1000 },
            },
        });
        watch("someone", "broken");
        someoneOnReddit();
        harness.on("GET", SUBMITTED("broken"), { status: 404, json: { message: "Not Found", error: 404 } });
        const page = await harness.open();
        await syncFinished(page);
        expect(harness.requestsTo("GET", SUBMITTED("someone"))).toEqual([]); // the background sync skipped both

        await page.getByRole("button", { name: "Sync", exact: true }).click();
        await expect(page.locator("div").filter({ hasText: /^Synced / }).last())
            .toHaveText("Synced 1 watched users · 3 new posts sent · 1 failed (console, F12)", { timeout: 10000 });
        expect(jobsFor("someone folder")).toHaveLength(1);
        expect(harness.requestsTo("GET", SUBMITTED("broken"))).toHaveLength(1);
        expect(harness.warnings()).toContainEqual(expect.stringMatching(/^BDFR sync: u\/broken.*HTTP 404/));
    });

    test("from Tampermonkey's menu on a page without the panel", async ({ context }) => {
        await setUp(context, { "bdfr-sync": { someone: { lastSuccess: Date.now() - HOUR } } });
        watch("someone");
        someoneOnReddit();
        const page = await harness.open("/");
        await syncFinished(page);
        await page.evaluate(() => __gmMenu["Sync watched users now"]());
        await expect.poll(() => jobsFor("someone folder").length, { timeout: 10000 }).toBe(1);
        await expect.poll(() => harness.logs.map((log) => log.text), { timeout: 10000 })
            .toContainEqual("BDFR sync: Synced 1 watched users · 3 new posts sent");
    });

    test("bdfr serve down: stops and says so", async ({ context }) => {
        await setUp(context, { "bdfr-sync": { someone: { lastSuccess: Date.now() - HOUR } } });
        watch("someone");
        const page = await harness.open();
        await syncFinished(page);
        harness.serve("GET", "/watchlist", { network: "error" });
        await page.getByRole("button", { name: "Sync", exact: true }).click();
        await expect(page.getByText("Couldn't read the watchlist: bdfr serve isn't running")).toBeVisible({ timeout: 10000 });
    });
});

test.describe("Reddit fails for one user: logged, the others still synced, retried later", () => {
    const cases = [
        ["logged out", { status: 200, body: fixture("reddit-login.html"), finalUrl: "https://old.reddit.com/login/?reason=lor2&dest=x" }, "Reddit asked to log in"],
        ["NSFW profile without the over-18 setting", { status: 200, body: "<html>over18</html>", finalUrl: "https://old.reddit.com/over18?dest=x" }, "over 18"],
        ["404 although the profile exists", { status: 404, json: { message: "Not Found", error: 404 } }, "HTTP 404"],
        ["private or banned profile", { status: 403, json: { reason: "banned", message: "Forbidden", error: 403 } }, "HTTP 403"],
        ["rate limited, still after retries", { status: 429, body: fixture("reddit-429.html"), headers: { "x-ratelimit-reset": "0.2" } }, "HTTP 429"],
        ["unreachable", { network: "error" }, "couldn't be reached"],
        ["too slow", { network: "timeout" }, "didn't answer in time"],
        ["not a listing", { status: 200, body: "<html>maintenance</html>" }, "didn't answer with a listing"],
    ];
    for (const [name, reply, reason] of cases) {
        test(name, async ({ context }) => {
            await setUp(context);
            watch("broken", "someone");
            harness.on("GET", SUBMITTED("broken"), reply);
            someoneOnReddit();
            const page = await harness.open("/");
            await expect.poll(() => jobsFor("someone folder").length, { timeout: 10000 }).toBe(1);
            await syncFinished(page);
            expect(jobsFor("broken folder")).toEqual([]);
            expect(harness.warnings()).toContainEqual(expect.stringMatching(new RegExp(`^BDFR sync: u/broken.*${reason}`)));
            const state = await page.evaluate(() => GM_getValue("bdfr-sync", {}));
            expect(state.broken.lastSuccess).toBeUndefined();
            expect(state.broken.lastError).toContain(reason);
            expect(state.someone.lastSuccess).toBeGreaterThan(0);
            const reads = harness.requestsTo("GET", SUBMITTED("broken")).length; // 1, or 4 with a 429's retries

            // Not retried at once on the next page load...
            await harness.open("/");
            await expect.poll(() => harness.requestsTo("GET", `${SERVER}/watchlist`).length, { timeout: 10000 }).toBe(2);
            await syncFinished(page);
            expect(harness.requestsTo("GET", SUBMITTED("broken"))).toHaveLength(reads);
        });
    }

    test("...but after an hour", async ({ context }) => {
        await setUp(context, { "bdfr-sync": { broken: { lastError: "Reddit answered HTTP 429", lastAttempt: Date.now() - HOUR - 1000 } } });
        watch("broken");
        harness.on("GET", SUBMITTED("broken"), { json: JSON.parse(fixture("submitted-page2.json")) });
        harness.serve("POST", "/known", (request) => ({ json: { known: request.data.ids, known_by_link: [], duplicates: [], supported: true } }));
        const page = await harness.open("/");
        await syncFinished(page);
        expect(harness.requestsTo("GET", SUBMITTED("broken"))).toHaveLength(1);
        const state = await page.evaluate(() => GM_getValue("bdfr-sync", {}));
        expect(state.broken.lastError).toBeUndefined();
        expect(state.broken.lastSuccess).toBeGreaterThan(0);
    });
});

test.describe("bdfr serve fails during the sync: logged", () => {
    test("the watchlist can't be read: Reddit isn't read", async ({ context }) => {
        await setUp(context);
        harness.serve("GET", "/watchlist", { network: "error" });
        const page = await harness.open("/");
        await expect.poll(() => harness.warnings(), { timeout: 10000 }).toContainEqual(expect.stringMatching(/^BDFR sync: .*watchlist.*bdfr serve isn't running/));
        await syncFinished(page);
        expect(harness.requestsTo("GET", /old\.reddit\.com/)).toEqual([]);
    });

    test("the server has no watchlist support (an older bdfr serve)", async ({ context }) => {
        await setUp(context);
        harness.serve("GET", "/watchlist", { status: 404, json: { error: "Not found" } });
        const page = await harness.open("/");
        await expect.poll(() => harness.warnings(), { timeout: 10000 }).toContainEqual(expect.stringMatching(/^BDFR sync: .*watchlist.*Not found/));
        await syncFinished(page);
    });

    test("checking known posts fails: no job for that user", async ({ context }) => {
        await setUp(context);
        watch("someone");
        someoneOnReddit();
        harness.serve("POST", "/known", { status: 500, json: { error: "database is locked" } });
        const page = await harness.open("/");
        await expect.poll(() => harness.warnings(), { timeout: 10000 }).toContainEqual(expect.stringMatching(/^BDFR sync: u\/someone.*database is locked/));
        await syncFinished(page);
        expect(jobsFor("someone folder")).toEqual([]);
    });

    test("the job is rejected", async ({ context }) => {
        await setUp(context);
        watch("someone");
        someoneOnReddit();
        harness.serve("POST", "/jobs", { status: 400, json: { error: "'posts' must be a list of 1 to 5000 posts" } });
        const page = await harness.open("/");
        await expect.poll(() => harness.warnings(), { timeout: 10000 }).toContainEqual(expect.stringMatching(/^BDFR sync: u\/someone.*must be a list/));
        await syncFinished(page);
        const state = await page.evaluate(() => GM_getValue("bdfr-sync", {}));
        expect(state.someone.lastSuccess).toBeUndefined();
    });
});

// Reddit with nothing new for the user: one page, every post already downloaded
function nothingNewOnReddit(user, delayMs = 0) {
    harness.on("GET", SUBMITTED(user), async () => {
        if (delayMs) await new Promise((resolve) => setTimeout(resolve, delayMs));
        return { json: JSON.parse(fixture("submitted-page2.json")) };
    });
}

test.describe("Reddit's rate limit", () => {
    test("a 429 is waited out and retried", async ({ context }) => {
        await setUp(context);
        watch("someone");
        someoneOnReddit();
        harness.on("GET", SUBMITTED("someone"), { status: 429, body: fixture("reddit-429.html"), headers: { "x-ratelimit-reset": "1" } }, { times: 1 });
        const page = await harness.open("/");
        await expect.poll(() => jobsFor("someone folder").length, { timeout: 10000 }).toBe(1);
        await syncFinished(page);
        const reads = harness.requestsTo("GET", SUBMITTED("someone"));
        expect(reads).toHaveLength(3); // the 429, then pages 1 and 2
        expect(reads[1].url).toBe(reads[0].url);
        expect(harness.warnings()).toEqual(["BDFR sync: Reddit answered HTTP 429 (rate limited); waiting 1s"]);
        const state = await page.evaluate(() => GM_getValue("bdfr-sync", {}));
        expect(state.someone.lastSuccess).toBeGreaterThan(0);
    });

    test("an almost used-up window is waited out before the next request", async ({ context }) => {
        await setUp(context);
        watch("someone");
        const times = [];
        harness.on("GET", SUBMITTED("someone"), (request) => {
            times.push(Date.now());
            const page = request.url.includes("after=t3_new0002") ? "submitted-page2.json" : "submitted-page1.json";
            return { json: JSON.parse(fixture(page)), headers: { "x-ratelimit-remaining": "2.0", "x-ratelimit-used": "98", "x-ratelimit-reset": "2" } };
        });
        harness.serve("POST", "/known", (request) => ({
            json: { known: request.data.ids.filter((id) => id.startsWith("kno")), known_by_link: [], duplicates: [], supported: true },
        }));
        const page = await harness.open("/");
        await expect.poll(() => jobsFor("someone folder").length, { timeout: 10000 }).toBe(1);
        await syncFinished(page);
        expect(times).toHaveLength(2);
        expect(times[1] - times[0]).toBeGreaterThanOrEqual(1900);
        expect(harness.logs.map((log) => log.text)).toContainEqual(expect.stringMatching(/^BDFR sync: waiting 2s for Reddit's rate limit/));
    });
});

test.describe("an unfinished Sync now is carried on", () => {
    test("users it already synced are skipped; failed ones tried again", async ({ context }) => {
        const now = Date.now();
        await setUp(context, {
            "bdfr-sync-run": { started: now - HOUR },
            "bdfr-sync": {
                done: { lastSuccess: now - HOUR / 2 },
                failed: { lastSuccess: now - 2 * HOUR, lastError: "Reddit answered HTTP 500", lastAttempt: now - HOUR / 2 },
                todo: { lastSuccess: now - 2 * HOUR },
            },
        });
        watch("done", "failed", "todo");
        for (const user of ["done", "failed", "todo"]) nothingNewOnReddit(user);
        harness.serve("POST", "/known", (request) => ({ json: { known: request.data.ids, known_by_link: [], duplicates: [], supported: true } }));
        const page = await harness.open();
        await syncFinished(page);
        expect(harness.requestsTo("GET", SUBMITTED("todo"))).toEqual([]); // the background sync: not due yet
        await page.getByRole("button", { name: "Sync", exact: true }).click();
        await expect(page.locator("div").filter({ hasText: /^Synced / }).last())
            .toHaveText("Synced 2 watched users · 0 new posts sent · 1 already synced by the unfinished sync", { timeout: 10000 });
        expect(harness.requestsTo("GET", SUBMITTED("done"))).toEqual([]);
        expect(harness.requestsTo("GET", SUBMITTED("failed"))).toHaveLength(1);
        expect(harness.requestsTo("GET", SUBMITTED("todo"))).toHaveLength(1);
        expect(await page.evaluate(() => GM_getValue("bdfr-sync-run", null))).toBeNull(); // finished

        // The next Sync now starts from the top
        await page.getByRole("button", { name: "Sync", exact: true }).click();
        await expect(page.locator("div").filter({ hasText: /^Synced / }).last())
            .toHaveText("Synced 3 watched users · 0 new posts sent", { timeout: 10000 });
    });

    test("one older than a day starts from the top", async ({ context }) => {
        const now = Date.now();
        await setUp(context, {
            "bdfr-sync-run": { started: now - 25 * HOUR },
            "bdfr-sync": { done: { lastSuccess: now - HOUR } },
        });
        watch("done");
        nothingNewOnReddit("done");
        harness.serve("POST", "/known", (request) => ({ json: { known: request.data.ids, known_by_link: [], duplicates: [], supported: true } }));
        const page = await harness.open();
        await syncFinished(page);
        await page.getByRole("button", { name: "Sync", exact: true }).click();
        await expect(page.locator("div").filter({ hasText: /^Synced / }).last())
            .toHaveText("Synced 1 watched users · 0 new posts sent", { timeout: 10000 });
    });

    test("pressing Sync again stops after the current user; the next Sync carries on", async ({ context }) => {
        const now = Date.now();
        await setUp(context, { "bdfr-sync": { first: { lastSuccess: now - HOUR }, second: { lastSuccess: now - HOUR } } });
        watch("first", "second");
        nothingNewOnReddit("first", 1500);
        nothingNewOnReddit("second");
        harness.serve("POST", "/known", (request) => ({ json: { known: request.data.ids, known_by_link: [], duplicates: [], supported: true } }));
        const page = await harness.open();
        await syncFinished(page);
        const sync = page.getByRole("button", { name: "Sync", exact: true });
        await sync.click();
        await expect.poll(() => harness.requestsTo("GET", SUBMITTED("first")).length, { timeout: 10000 }).toBe(1);
        await sync.click();
        await expect(page.getByText(/^Stopping after this user/)).toBeVisible();
        await expect(page.getByText("Sync stopped: 1 of 2 users done · 0 new posts sent · Sync carries on from u/second"))
            .toBeVisible({ timeout: 10000 });
        expect(harness.requestsTo("GET", SUBMITTED("second"))).toEqual([]);
        expect(await page.evaluate(() => GM_getValue("bdfr-sync-run", null))).not.toBeNull();

        await sync.click();
        await expect(page.locator("div").filter({ hasText: /^Synced / }).last())
            .toHaveText("Synced 1 watched users · 0 new posts sent · 1 already synced by the unfinished sync", { timeout: 10000 });
        expect(harness.requestsTo("GET", SUBMITTED("first"))).toHaveLength(1);
        expect(harness.requestsTo("GET", SUBMITTED("second"))).toHaveLength(1);
    });
});
