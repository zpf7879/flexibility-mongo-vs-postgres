"use strict";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const $ = (sel, el = document) => el.querySelector(sel);

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (n) => (n == null ? "?" : Number(n).toLocaleString());

function saved(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v == null ? fallback : JSON.parse(v);
  } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ }
}

async function post(url, body = {}) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (j.procs) mergeProcs(j.procs);
  if (!r.ok || j.ok === false) throw new Error(j.error || `${r.status} ${r.statusText}`);
  return j;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const M = "mongodb", P = "postgres";
const DB_NAME = { mongodb: "MongoDB", postgres: "PostgreSQL" };
const SERVICES = new Set(["microservice_one.py", "microservice_two.py", "microservice_two_v2.py"]);

const S = {
  procs: {},          // "db/script.py" -> {running, code, ch}
  status: null,       // {mongodb: {...}, postgres: {...}}
  step: saved("demo.step", 0),
  visited: new Set(saved("demo.visited", [0])),
  locksOn: false,
};

const isRunning = (key) => !!(S.procs[key] && S.procs[key].running);
const channelOf = (db, script) => (SERVICES.has(script) ? `${db}:${script.replace(".py", "")}` : `${db}:scripts`);
const channelBusy = (ch) => Object.values(S.procs).some((p) => p.running && p.ch === ch);
const runningServices = (db) =>
  [...SERVICES].filter((s) => isRunning(`${db}/${s}`)).map((s) => s);

function mergeProcs(procs) {
  for (const [k, v] of Object.entries(procs)) S.procs[k] = v;
  renderAll();
}

// What the data in each database looks like right now.
function facts(db) {
  const st = (S.status && S.status[db]) || {};
  if (db === M) {
    return {
      ok: st.ok, err: st.err || (S.status && !st.uri_set ? "MONGODB_URI is not set" : null),
      loaded: (st.count || 0) > 0,
      v1: (st.with_department || 0) > 0,
      v2: (st.hobbies_objects || 0) > 0,
      st,
    };
  }
  return {
    ok: st.ok, err: st.err,
    loaded: !!st.table && (st.count == null || st.count > 0),
    v1: !!st.department_column,
    v2: !!st.hobbies_table,
    hobbiesColumn: !!st.hobbies_column,
    st,
  };
}

// ---------------------------------------------------------------------------
// Guards: catch the usual mistakes before a script runs. Returns a warning,
// or null when the script can just run.
// ---------------------------------------------------------------------------

function guard({ db, script, args = [] }) {
  const f = facts(db);
  const stepId = STEPS[S.step].id;
  const name = DB_NAME[db];
  if (!f.ok) return `${name} isn't reachable right now (${f.err || "no status yet"}), so this will probably fail.`;

  const services = runningServices(db).filter((s) => s !== script);
  switch (script) {
    case "create_model.py":
      if (services.length) return `${name}: ${services.join(", ")} ${services.length > 1 ? "are" : "is"} running. Reloading drops the data underneath ${services.length > 1 ? "them" : "it"}; stop ${services.length > 1 ? "them" : "it"} first unless you mean to.`;
      if (f.loaded) return `${name} already has ${fmt(f.st.count)} employees. This drops and reloads them all, undoing changes v1 and v2.`;
      return null;
    case "clean_environment.py":
      if (services.length) return `${name}: ${services.join(", ")} still running and will start failing once the data is gone.`;
      return null;
    case "microservice_one.py":
      if (!f.loaded) return `${name} has no employees yet, so microservice_one.py has nothing to read and will exit. Run Step 1 first.`;
      return null;
    case "microservice_two.py":
      if (db === P) {
        if (f.loaded && !f.v1 && stepId !== "early")
          return "The department column doesn't exist yet, so this fails with UndefinedColumn. That failure is the point of Step 3, but this isn't Step 3.";
        if (f.v1 && !f.hobbiesColumn)
          return "The hobbies column has been dropped (--contract), so this v1 service will fail with UndefinedColumn.";
        if (isRunning("postgres/microservice_two_v2.py"))
          return "microservice_two_v2.py is already running. This starts the old v1 reader next to it.";
      }
      return null;
    case "microservice_two_v2.py":
      if (!f.v2) return "employee_hobbies doesn't exist yet, so this will fail. Run alter_model_v2.py first (Step 7).";
      return null;
    case "alter_model.py":
      if (!f.loaded) return `${name} has no employees yet. Run create_model.py first (Step 1).`;
      if (f.v1) return `Change v1 is already applied in ${name}. To start over, run Step 1 again first.`;
      return null;
    case "alter_model_v2.py":
      if (!f.v1) return `Change v1 hasn't been applied in ${name} yet. Run alter_model.py first (Step 4).`;
      if (args.includes("--contract")) {
        if (!f.v2) return "The child table doesn't exist yet. Run alter_model_v2.py without --contract first.";
        if (!f.hobbiesColumn) return "The hobbies column is already gone. Nothing to contract.";
        if (isRunning("postgres/microservice_two.py"))
          return "microservice_two.py (v1) is still running and reads the hobbies array, so dropping the column makes it fail with UndefinedColumn. That's why the cleanup has to wait for every old deploy, so run it anyway if you want to show that.";
        return null;
      }
      if (args.includes("--convert-all")) return null;
      if (db === P && f.v2) return "Change v2 has already run in PostgreSQL (employee_hobbies exists), so the script will stop. Run Step 1 and Step 4 again to start over.";
      if (db === M && f.v2) return "About half of the hobbies are already objects. Running it again converts half of the rest.";
      return null;
    case "lock_contention_demo.py":
      if (!f.loaded) return "There's no employees table yet. Run create_model.py first (Step 1).";
      return null;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Steps: the README walkthrough, one screen per step.
// ---------------------------------------------------------------------------

const pair = (script, label, extra = {}) => ({ kind: "pair", script, label, ...extra });
const one = (db, script, label, extra = {}) => ({ kind: "one", db, script, label, ...extra });

const STEPS = [
  {
    id: "setup", n: "0", title: "Setup and pre-flight",
    lead: "Do this before the audience arrives: both status chips in the top bar should turn green.",
    points: [
      "<span class='db mongodb'>MongoDB</span>: an M10 Atlas cluster. Enter its connection string with <b>MongoDB URI</b>, unless <code>MONGODB_URI</code> was set when the server started.",
      "<span class='db postgres'>PostgreSQL</span>: Postgres 16 in Docker, on <code>localhost:5432</code>. Start it below; the first start takes a few seconds before it accepts connections.",
      "Ran the demo before? Use <b>Reset…</b> (top right) to start clean.",
    ],
    actions: [
      { kind: "uri", label: "Set the MongoDB connection string" },
      { kind: "docker", action: "up", label: "Start PostgreSQL", cmd: "docker compose up -d" },
      { kind: "docker", action: "ps", label: "Show the container status", cmd: "docker compose ps" },
    ],
  },
  {
    id: "load", n: "1", title: "Load the data",
    lead: "Both databases get the same 10,000 employees: employee number, name, gender, hire date and salary.",
    points: [
      "Open <b>Data peek</b> under each column to show the documents and the rows.",
    ],
    actions: [pair("create_model.py", "Load 10,000 employees")],
  },
  {
    id: "old", n: "2", title: "Start the old service",
    lead: "microservice_one.py only knows names and gender. Leave it running for the rest of the demo.",
    points: ["It prints five employees every five seconds, in both databases."],
    actions: [pair("microservice_one.py", "Start the old service")],
    highlight: ["mongodb:microservice_one", "postgres:microservice_one"],
  },
  {
    id: "early", n: "3", title: "Deploy the new service early",
    tag: "Part of Difference 3",
    lead: "Before changing anything, deploy the new service, which reports employees by department.",
    points: [
      "<span class='db postgres'>PostgreSQL</span> stops with <code>UndefinedColumn</code>: the schema change has to land first.",
      "<span class='db mongodb'>MongoDB</span> starts fine. Its query returns <code>[]</code>, so it waits and checks again every five seconds. <b>Leave it running.</b>",
      "On its own, a small difference. It grows with ORMs, and with several services sharing one database.",
    ],
    actions: [pair("microservice_two.py", "Deploy the new service")],
    highlight: ["mongodb:microservice_two", "postgres:microservice_two"],
  },
  {
    id: "v1", n: "4", title: "Apply change v1",
    lead: "Add a department, job title, birth date and a list of hobbies to 1,000 of the employees.",
    points: [
      "<span class='db postgres'>PostgreSQL</span> times each step: <code>ADD COLUMN</code> takes milliseconds, so there's no downtime. The other 9,000 rows now have <code>NULL</code>s.",
      "<span class='db mongodb'>MongoDB</span> has no schema step at all. In <b>Data peek</b>, some documents have the new fields and others don't.",
      "Within five seconds, the MongoDB microservice_two.py from Step 3 starts reporting, with no restart.",
      "Both create the same <code>department</code> index and store hobbies as a simple list. <b>A draw.</b>",
    ],
    actions: [pair("alter_model.py", "Apply change v1")],
    highlight: ["mongodb:microservice_two"],
  },
  {
    id: "unaffected", n: "5", title: "Check the old service",
    tag: "What's not different",
    lead: "Look at both microservice_one.py panels.",
    points: [
      "Both are still running, and their code never had to change.",
      "Say so plainly: <b>this is a draw</b>, and admitting it makes the rest of the comparison more credible.",
    ],
    actions: [],
    highlight: ["mongodb:microservice_one", "postgres:microservice_one"],
  },
  {
    id: "two", n: "6", title: "Run the new service on change v1",
    lead: "The MongoDB microservice_two.py has been running since Step 3. Start the PostgreSQL one, which can only start now.",
    points: [
      "Both print employees by department, with their hobbies.",
      "Open the two files side by side: each is one query on one table or collection. <b>Another draw</b>, and it sets up the next step.",
    ],
    actions: [one(P, "microservice_two.py", "Start the new service")],
    highlight: ["mongodb:microservice_two", "postgres:microservice_two"],
  },
  {
    id: "v2", n: "7", title: "Change v2: hobbies grow up",
    tag: "Differences 1 and 3",
    lead: "Each hobby now needs a level and a start year. Leave both microservice_two.py services running.",
    points: [
      "<span class='db mongodb'>MongoDB</span> converts about half the employees to <code>{name, level, since}</code> and leaves the rest as strings. Within five seconds microservice_two.py shows both shapes, with no restart. Show the four-line <code>describe()</code> function: that's the cost on the MongoDB side.",
      "<span class='db postgres'>PostgreSQL</span> prints the five rollout steps and runs the two a script can do: create the child table, and copy every hobby across. Point out the two it can't: deploying dual writes, and switching reads.",
      "Then play the read switch for PostgreSQL, and show <code>microservice_two_v2.py</code>'s query: <code>LEFT JOIN</code>, <code>json_agg</code>, <code>GROUP BY</code> before <code>LIMIT</code>, <code>FILTER</code>/<code>COALESCE</code>. MongoDB's microservice_two.py hasn't changed.",
    ],
    actions: [
      pair("alter_model_v2.py", "Apply change v2"),
      {
        kind: "custom", label: "Switch reads (rollout step 4)", db: P,
        cmd: "stop microservice_two.py, start microservice_two_v2.py",
        hint: "Stops the v1 reader and deploys the version that reads from the child table.",
        disabled: () => isRunning("postgres/microservice_two_v2.py"),
        run: switchReads,
      },
      one(P, "alter_model_v2.py", "Optional finale: contract", { args: ["--contract"], hint: "DROP COLUMN hobbies. If the old microservice_two.py is still running, it fails with UndefinedColumn." }),
      one(M, "alter_model_v2.py", "Optional finale: convert the rest", { args: ["--convert-all"] }),
    ],
    highlight: ["mongodb:microservice_two", "postgres:microservice_two_v2"],
  },
  {
    id: "locks", n: "8", title: "Show the lock queue",
    tag: "Difference 2",
    lead: "The lock monitor above the output replaces the psql window: it shows who is blocked by whom, every second.",
    points: [
      "Two scenarios, about ten seconds each.",
      "<b>Plain <code>ALTER TABLE</code></b>: an open transaction holds the table, the <code>ALTER</code> waits for it, and a reader stalls behind the <code>ALTER</code>. Watch the blocking chain build up in the monitor.",
      "<b>With <code>lock_timeout</code> and retry</b>: the same <code>ALTER</code> gives up after a second and retries, and the reader's longest wait drops to about a second.",
      "There's no MongoDB equivalent to run, and that's the point: neither change needed a schema change.",
    ],
    actions: [one(P, "lock_contention_demo.py", "Run the lock demo")],
    locks: true,
  },
  {
    id: "process", n: "9", title: "Close with the process",
    tag: "Difference 3",
    lead: "No script here. Ask the audience:",
    points: [
      "How does a schema change reach production in your team today? What tool, what reviews, how many environments, who coordinates the release?",
      "How many times would change v2 go through that process? (Expand, dual writes, copy, read switch, cleanup.)",
    ],
    actions: [],
  },
  {
    id: "cleanup", n: "✓", title: "Clean up",
    lead: "Stop the services and drop the demo data.",
    points: ["<b>Reset…</b> runs <code>clean_environment.py</code> in both databases, and can also remove the Postgres container and its data."],
    actions: [
      { kind: "custom", label: "Stop every running script", run: () => post("/api/stop-all").catch(showError("system")) },
      { kind: "custom", label: "Reset…", run: openReset },
    ],
  },
];

// ---------------------------------------------------------------------------
// Running things
// ---------------------------------------------------------------------------

const showError = (ch) => (e) => appendLine(ch, String(e.message || e), "err");

function confirmRun(warnings) {
  if (!warnings.length) return Promise.resolve(true);
  const dlg = $("#dlg-confirm");
  const text = $("#confirm-text");
  text.replaceChildren(warnings.length === 1 ? warnings[0] : h("ul", {}, warnings.map((w) => h("li", {}, w))));
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
}

async function runScripts(list) {
  const warnings = list.map((a) => guard(a)).filter(Boolean);
  if (!(await confirmRun(warnings))) return;
  for (const a of list) {
    try {
      await post("/api/run", a);
    } catch (e) {
      showError(channelOf(a.db, a.script))(e);
    }
  }
}

async function stopKey(key) {
  try { await post("/api/stop", { key }); } catch (e) { showError("system")(e); }
}

async function switchReads() {
  if (!(await confirmRun(guard({ db: P, script: "microservice_two_v2.py" }) ? [guard({ db: P, script: "microservice_two_v2.py" })] : []))) return;
  await stopKey("postgres/microservice_two.py");
  try { await post("/api/run", { db: P, script: "microservice_two_v2.py" }); }
  catch (e) { showError("postgres:microservice_two_v2")(e); }
}

async function docker(action) {
  try { await post("/api/docker", { action }); } catch (e) { showError("system")(e); }
}

// ---------------------------------------------------------------------------
// Terminals
// ---------------------------------------------------------------------------

const TERMS = {
  mongodb: [
    { ch: "mongodb:scripts", title: "scripts", sub: "one-off scripts run here", tall: true },
    { ch: "mongodb:microservice_one", db: M, script: "microservice_one.py", sub: "old service" },
    { ch: "mongodb:microservice_two", db: M, script: "microservice_two.py", sub: "new service" },
  ],
  postgres: [
    { ch: "postgres:scripts", title: "scripts", sub: "one-off scripts run here", tall: true },
    { ch: "postgres:microservice_one", db: P, script: "microservice_one.py", sub: "old service" },
    { ch: "postgres:microservice_two", db: P, script: "microservice_two.py", sub: "new service, v1: reads the text[] array" },
    { ch: "postgres:microservice_two_v2", db: P, script: "microservice_two_v2.py", sub: "new service, v2: reads with a JOIN" },
  ],
  system: [{ ch: "system", title: "docker compose", sub: "and server messages" }],
};

const terms = {}; // channel -> {el, body, badge, start, stop, def}
const MAX_LINES = 2000;

function buildTerms() {
  for (const [where, defs] of Object.entries(TERMS)) {
    const host = $(`#terms-${where}`);
    for (const def of defs) {
      const key = def.script ? `${def.db}/${def.script}` : null;
      const badge = h("span", { class: "badge" }, "stopped");
      const start = def.script
        ? h("button", { class: "small", onclick: () => runScripts([{ db: def.db, script: def.script }]) }, "Start")
        : null;
      const stopBtn = h("button", {
        class: "small",
        onclick: () => {
          const p = Object.entries(S.procs).find(([, v]) => v.running && v.ch === def.ch);
          if (p) stopKey(p[0]);
        },
      }, "Stop");
      const body = h("div", { class: "term-body", role: "log", "aria-live": "off" },
        h("div", { class: "empty" }, "No output yet."));
      const el = h("div", { class: "term" + (def.tall ? " tall" : "") },
        h("div", { class: "term-head" },
          h("span", { class: "title" }, def.script || def.title, h("span", { class: "sub" }, "  ·  " + def.sub)),
          badge, start, stopBtn,
          h("button", { class: "small", onclick: () => body.replaceChildren(), title: "Clear this panel" }, "Clear")),
        body);
      host.append(el);
      terms[def.ch] = { el, body, badge, start, stop: stopBtn, def, key };
    }
  }
}

function classify(text) {
  if (/Traceback|^\s*ERROR|Error:|Errno|Could not connect|UndefinedColumn|Exception|error during connect/i.test(text)) return "err";
  if (/successfully|Migration complete/i.test(text)) return "ok";
  if (/^Step \d|APP DEPLOY|Lock timeout|stalled|waiting for/i.test(text)) return "warnl";
  return "";
}

function appendLine(ch, text, cls) {
  const t = terms[ch];
  if (!t) return;
  const body = t.body;
  const empty = body.querySelector(".empty");
  if (empty) empty.remove();
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 30;
  body.append(h("div", { class: cls || classify(text) }, text || " "));
  while (body.childElementCount > MAX_LINES) body.firstChild.remove();
  if (atBottom) body.scrollTop = body.scrollHeight;
}

function renderTerms() {
  const hl = new Set(STEPS[S.step].highlight || []);
  for (const [ch, t] of Object.entries(terms)) {
    const busy = channelBusy(ch);
    const last = t.key && S.procs[t.key];
    t.badge.className = "badge" + (busy ? " running" : last && !last.running && last.code && !last.stopped ? " failed" : "");
    t.badge.textContent = busy ? "running" : last && !last.running && last.code && !last.stopped ? `exited (${last.code})` : t.key ? "stopped" : "idle";
    if (t.start) t.start.disabled = busy;
    t.stop.disabled = !busy;
    t.el.classList.toggle("highlight", hl.has(ch));
  }
}

// ---------------------------------------------------------------------------
// Live output (Server-Sent Events)
// ---------------------------------------------------------------------------

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onopen = () => setChip("stream", "ok", "Connected to the demo server");
  es.onerror = () => setChip("stream", "bad", "Lost the connection to the demo server. Is server.py still running? Retrying…");
  es.addEventListener("reset", () => {
    for (const t of Object.values(terms)) t.body.replaceChildren();
    S.procs = {};
  });
  es.onmessage = (m) => {
    const e = JSON.parse(m.data);
    if (e.type === "line") {
      appendLine(e.ch, e.text, e.cls);
    } else if (e.type === "proc") {
      S.procs[e.key] = { running: e.state === "running", code: e.code ?? null, ch: e.ch, stopped: !!e.stopped };
      renderAll();
      if (e.state === "exited") {
        refreshState();
        const db = e.key.split("/")[0];
        const peekEl = $(`.peek[data-db="${db}"]`);
        if (peekEl && peekEl.open) setTimeout(() => loadPeek(db), 300);
      }
    }
  };
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

function setChip(id, state, title) {
  const chip = $(`#chip-${id}`);
  chip.className = "chip " + state;
  chip.title = title || "";
}

let firstState = true;
let stateInFlight = false;
async function refreshState() {
  // A database that's down can take a few seconds to time out; don't let
  // the polls pile up behind it.
  if (stateInFlight) return;
  stateInFlight = true;
  try {
    const r = await fetch("/api/state");
    const j = await r.json();
    S.status = j.status;
    S.procs = j.procs;
    renderAll();
    if (firstState) {
      firstState = false;
      if (!S.status.mongodb.uri_set) openUri();
    }
  } catch { /* the event stream chip already shows the server is gone */ }
  finally { stateInFlight = false; }
}

function renderDbState() {
  if (!S.status) return;
  const m = S.status.mongodb;
  setChip("mongodb", m.ok ? "ok" : "bad", m.ok ? `Connected to ${m.host}` : m.err || "MONGODB_URI is not set");
  const mEl = $("#state-mongodb");
  if (!m.uri_set) mEl.innerHTML = "<span class='err'>No connection string yet.</span> Use <b>MongoDB URI</b> at the top.";
  else if (!m.ok) mEl.innerHTML = `<span class='err'>${esc(m.err)}</span>`;
  else if (!m.count) mEl.innerHTML = `${esc(m.host)} · no employees yet`;
  else mEl.innerHTML =
    `<b>${fmt(m.count)}</b> employees · <b>${fmt(m.with_department)}</b> with the v1 fields` +
    (m.with_department ? ` · hobbies: <b>${fmt(m.hobbies_strings)}</b> as strings, <b>${fmt(m.hobbies_objects)}</b> as objects` : "");

  const p = S.status.postgres;
  setChip("postgres", p.ok ? (p.busy === "table locked" ? "warn" : "ok") : "bad", p.ok ? `Connected to ${p.host}` : p.err);
  const pEl = $("#state-postgres");
  if (!p.ok) pEl.innerHTML = `<span class='err'>${esc(p.err || "not reachable")}</span>`;
  else if (!p.table) pEl.innerHTML = `${esc(p.host)} · no employees table yet`;
  else {
    const parts = [];
    parts.push(p.count != null ? `<b>${fmt(p.count)}</b> employees` : `employees table (<i>${esc(p.busy)}</i>)`);
    if (p.department_column) parts.push(p.with_department != null ? `<b>${fmt(p.with_department)}</b> with the v1 columns` : "v1 columns added");
    const hob = [];
    if (p.hobbies_column) hob.push("<code>text[]</code> column");
    if (p.hobbies_table) hob.push(`child table${p.hobby_rows != null ? ` (<b>${fmt(p.hobby_rows)}</b> rows)` : ""}`);
    if (hob.length) parts.push("hobbies: " + hob.join(" + "));
    pEl.innerHTML = parts.join(" · ");
  }
}

// ---------------------------------------------------------------------------
// Step navigation and the step card
// ---------------------------------------------------------------------------

function renderNav() {
  const nav = $("#steps");
  nav.replaceChildren(...STEPS.map((s, i) =>
    h("button", {
      class: [i === S.step && "active", S.visited.has(i) && "visited"].filter(Boolean).join(" "),
      onclick: () => goStep(i),
      "aria-current": i === S.step ? "step" : null,
    }, h("span", { class: "n" }, s.n), h("span", { class: "t" }, s.title))));
}

function goStep(i) {
  S.step = Math.max(0, Math.min(STEPS.length - 1, i));
  S.visited.add(S.step);
  save("demo.step", S.step);
  save("demo.visited", [...S.visited]);
  if (STEPS[S.step].locks) showLocks(true);
  lastCardSig = null;
  renderAll();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function scriptButton(db, a) {
  const key = `${db}/${a.script}`;
  const busy = SERVICES.has(a.script) ? isRunning(key) : channelBusy(`${db}:scripts`);
  return h("button", {
    class: "primary",
    disabled: busy,
    title: busy ? "Already running" : null,
    onclick: () => runScripts([{ db, script: a.script, args: a.args || [] }]),
  }, DB_NAME[db]);
}

function actionRow(a) {
  const cmd = a.cmd || (a.script ? [a.script, ...(a.args || [])].join(" ") : null);
  const label = h("span", { class: "label" }, a.label, cmd ? h("span", { class: "muted" }, "  ", h("code", {}, cmd)) : null);
  const row = h("div", { class: "action" }, label);
  const warnings = [];

  if (a.kind === "pair") {
    const mBtn = scriptButton(M, a), pBtn = scriptButton(P, a);
    const both = h("button", {
      disabled: mBtn.disabled || pBtn.disabled,
      onclick: () => runScripts([M, P].map((db) => ({ db, script: a.script, args: a.args || [] }))),
    }, "Both");
    row.append(mBtn, pBtn, both);
    for (const db of [M, P]) {
      const w = guard({ db, script: a.script, args: a.args });
      if (w) warnings.push(w);
    }
  } else if (a.kind === "one") {
    row.append(scriptButton(a.db, a));
    const w = guard({ db: a.db, script: a.script, args: a.args });
    if (w) warnings.push(w);
  } else if (a.kind === "docker") {
    row.append(h("button", { class: "primary", disabled: isRunning("system/docker"), onclick: () => docker(a.action) }, "Run"));
  } else if (a.kind === "uri") {
    row.append(h("button", { class: "primary", onclick: openUri }, "Open"));
  } else if (a.kind === "custom") {
    row.append(h("button", { class: "primary", disabled: a.disabled ? a.disabled() : false, onclick: a.run }, "Run"));
  }
  if (a.hint) row.append(h("div", { class: "hint" }, a.hint));
  for (const w of warnings) row.append(h("div", { class: "warn" }, "⚠ " + w));
  return row;
}

let lastCardSig = null;
function renderCard() {
  const s = STEPS[S.step];
  const actions = s.actions.map(actionRow);
  // Rebuild only when something visible changed, so buttons don't flicker
  // under the mouse on every status poll.
  const sig = S.step + "|" + actions.map((r) => r.outerHTML).join("");
  if (sig === lastCardSig) return;
  lastCardSig = sig;

  const card = $("#stepcard");
  card.replaceChildren(
    h("div", { class: "muted small" }, `Step ${s.n}`, s.tag ? ` · ${s.tag}` : ""),
    h("h1", {}, s.title),
    h("p", { class: "lead" }, s.lead),
    s.points.length ? h("ul", {}, s.points.map((p) => h("li", { html: p }))) : null,
    actions.length ? h("div", { class: "actions" }, actions) : null,
    h("div", { class: "stepnav" },
      h("button", { disabled: S.step === 0, onclick: () => goStep(S.step - 1) }, "← Previous"),
      h("button", { class: "primary", disabled: S.step === STEPS.length - 1, onclick: () => goStep(S.step + 1) }, "Next step →")),
  );
}

function renderAll() {
  renderNav();
  renderCard();
  renderTerms();
  renderDbState();
}

// ---------------------------------------------------------------------------
// Lock monitor
// ---------------------------------------------------------------------------

let lockTimer = null;

function showLocks(on) {
  S.locksOn = on;
  $("#lockpanel").hidden = !on;
  $("#btn-locks").classList.toggle("primary", on);
  clearInterval(lockTimer);
  if (on) {
    loadLocks();
    lockTimer = setInterval(loadLocks, 1000);
  }
}

async function loadLocks() {
  const el = $("#lock-body");
  try {
    const j = await (await fetch("/api/locks")).json();
    if (j.error) { el.innerHTML = `<p class="muted">PostgreSQL isn't reachable: ${esc(j.error)}</p>`; return; }
    if (!j.rows.length) { el.innerHTML = `<p class="muted">No other sessions connected right now.</p>`; return; }
    const cols = ["pid", "blocked_by", "state", "wait_event_type", "wait_event", "seconds", "query"];
    const head = "<tr>" + ["pid", "blocked by", "state", "wait type", "wait event", "secs", "query"].map((c) => `<th>${c}</th>`).join("") + "</tr>";
    const rows = j.rows.map((r) => {
      const cls = r.blocked_by && r.blocked_by.length ? "blocked" : r.state === "idle in transaction" ? "idle" : "";
      return `<tr class="${cls}">` + cols.map((c) => {
        let v = r[c];
        if (c === "blocked_by") v = "{" + (v || []).join(",") + "}";
        return `<td class="${c === "query" ? "mono" : ""}">${v == null ? "" : esc(v)}</td>`;
      }).join("") + "</tr>";
    }).join("");
    el.innerHTML = `<div class="tablewrap"><table>${head}${rows}</table></div>`;
  } catch {
    el.innerHTML = `<p class="muted">Can't reach the demo server.</p>`;
  }
}

// ---------------------------------------------------------------------------
// Data peek
// ---------------------------------------------------------------------------

function pgTable(columns, rows) {
  const names = columns.length ? columns.map((c) => c.name) : Object.keys(rows[0] || {});
  const types = Object.fromEntries(columns.map((c) => [c.name, c.type]));
  const head = "<tr>" + names.map((n) => `<th>${esc(n)}<span class="type">${esc(types[n] || "")}</span></th>`).join("") + "</tr>";
  const body = rows.map((r) => "<tr>" + names.map((n) => {
    const v = r[n];
    if (v == null) return `<td class="null">NULL</td>`;
    return `<td>${esc(Array.isArray(v) ? "{" + v.join(",") + "}" : v)}</td>`;
  }).join("") + "</tr>").join("");
  return `<div class="tablewrap"><table>${head}${body}</table></div>`;
}

async function loadPeek(db) {
  const el = $(`.peek[data-db="${db}"] .peek-body`);
  el.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const j = await (await fetch(`/api/peek?db=${db}`)).json();
    if (j.error) { el.innerHTML = `<p class="muted">${esc(j.error)}</p>`; return; }
    let html = "";
    if (db === M) {
      if (!j.samples.length) html = `<p class="muted">No documents yet.</p>`;
      for (const s of j.samples) html += `<div><h4>${esc(s.label)}</h4><pre>${esc(JSON.stringify(s.doc, null, 2))}</pre></div>`;
    } else {
      if (!j.columns.length) html = `<p class="muted">No employees table yet.</p>`;
      for (const s of j.samples) html += `<div><h4>employees · ${esc(s.label)}</h4>${pgTable(j.columns, s.rows)}</div>`;
      if (j.child_rows) html += `<div><h4>employee_hobbies · one employee's hobbies, one row each</h4>${pgTable(j.child_columns, j.child_rows)}</div>`;
    }
    el.innerHTML = html;
    el.append(h("div", {}, h("button", { class: "small", onclick: () => loadPeek(db) }, "Refresh")));
  } catch {
    el.innerHTML = `<p class="muted">Can't reach the demo server.</p>`;
  }
}

// ---------------------------------------------------------------------------
// Dialogs
// ---------------------------------------------------------------------------

function openUri() {
  const dlg = $("#dlg-uri");
  if (dlg.open) return;
  $("#uri-input").value = "";
  dlg.returnValue = "";
  dlg.showModal();
}

function openReset() {
  const dlg = $("#dlg-reset");
  $("#reset-docker").checked = false;
  dlg.returnValue = "";
  dlg.showModal();
}

function wireUi() {
  $("#btn-uri").onclick = openUri;
  $("#btn-locks").onclick = () => showLocks(!S.locksOn);
  $("[data-close-locks]").onclick = () => showLocks(false);
  $("#btn-stop-all").onclick = () => post("/api/stop-all").catch(showError("system"));
  $("#btn-reset").onclick = openReset;

  $("#uri-show").onchange = (e) => { $("#uri-input").type = e.target.checked ? "text" : "password"; };
  $("#dlg-uri").addEventListener("close", async () => {
    if ($("#dlg-uri").returnValue !== "ok") return;
    const uri = $("#uri-input").value.trim();
    if (!uri) return;
    try { await post("/api/mongo-uri", { uri }); } catch (e) { showError("system")(e); }
    refreshState();
  });
  $("#dlg-reset").addEventListener("close", async () => {
    if ($("#dlg-reset").returnValue !== "ok") return;
    try { await post("/api/reset", { docker: $("#reset-docker").checked }); } catch (e) { showError("system")(e); }
  });

  for (const d of document.querySelectorAll(".peek")) {
    d.addEventListener("toggle", () => { if (d.open) loadPeek(d.dataset.db); });
  }
}

// ---------------------------------------------------------------------------

if (!STEPS[S.step]) S.step = 0;
buildTerms();
wireUi();
renderAll();
if (STEPS[S.step].locks) showLocks(true);
connectEvents();
refreshState();
setInterval(refreshState, 3000);
