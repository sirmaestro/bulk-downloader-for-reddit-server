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
