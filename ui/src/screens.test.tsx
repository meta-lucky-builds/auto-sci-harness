// Every screen renders from typed API answers. The stubbed `fetch` plays the local server.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Screen } from "./App";
import { lightFigure } from "./design/figure";
import { MetaContext, NumberSheet } from "./design/kit";

const num = (value: number | null) => ({ value, link: `chh://x/y/${value}` });
const readiness = { sentence: "No objection at 2000 test rows.", notice: "rough", rows: [{ test_rows: num(2000), chance: num(0.8), objection: false }] };
const answers: Record<string, unknown> = {
  waiting: { items: [{ id: "a", title: "Confirm what a test dataset is", why: "w", human_only: true, optional: false, command: "chh approve-role t --approver NAME" }], total: num(1) },
  approve: { id: "a", title: "Confirm", human_only: true, command: "chh approve-role t --approver NAME", performed: false, note: "Only a person may do this", binds: {} },
  cases: { cases: [{ case_id: "c1", title: "Label smoothing", state: "NOT CERTIFIED", looks_remaining: num(0) }], total: num(1) },
  sessions: { sessions: [{ session_id: "s1", project: "p", agent: "a", box: "running" }], total: num(1) },
  frozen: { items: [{ promotion_id: "f1", session_id: "s1", title: "Frozen one", sentence: "x" }], total: num(1) },
  lab: { session_id: "s1", project: "p", notice: "Lab work is exploration.", ideas: [{ idea_id: "i", title: "Mixup", status: "abandoned" }], open_ideas: num(0), dropped_ideas: num(1), runs: num(2), steers: num(0), web_connections: num(0), cost_usd: num(null), costs_unknown: num(1) },
  lab_log: { session_id: "s1", lines: [{ at: "10:00:00", who: "agent", kind: "run", text: "python train.py" }], reading: [{ source: "arxiv", query: "mixup", found: num(3) }], web: ["arxiv.org"] },
  record: { researcher: "r", dead_ends: [{ title: "Mixup", reason: "worse", source: "idea" }], outcomes: [], verdicts_unopened: num(0), harness_improvement: "Nothing measured yet.", calibration: { claims: num(0), mean_squared_error: num(null), evidence_bits: num(null), note: "n" } },
  trust: { rows: [{ name: "Docker", state: "ok", detail: "d", next_step: null, needed_for: "both" }], steps_left: num(0), limits: ["No discovery has been certified yet."] },
  rehearse: { promotion_id: "f1", notice: "Lab data, not evidence.", ran: true, would_certify: true, reason: null, checks: [{ name: "c", status: "pass" }], scores: [{ arm: "method", seed: "0", score: num(0.8) }, { arm: "baseline", seed: "0", score: num(0.7) }], readiness },
  promote_preview: { promotion_id: "f1", route: "sealed", finding_possible: true, readiness, looks_remaining: num(1), what_happens: ["The claim and the code are fixed for the Court."], no_data_prior: num(0.2), prior_note: "It can only make the test harder." },
  promote: { promotion_id: "f1", case_id: "c1", second_case_id: null, route: "sealed", next_step: "Ready for the trial.", command: "chh exp run c1" },
  resolve: { link: "l", value: 2, rests_on: "The session's Story.", source: {} },
};
const calls: string[] = [];

beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal("EventSource", class { addEventListener() {} close() {} });
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const name = (url.split("/api/")[1] as string).split("?")[0] as string;
    calls.push(`${init?.method ?? "GET"} ${name}`);
    if (name === "paper") return new Response(JSON.stringify({ code: "DisplayRefused", message: "It opens once the test data is retired." }), { status: 409 });
    if (name === "case") return new Response(JSON.stringify({ code: "Broken", message: "Something broke." }), { status: 500 });
    return new Response(JSON.stringify(answers[name]), { status: 200 });
  }));
});
afterEach(cleanup);

const show = (page: string, arg?: string) => render(
  <MetaContext.Provider value={{ scripted: true, notice: "n", researcher: "r" }}><NumberSheet><Screen page={page} arg={arg} /></NumberSheet></MetaContext.Provider>);

describe("screens", () => {
  it("Today lists what needs you and opens no case", async () => {
    show("");
    expect(await screen.findByText("Label smoothing")).toBeTruthy();
    expect(screen.getByText("Frozen one")).toBeTruthy();
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
  });

  it("the Lab shows ideas, the Story, runs and a literature drawer; an unknown cost stays unknown", async () => {
    show("lab", "s1");
    expect(await screen.findByText("Mixup")).toBeTruthy();
    expect((await screen.findAllByText("python train.py")).length).toBe(2);
    expect(screen.getByText("unknown")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Literature" }));
    expect(screen.getByText("mixup")).toBeTruthy();
  });

  it("every number opens what it rests on", async () => {
    show("lab", "s1");
    fireEvent.click(await screen.findByRole("button", { name: "2" }));
    expect(await screen.findByText("The session's Story.")).toBeTruthy();
  });

  it("Rehearse shows the notice, checks and a figure button", async () => {
    show("rehearse", "f1");
    fireEvent.click(await screen.findByRole("button", { name: "Rehearse" }));
    expect(await screen.findByText("Lab data, not evidence.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Export a light figure" })).toBeTruthy();
  });

  it("Promote needs the preview and a tick before it can happen", async () => {
    show("promote", "f1");
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "p" } });
    fireEvent.click(screen.getByRole("button", { name: "See what will happen" }));
    const go = await screen.findByRole<HTMLButtonElement>("button", { name: "Promote" });
    expect(go.disabled).toBe(true);
    expect(calls).not.toContain("POST promote");
    expect(screen.getByText(/It can only make the test harder/)).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(go);
    const done = await screen.findByText("Fixed for the Court");
    expect(done.closest(".moment")).toBeTruthy();
  });

  it("the verdict is read only after a press; a crash is shown as the machine's fault", async () => {
    show("case", "c1");
    await waitFor(() => expect(screen.getByRole("button", { name: "Open the verdict" })).toBeTruthy());
    expect(calls).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Open the verdict" }));
    const alert = await screen.findByRole("alert");
    expect(alert.className).toContain("broken");
  });

  it("Waiting has a way to read the step and no way to take it", async () => {
    show("waiting");
    fireEvent.click(await screen.findByRole("button", { name: "How to do this" }));
    expect(await screen.findByText("chh approve-role t --approver NAME")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(calls).toContain("GET approve");
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
  });

  it("the Record and the Trust board read plainly", async () => {
    show("record");
    expect(await screen.findByText("Mixup")).toBeTruthy();
    cleanup();
    show("trust");
    expect(await screen.findByText("No discovery has been certified yet.")).toBeTruthy();
  });

  it("the paper builder shows a refusal in plain words, as a refusal and not a breakage", async () => {
    show("paper", "c1");
    fireEvent.change(await screen.findByLabelText("Title"), { target: { value: "t" } });
    fireEvent.click(screen.getByRole("button", { name: "Build" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("retired");
    expect(alert.className).toContain("muted");
  });
});

describe("the exported figure", () => {
  it("is light, whatever the screens are", () => {
    const svg = lightFigure(answers.rehearse ? (answers.rehearse as { scores: never[] }).scores : [], "a <title>");
    expect(svg).toContain('fill="#ffffff"');
    expect(svg).toContain("&#60;title&#62;");
    expect(svg).not.toMatch(/#0f1419|#171d24|#34c9a3/i);
  });
});
