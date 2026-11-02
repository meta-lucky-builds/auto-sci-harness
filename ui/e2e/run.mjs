// End-to-end: the built UI, the real local server with its scripted stand-in, in the installed Edge (no browser is
// downloaded). No agent, no store, no model. Set CHH_PY to the Python that has custom_harness.
import { spawn } from "node:child_process";
import assert from "node:assert/strict";
import { mkdirSync, statSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const out = join(here, "out");
mkdirSync(out, { recursive: true });
const PORT = 8799;
const BASE = `http://127.0.0.1:${PORT}`;
const py = process.env.CHH_PY ?? "python";

const server = spawn(py, ["-m", "custom_harness.cli", "exp", "ui", "--scripted", "--port", String(PORT)], {
  cwd: root, env: { ...process.env, PYTHONPATH: join(root, "src") }, stdio: ["ignore", "pipe", "inherit"],
});
const up = new Promise((ok, no) => {
  server.stdout.on("data", (d) => String(d).includes("The UI is at") && ok());
  server.on("exit", (c) => no(new Error(`server exited ${c}`)));
  setTimeout(() => no(new Error("the server did not start")), 60000);
});

const steps = [];
const step = async (name, fn) => { await fn(); steps.push(name); console.log(`ok - ${name}`); };
const failures = [];
// Kaspersky, on the owner's machine, rewrites plain HTTP pages in transit: it adds its script and widens the page's
// security policy to allow it (checked 2026-11-02; docs/ui.md). The server sends neither. This one host is left out by
// exact suffix, and a run says how often it was seen. tests/test_ui_rules.py checks the page as the server sends it.
const ANTIVIRUS = /^https?:\/\/[^/]+\.scr\.kaspersky-labs\.com\//;
let antivirus = 0;
const budget = { js: 400 * 1024, loadMs: 3000 };

const browser = await (async () => {
  await up;
  return chromium.launch({ channel: "msedge", headless: true });
})();

try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on("pageerror", (e) => failures.push(`page error: ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") failures.push(`console: ${m.text()}`); });
  const outside = [];
  page.on("request", (r) => {
    if (ANTIVIRUS.test(r.url())) { antivirus += 1; return; }
    if (!r.url().startsWith(BASE) && !r.url().startsWith("blob:") && !r.url().startsWith("data:")) outside.push(r.url());
  });

  await step("Today loads within the budget and says it is scripted", async () => {
    const t = Date.now();
    await page.goto(`${BASE}/`);
    await page.getByRole("heading", { name: "Today" }).waitFor();
    await page.getByText("Scripted example.").first().waitFor();
    await page.getByRole("link", { name: "s-label-smoothing" }).waitFor();
    assert.ok(Date.now() - t < budget.loadMs, `took ${Date.now() - t} ms`);
    await page.screenshot({ path: join(out, "home.png") });
  });

  await step("the Lab is alive: the Story grows by itself", async () => {
    await page.getByRole("link", { name: "s-label-smoothing" }).click();
    const lines = page.getByLabel("Story").locator("div");
    await lines.first().waitFor();
    const before = await lines.count();
    await page.waitForFunction((n) => document.querySelectorAll('[aria-label="Story"] div').length > n, before, { timeout: 15000 });
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).backgroundColor), "rgb(15, 20, 25)");
    await page.screenshot({ path: join(out, "lab.png") });
  });

  await step("a steer goes into the Story", async () => {
    await page.getByLabel("Your words go into the Story.").fill("Keep the baseline fixed.");
    await page.getByRole("button", { name: "Send" }).click();
    await page.getByLabel("Story").getByText("Keep the baseline fixed.").waitFor();
  });

  await step("the literature drawer opens", async () => {
    await page.getByRole("button", { name: "Literature" }).click();
    await page.getByText("label smoothing tabular").waitFor();
  });

  await step("every number is a link", async () => {
    await page.locator("button.num").first().click();
    await page.getByLabel("What this number rests on").waitFor();
    await page.getByRole("button", { name: "Close" }).click();
  });

  await step("Rehearse runs and exports a light figure", async () => {
    await page.goto(`${BASE}/#/rehearse`);
    await page.getByRole("button", { name: "Rehearse", exact: true }).click();
    await page.getByText("Lab data, not evidence.").first().waitFor();
    const [file] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export a light figure" }).click()]);
    assert.equal(file.suggestedFilename(), "rehearsal.svg");
  });

  await step("the Promote moment takes a preview, a tick, and one slow fade", async () => {
    await page.goto(`${BASE}/#/promote`);
    await page.getByLabel("Project").fill("tabular-rings");
    await page.getByRole("button", { name: "See what will happen" }).click();
    const go = page.getByRole("button", { name: "Promote", exact: true });
    await go.waitFor();
    assert.equal(await go.isDisabled(), true);
    await page.getByRole("checkbox").check();
    await go.click();
    await page.getByRole("heading", { name: "Fixed for the Court" }).waitFor();
    const name = await page.locator(".moment").evaluate((e) => [getComputedStyle(e).animationName, getComputedStyle(e).animationIterationCount]);
    assert.deepEqual(name, ["fade", "1"]);
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).backgroundColor), "rgb(23, 29, 36)");
  });

  await step("a verdict opens only on a press, and nothing scripted is green", async () => {
    await page.goto(`${BASE}/#/case/c-smoothing`);
    await page.getByText("Opening a verdict counts as one look").waitFor();
    assert.equal(await page.locator(".verdict").count(), 0);
    await page.getByRole("button", { name: "Open the verdict" }).click();
    await page.locator(".verdict-sentence").waitFor();
    assert.equal(await page.locator(".badge-certified").count(), 0);
    assert.ok(await page.locator(".badge-example").count() >= 1);
    const font = await page.locator(".verdict-sentence").evaluate((e) => getComputedStyle(e).fontFamily);
    assert.ok(/Georgia/.test(font), font);
    await page.screenshot({ path: join(out, "verdict.png") });
  });

  await step("a rejected case is grey; a crashed run is red", async () => {
    await page.goto(`${BASE}/#/case/c-cosine`);
    await page.getByRole("button", { name: "Open the verdict" }).click();
    const grey = await page.locator(".verdict-sentence").evaluate((e) => getComputedStyle(e).color);
    assert.equal(grey, "rgb(131, 143, 155)");
    await page.goto(`${BASE}/#/case/c-crash`);
    await page.getByRole("button", { name: "Open the verdict" }).click();
    const red = await page.locator(".verdict-sentence").evaluate((e) => getComputedStyle(e).color);
    assert.equal(red, "rgb(224, 108, 108)");
  });

  await step("the Record shows only what was opened", async () => {
    await page.goto(`${BASE}/#/record`);
    await page.getByRole("heading", { name: "Record" }).waitFor();
    await page.getByRole("cell", { name: "NOT CERTIFIED" }).waitFor();
  });

  await step("Waiting points to the step and cannot take it", async () => {
    await page.goto(`${BASE}/#/waiting`);
    await page.getByRole("button", { name: "How to do this" }).first().click();
    await page.getByLabel("Command").getByText("chh approve-role").waitFor();
    assert.equal(await page.getByRole("button", { name: /^approve/i }).count(), 0);
  });

  await step("the Trust board and the paper builder answer", async () => {
    await page.goto(`${BASE}/#/trust`);
    await page.getByText("No discovery has been certified yet.").waitFor();
    await page.goto(`${BASE}/#/paper/c-smoothing`);
    await page.getByLabel("Title").fill("A title");
    await page.getByRole("button", { name: "Build" }).click();
    await page.getByRole("alert").getByText("retired").waitFor();
  });

  await step("phone width has no sideways scroll", async () => {
    await page.setViewportSize({ width: 375, height: 800 });
    for (const h of ["#/", "#/lab", "#/record", "#/trust"]) {
      await page.goto(`${BASE}/${h}`);
      await page.getByRole("heading").first().waitFor();
      const wide = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      assert.equal(wide, false, h);
    }
  });

  await step("nothing left this machine, and the bundle is within budget", async () => {
    assert.deepEqual(outside, []);
    const assets = join(root, "ui", "dist", "assets");
    const js = readdirSync(assets).filter((f) => f.endsWith(".js")).reduce((n, f) => n + statSync(join(assets, f)).size, 0);
    assert.ok(js < budget.js, `${js} bytes of script`);
  });

  console.log("all console and page errors:", JSON.stringify(failures, null, 1));
  assert.deepEqual(failures.filter((f) => !f.includes("409") && !f.includes("kaspersky")), []);
  console.log(`${steps.length} steps passed; the antivirus tried to add its script ${antivirus} time(s)`);
} finally {
  await browser.close();
  server.kill();
}
