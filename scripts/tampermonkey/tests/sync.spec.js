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
}

function watch(...users) {
    harness.serve("GET", "/watchlist", {
        json: { enabled: true, users: users.map((name) => ({ name, folder: `${name} folder` })) },
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

test.describe("Reddit fails for one user: logged, the others still synced, retried later", () => {
    const cases = [
        ["logged out", { status: 200, body: fixture("reddit-login.html"), finalUrl: "https://old.reddit.com/login/?reason=lor2&dest=x" }, "Reddit asked to log in"],
        ["NSFW profile without the over-18 setting", { status: 200, body: "<html>over18</html>", finalUrl: "https://old.reddit.com/over18?dest=x" }, "over 18"],
        ["deleted or suspended user", { status: 404, json: { message: "Not Found", error: 404 } }, "HTTP 404"],
        ["private or banned profile", { status: 403, json: { reason: "banned", message: "Forbidden", error: 403 } }, "HTTP 403"],
        ["rate limited", { status: 429, body: fixture("reddit-429.html") }, "HTTP 429"],
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

            // Not retried at once on the next page load...
            await harness.open("/");
            await expect.poll(() => harness.requestsTo("GET", `${SERVER}/watchlist`).length, { timeout: 10000 }).toBe(2);
            await syncFinished(page);
            expect(harness.requestsTo("GET", SUBMITTED("broken"))).toHaveLength(1);
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
