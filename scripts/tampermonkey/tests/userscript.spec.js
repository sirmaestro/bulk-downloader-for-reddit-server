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

test.describe("the Status view", () => {
    function serverView(page) {
        return page.locator(".bdfr-server-view");
    }

    test("shows the queue, failed posts, recent jobs and watchlist problems", async () => {
        harness.serve("GET", "/status", { json: { version: "3.1.0", running: "job2", queued: 3, retries: { waiting: 4, given_up: 1 } } });
        harness.serve("GET", "/jobs", {
            json: {
                jobs: [
                    { id: "job2", folder: "someone", user: "someone", total: null, state: "running", error: null, retry: false, created: 1790000000, ...counts({ new: 2 }) },
                    { id: "job1", folder: "other", user: null, total: 5, state: "failed", error: "Arctic Shift unavailable", retry: true, created: 1789990000, ...counts({ existing: 3, failed: 2 }) },
                ],
            },
        });
        harness.serve("GET", "/watchlist", { json: { enabled: true, users: [{ name: "broken", last_error: "HTTP 422" }, { name: "fine" }], gone: [{ name: "left" }] } });
        const page = await harness.open();
        await expect(serverView(page)).toBeHidden();
        await page.getByRole("button", { name: "Status" }).click();
        const view = serverView(page);
        await expect(view).toContainText("bdfr serve 3.1.0 · running a job · 3 queued · failed posts: 4 to retry, 1 given up");
        await expect(view).toContainText(/someone: u\/someone · running · 2 new/);
        await expect(view).toContainText(/other: 5 posts \(retry\) · failed · 3 had, 2 failed · Arctic Shift unavailable/);
        await expect(view).toContainText("u/broken: HTTP 422");
        await expect(view).toContainText("1 watched users suspended or deleted");
        await page.getByRole("button", { name: "Status" }).click();
        await expect(view).toBeHidden();
        expect(harness.warnings()).toEqual([]);
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
