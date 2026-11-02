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
const budget = { js: 400 * 1024, fonts: 120 * 1024, loadMs: 3000 };

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
    assert.ok(await page.evaluate(() => document.fonts.check('16px "Inter Tight Variable"')), "the interface font loaded");
    assert.ok(Date.now() - t < budget.loadMs, `took ${Date.now() - t} ms`);
    await page.screenshot({ path: join(out, "home.png") });
  });

  await step("the Lab is alive: the Story grows by itself", async () => {
    await page.getByRole("link", { name: "s-label-smoothing" }).click();
    const lines = page.getByLabel("Story").locator("div");
    await lines.first().waitFor();
    const before = await lines.count();
    await page.waitForFunction((n) => document.querySelectorAll('[aria-label="Story"] div').length > n, before, { timeout: 15000 });
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).backgroundColor), "rgb(0, 0, 0)");
    await page.screenshot({ path: join(out, "lab.png") });
  });

  await step("the graph is live and direct: nodes grow, a node drags, a press opens it in place, the wheel zooms", async () => {
    const nodes = page.locator(".node[role=button]");
    await nodes.first().waitFor();
    const count = await nodes.count();
    await page.waitForFunction((n) => document.querySelectorAll(".node[role=button]").length > n, count, { timeout: 20000 });
    const a = nodes.first();
    const box = await a.locator(".dot").boundingBox();
    const at = [box.x + box.width / 2, box.y + box.height / 2];
    await page.mouse.move(...at); await page.mouse.down(); await page.mouse.move(at[0] + 60, at[1] + 40, { steps: 6 }); await page.mouse.up();
    await page.waitForTimeout(500);
    const moved = await a.locator(".dot").boundingBox();
    assert.ok(Math.abs(moved.x - box.x) > 20, "the node stayed where it was dragged");
    await a.click();
    await page.locator(".node-card").waitFor();
    await page.locator(".node-card").getByRole("button", { name: "Close" }).click();
    const world = () => page.locator(".graph svg > g").getAttribute("transform");
    const before = await world();
    await page.mouse.move(400, 400); await page.mouse.wheel(0, -300); await page.waitForTimeout(300);
    assert.notEqual(await world(), before);
    const fills = await page.evaluate(() => [...document.querySelectorAll(".graph *")].map((e) => getComputedStyle(e).fill + getComputedStyle(e).stroke).join());
    assert.ok(!fills.includes("rgb(63, 224, 178)"), "the graph has no green");
  });

  await step("hovering an idea lights its lines across the screen", async () => {
    await page.locator(".node[role=button]").first().hover();
    await page.waitForFunction(() => document.querySelectorAll(".line.lit").length > 0);
    await page.mouse.move(5, 5);
  });

  await step("the slider replays the session from the Story, then returns to live", async () => {
    const live = await page.locator(".line").count();
    await page.getByLabel("Step in this session").fill("3");
    assert.ok(await page.locator(".line").count() <= 3 + 1 && live > 3);
    await page.getByText("Replaying from the Story. Nothing runs.").waitFor();
    await page.getByRole("button", { name: "Live" }).click();
    assert.ok(await page.locator(".line").count() >= live);
  });

  await step("Ctrl+K finds, explains and goes; it cannot approve", async () => {
    await page.keyboard.press("Control+k");
    await page.getByRole("combobox").fill("trust");
    await page.getByRole("option", { name: /Trust board/ }).first().waitFor();
    assert.equal(await page.getByRole("dialog").getByRole("button").count(), 0);
    await page.keyboard.press("Enter");
    await page.getByRole("heading", { name: "Trust board" }).waitFor();
    await page.goBack();
    await page.locator(".node[role=button]").first().waitFor();
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
    const veil = page.locator(".moment-veil");
    await veil.waitFor();
    assert.equal(await veil.evaluate((e) => getComputedStyle(e).animationIterationCount), "1");
    await page.getByText("Fixed. Nothing has been decided.").waitFor({ state: "attached" });
    await veil.waitFor({ state: "detached", timeout: 3000 });
    await page.goto(`${BASE}/#/promote`);
    await page.reload();
    await page.getByLabel("Project").fill("tabular-rings");
    await page.getByRole("button", { name: "See what will happen" }).click();
    await page.getByRole("checkbox").check();
    await page.getByRole("button", { name: "Promote", exact: true }).click();
    await page.locator(".moment-veil").waitFor();
    await page.keyboard.press("Space"); // skippable
    await page.locator(".moment-veil").waitFor({ state: "detached", timeout: 500 });
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).backgroundColor), "rgb(0, 0, 0)");
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
    assert.ok(/Instrument Serif/.test(font), font);
    const moving = await page.evaluate(() => document.getAnimations().length);
    assert.equal(moving, 0, "nothing moves in the Court");
    await page.locator("button.num").first().hover();
    assert.ok(await page.locator(".num.lit").count() >= 1, "a number still traces");
    await page.screenshot({ path: join(out, "verdict.png") });
  });

  await step("a rejected case is grey; a crashed run is red", async () => {
    await page.goto(`${BASE}/#/case/c-cosine`);
    await page.getByRole("button", { name: "Open the verdict" }).click();
    const grey = await page.locator(".verdict-sentence").evaluate((e) => getComputedStyle(e).color);
    assert.equal(grey, "rgb(145, 155, 165)");
    await page.goto(`${BASE}/#/case/c-crash`);
    await page.getByRole("button", { name: "Open the verdict" }).click();
    const red = await page.locator(".verdict-sentence").evaluate((e) => getComputedStyle(e).color);
    assert.equal(red, "rgb(232, 112, 108)");
  });

  await step("the Record shows only what was opened", async () => {
    await page.goto(`${BASE}/#/record`);
    await page.getByRole("heading", { name: "Record" }).waitFor();
    await page.getByLabel("Search").fill("mixup");
    await page.waitForFunction(() => document.querySelectorAll(".dead li").length === 1);
    await page.getByLabel("Search").fill("");
    await page.locator(".outcomes .state", { hasText: "NOT CERTIFIED" }).waitFor();
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

  await step("reduced motion: the moment is a plain fade and the graph does not pulse", async () => {
    const calmPage = await browser.newPage({ reducedMotion: "reduce", viewport: { width: 1280, height: 800 } });
    await calmPage.goto(`${BASE}/#/promote`);
    await calmPage.getByLabel("Project").fill("tabular-rings");
    await calmPage.getByRole("button", { name: "See what will happen" }).click();
    await calmPage.getByRole("checkbox").check();
    await calmPage.getByRole("button", { name: "Promote", exact: true }).click();
    await calmPage.locator(".moment-calm").waitFor();
    await calmPage.locator(".moment-calm").waitFor({ state: "detached", timeout: 1000 });
    await calmPage.goto(`${BASE}/#/lab`);
    await calmPage.locator(".node[role=button]").first().waitFor();
    await calmPage.waitForTimeout(600);
    assert.equal(await calmPage.locator(".pulse[opacity]").count(), 0);
    await calmPage.close();
  });

  await step("phone width has no sideways scroll", async () => {
    await page.setViewportSize({ width: 375, height: 800 });
    for (const h of ["#/", "#/lab", "#/story", "#/rehearse", "#/promote", "#/waiting", "#/record", "#/trust", "#/paper"]) {
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
    const fonts = readdirSync(assets).filter((f) => f.endsWith(".woff2")).reduce((n, f) => n + statSync(join(assets, f)).size, 0);
    assert.ok(fonts < budget.fonts, `${fonts} bytes of fonts`);
  });

  console.log("all console and page errors:", JSON.stringify(failures, null, 1));
  assert.deepEqual(failures.filter((f) => !f.includes("409") && !f.includes("kaspersky")), []);
  console.log(`${steps.length} steps passed; the antivirus tried to add its script ${antivirus} time(s)`);
} finally {
  await browser.close();
  server.kill();
}
