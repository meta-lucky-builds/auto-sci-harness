// Every screen renders from typed API answers. The stubbed `fetch` plays the local server.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Palette, score } from "./design/Palette";
import { glance } from "./design/Peek";
import { buildTree, layout, parseRun } from "./design/timeline";
import { Screen } from "./App";
import { lightFigure } from "./design/figure";
import { forgetReads, MetaContext, NumberSheet } from "./design/kit";

const num = (value: number | null) => ({ value, link: `chh://x/y/${value}` });
const readiness = { sentence: "No objection at 2000 test rows.", notice: "rough", rows: [{ test_rows: num(2000), chance: num(0.8), objection: false }] };
const answers: Record<string, unknown> = {
  waiting: { items: [{ id: "a", title: "Confirm what a test dataset is", why: "w", human_only: true, optional: false, command: "chh approve-role t --approver NAME" }], total: num(1) },
  approve: { id: "a", title: "Confirm", human_only: true, command: "chh approve-role t --approver NAME", performed: false, note: "Only a person may do this", binds: {} },
  cases: { cases: [{ case_id: "c1", title: "Label smoothing", state: "NOT CERTIFIED", looks_remaining: num(0) }], total: num(1) },
  sessions: { sessions: [{ session_id: "s1", project: "p", agent: "a", box: "running" }], total: num(1) },
  frozen: { items: [{ promotion_id: "f1", session_id: "s1", title: "Frozen one", sentence: "x" }], total: num(1) },
  lab: { session_id: "s1", project: "p", notice: "Lab work is exploration.", ideas: [{ idea_id: "i", title: "Mixup", status: "abandoned" }], open_ideas: num(0), dropped_ideas: num(1), runs: num(2), steers: num(0), web_connections: num(0), cost_usd: num(null), costs_unknown: num(1) },
  lab_log: { session_id: "s1", lines: [{ at: "10:00:00", who: "agent", kind: "idea", text: "propose: Mixup" }, { at: "10:00:30", who: "agent", kind: "run", text: "python train.py  accuracy 0.7 (dev)" }], reading: [{ source: "arxiv", query: "mixup", found: num(3) }], web: ["arxiv.org"] },
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
afterEach(() => { cleanup(); forgetReads(); });

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
    expect((await screen.findAllByText("python train.py")).length).toBe(1); // the run list
    expect(screen.getByText("python train.py accuracy 0.7 (dev)")).toBeTruthy(); // the Story
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
    expect(screen.getByRole("img", { name: /Score of every seed/ })).toBeTruthy();
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
    expect(calls).toContain("POST promote");
    expect(screen.getByRole("status").textContent).toContain("Nothing has been decided.");
    fireEvent.keyDown(window, { key: " " }); // skippable
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
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

describe("the reactive parts", () => {
  const lines = ["propose: A", "run|x", "branch: B", "run|y", "run|y  accuracy 0.8 (dev)", "abandon: B (worse)", "propose: C"].map((t) => {
    const r = t.split("|")[1];
    return r ? { at: "t", who: "agent" as const, kind: "run", text: r } : { at: "t", who: "agent" as const, kind: "idea", text: t };
  });

  it("the tree grows with the Story and replays from any step", () => {
    const end = buildTree(lines, lines.length);
    expect(end.nodes.map((n) => [n.title, n.status, n.parent])).toEqual([["A", "open", null], ["B", "abandoned", "A"], ["C", "open", null]]);
    expect(end.nodes[1]?.reason).toBe("worse");
    expect(end.nodes[1]?.runs).toHaveLength(1); // a start and its result are one run
    expect(end.running).toBe("A"); // x never reported
    const mid = buildTree(lines, 4);
    expect(mid.nodes.map((n) => n.status)).toEqual(["open", "open"]);
    expect(mid.running).toBe("A");
    expect(buildTree(lines, 0).nodes).toEqual([]);
  });

  it("the API's word on an idea wins at the live end, and an idea the lines miss still shows", () => {
    const t = buildTree(lines, lines.length, [{ idea_id: "c", title: "C", status: "merged" }, { idea_id: "z", title: "Z", status: "open" }]);
    expect(t.nodes.find((n) => n.title === "C")?.status).toBe("merged");
    expect(t.nodes.some((n) => n.title === "Z")).toBe(true);
  });

  it("a run line splits into command and result; the layout gives every idea a place", () => {
    expect(parseRun(0, "python a.py  accuracy 0.8312 (dev)")).toEqual({ line: 0, command: "python a.py", result: "accuracy 0.8312 (dev)", value: 0.8312 });
    expect(parseRun(0, "python a.py").result).toBeNull();
    const at = layout(buildTree(lines, lines.length), 100, 40);
    expect([...at.keys()].sort()).toEqual(["", "A", "B", "C"]);
    expect((at.get("B") as { x: number }).x).toBeGreaterThan((at.get("A") as { x: number }).x);
  });

  it("the graph labels every idea and is reachable by keyboard", async () => {
    show("lab", "s1");
    const node = await screen.findByRole("button", { name: /^Mixup\./ });
    expect(node.getAttribute("tabindex")).toBe("0");
    expect(screen.getByRole("slider", { name: "Step in this session" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "Ideas of this session" })).toBeTruthy();
  });

  it("the time slider replays the Story without calling the API", async () => {
    show("lab", "s1");
    const slider = await screen.findByRole("slider", { name: "Step in this session" });
    await screen.findByText("python train.py accuracy 0.7 (dev)");
    const before = calls.length;
    fireEvent.change(slider, { target: { value: "1" } });
    expect(screen.queryByText("python train.py accuracy 0.7 (dev)")).toBeNull();
    expect(screen.getByText("Replaying from the Story. Nothing runs.")).toBeTruthy();
    expect(calls.length).toBe(before);
    fireEvent.click(screen.getByRole("button", { name: "Live" }));
    expect(screen.getByText("python train.py accuracy 0.7 (dev)")).toBeTruthy();
  });

  it("the palette finds as you type, explains, and offers nothing that approves", async () => {
    render(<Palette onClose={() => undefined} />);
    const box = screen.getByRole("combobox", { name: "Find anything" });
    fireEvent.change(box, { target: { value: "confirm" } });
    await screen.findAllByText("Confirm what a test dataset is");
    expect((await screen.findAllByText("chh approve-role t --approver NAME")).length).toBe(1);
    expect(screen.getByText(/Run it yourself in a terminal/)).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
    expect(score("rcd", "Record")).toBeGreaterThan(0);
    expect(score("zz", "Record")).toBe(0);
  });

  it("a preview of a case shows where it stands and opens nothing", async () => {
    const g = await glance("#/case/c1");
    expect(g?.lines).toEqual(["NOT CERTIFIED", "Looks left: 0"]);
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
  });

  it("an empty Today is an invitation with one first move, not empty boxes", async () => {
    const was = { ...answers };
    Object.assign(answers, { sessions: { sessions: [], total: num(0) }, cases: { cases: [], total: num(0) }, frozen: { items: [], total: num(0) }, waiting: { items: [], total: num(0) } });
    show("");
    expect(await screen.findByText(/Nothing has/)).toBeTruthy();
    expect((await screen.findByLabelText("Command")).textContent).toContain("chh");
    Object.assign(answers, was);
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
