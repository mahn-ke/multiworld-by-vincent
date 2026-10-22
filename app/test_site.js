const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "site.html"), "utf8")
  .split("<script>")[1].split("</script>")[0].replace(/\nload\(\);\s*$/, "");

function setup(goals = {"1": 0, "2": 0}) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {
        hidden: false, innerHTML: "", children: [], dataset: {}, parentElement: {}, style: {}, addEventListener() {}, classes,
        classList: {
          toggle: (name, active) => active ? classes.add(name) : classes.delete(name),
          add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name),
        },
      });
    }
    return elements.get(id);
  };
  const graph = {
    seed: "test",
    players: [
      {slot: 1, name: "A", game: "Pathogenic"},
      {slot: 2, name: "B", game: "Peglin"},
    ],
    plans: {
      all: {targets: [1, 2], spheres: [], goal_sphere: goals},
      "1": {targets: [1], spheres: [], goal_sphere: goals},
      "2": {targets: [2], spheres: [], goal_sphere: goals},
    },
  };
  const checks = {
    "1": [{receiver: "B"}, {receiver: "B"}],
    "2": [{receiver: "A"}, {receiver: "B"}],
  };
  const timers = [];
  const root = element(":root");
  const context = vm.createContext({
    document: {getElementById: element, documentElement: root},
    location: {hostname: "localhost"},
    console, performance: {now: () => 0},
    setTimeout: (fn, ms) => timers.push({fn, ms}) - 1,
    clearTimeout: id => { if (timers[id]) timers[id].fn = null; },
    fetch: async url => ({ok: true, json: async () => url === "/api/graph" ? graph : checks}),
  });
  vm.runInContext(source, context);
  const flushTimers = () => timers.splice(0).forEach(timer => timer.fn?.());
  return {graph, checks, element, timers, flushTimers, run: code => vm.runInContext(code, context)};
}

test("no-logic goals hide the path but show the all-checks table under Players", async () => {
  const ui = setup();
  await ui.run("loadSeed()");
  assert.equal(ui.element("goal-path").hidden, true);
  ui.run("draw()");
  assert.equal(ui.element("graph").innerHTML, "");
  assert.match(ui.element("distribution").innerHTML, /<th[^>]*>A<\/th><td>0<\/td><td>2<\/td>/);
  assert.match(ui.element("distribution").innerHTML, /<th[^>]*>B<\/th><td>1<\/td><td>1<\/td>/);
  const html = fs.readFileSync(path.join(__dirname, "site.html"), "utf8");
  assert.match(html, /<h2>Players<\/h2>(?:(?!<\/section>)[\s\S])*id="distribution"/);
});

test("meaningful and unreachable goals stay visible without no-logic tabs", async () => {
  for (const goal of [2, null]) {
    const ui = setup({"1": 0, "2": goal});
    await ui.run("loadSeed()");
    assert.equal(ui.element("goal-path").hidden, false);
    assert.doesNotMatch(ui.element("tabs").innerHTML, /A&#39;s goal|A's goal/);
    assert.match(ui.element("tabs").innerHTML, /B's goal/);
  }
});

test("goal-path lists do not own or filter the distribution table", async () => {
  const ui = setup({"1": 1, "2": 1});
  await ui.run("loadSeed()");
  const table = ui.element("distribution").innerHTML;
  ui.run('drawLists(graph.plans["1"], graph.players, ["1"])');
  assert.equal(ui.element("distribution").innerHTML, table);
  assert.doesNotMatch(ui.element("lists").innerHTML, /Who finds what for whom/);
});

test("seed reload refreshes counts, hides trivial goals and resets a removed goal tab", async () => {
  const ui = setup({"1": 1, "2": 1});
  await ui.run("loadSeed()");
  ui.run('plan = "1"');
  ui.graph.seed = "next";
  ui.graph.plans.all.goal_sphere = {"1": 0, "2": 0};
  ui.checks["1"].push({receiver: "A"});
  await ui.run("loadSeed()");
  assert.equal(ui.element("goal-path").hidden, true);
  assert.equal(ui.run("plan"), "all");
  assert.match(ui.element("distribution").innerHTML, /<th[^>]*>A<\/th><td>1<\/td><td>2<\/td>/);
});

function headerSetup(admin, hostname = "room.local") {
  const ui = setup();
  ui.run(`location.hostname = ${JSON.stringify(hostname)}; status = {
    admin: ${admin}, server: {running: true, lan: "192.0.2.10", port: 38286},
    downloads: ["AP_test.graph.json", "AP_test_Spoiler.txt"]
  }; drawHeader();`);
  return {...ui, classes: ui.element("header").classes};
}

test("localhost sees right-side spoiler and editor, but no visitor message", () => {
  const ui = headerSetup(true, "localhost");
  assert.equal(ui.element("downloads").hidden, false);
  assert.match(ui.element("downloads").innerHTML, /Spoiler Log/);
  assert.equal(ui.element("edit-open").hidden, false);
  assert.equal(ui.element("connection-status").hidden, true);
  assert.equal(ui.element("host-status").hidden, false);
  assert.equal(ui.classes.has("online"), true);
  assert.equal(ui.element("srv").textContent, "Stop server");
  const html = fs.readFileSync(path.join(__dirname, "site.html"), "utf8");
  assert.match(html, /<h1>Multiworld<\/h1>/);
  assert.doesNotMatch(html, /id="seed"/);
  assert.match(html, /class="header-actions">\s*<div id="downloads"[^>]*hidden><\/div>\s*<button id="edit-open"/);
});

test("visitors see the browser hostname or IP with the game port and no host actions", () => {
  for (const hostname of ["room.local", "192.0.2.20", "[2001:db8::1]", ""]) {
    const ui = headerSetup(false, hostname);
    assert.equal(ui.element("downloads").hidden, true);
    assert.equal(ui.element("downloads").innerHTML, "");
    assert.equal(ui.element("edit-open").hidden, true);
    assert.equal(ui.element("host-status").hidden, true);
    assert.equal(ui.element("connection-status").hidden, false);
    assert.equal(ui.element("connection-status").innerHTML,
      `Server online! Connect to <code>${hostname || "192.0.2.10"}:38286</code> and join!`);
    ui.run("status.server.running = false; drawHeader()");
    assert.equal(ui.element("srv").textContent, "Start server");
    ui.flushTimers();
    assert.equal(ui.element("connection-status").innerHTML, "Server offline! Waiting for host to start...");
    assert.equal(ui.classes.has("offline"), true);
    assert.equal(ui.classes.has("online"), false);
    ui.run("status.server.running = true; drawHeader()");
    assert.equal(ui.classes.has("online"), true);
    assert.equal(ui.classes.has("offline"), false);
  }
});

test("a public game address replaces host and port, and an empty room says so", () => {
  const ui = headerSetup(false);
  ui.run(`status.server.address = "multiworld.example:443"; status.versions = {archipelago: "0.6.8",
    worlds: [{file: "peglin.apworld", tag: "v1.2.0"}]}; drawHeader()`);
  assert.equal(ui.element("connection-status").innerHTML,
    "Server online! Connect to <code>multiworld.example:443</code> and join!");
  assert.equal(ui.element("versions").textContent, "Archipelago 0.6.8 · peglin v1.2.0");
  ui.run("status.server.running = false; status.server.seed = null; drawHeader()");
  ui.flushTimers();
  assert.equal(ui.element("connection-status").innerHTML, "No game yet! Waiting for the host to add players...");
  assert.equal(ui.element("srv").disabled, true);
});

test("an empty room shows no players and no distribution table", async () => {
  const ui = setup();
  ui.graph.players = [];
  ui.graph.seed = null;
  await ui.run("loadSeed()");
  assert.equal(ui.element("distribution").innerHTML, "");
  ui.run(`document.querySelectorAll = () => []; status = {admin: false, server: {running: false, lan: "x", port: 1, seed: null}, players: {}}; drawCards()`);
  assert.match(ui.element("cards").innerHTML, /No players yet/);
});

test("server control sends the current seed, disables duplicate clicks and surfaces failures", async () => {
  const ui = headerSetup(true);
  await ui.run("loadSeed()");
  ui.run(`poll = async () => {}; fetch = async (url, options) => {
    if (url !== "/api/server") throw new Error("Wrong endpoint");
    const body = JSON.parse(options.body);
    if (body.action !== "stop" || body.seed !== "test") throw new Error("Wrong action or seed");
    if (!document.getElementById("srv").disabled) throw new Error("Control not disabled");
    return {ok: false, json: async () => ({error: "The room is busy"})};
  }`);
  await ui.element("srv").onclick();
  assert.equal(ui.element("server-error").textContent, "The room is busy");
  assert.equal(ui.element("srv").disabled, false);
  ui.run('status.room = {busy: true}; drawHeader()');
  assert.equal(ui.element("srv").disabled, true);
});

test("start stays locked after the room reports idle until a poll confirms the server is running", async () => {
  const ui = headerSetup(true);
  await ui.run("loadSeed()");
  ui.run("status.server.running = false; drawHeader()");
  let api = {server: {running: false, lan: "192.0.2.10", port: 38286, seed: "test"},
             room: {busy: true, phase: "starting"}, admin: true, players: {}, feed: [], log: [], downloads: []};
  ui.run(`drawCards = draw = drawFeed = drawAdmin = () => {};`);
  ui.run(`fetch = async url => url === "/api/server"
    ? {ok: true, json: async () => ({ok: true})}
    : {ok: true, json: async () => JSON.parse(globalThis.api)}`);
  const setApi = () => ui.run(`globalThis.api = ${JSON.stringify(JSON.stringify(api))}`);
  setApi();
  await ui.element("srv").onclick();
  assert.equal(ui.element("server-error").textContent, "");
  assert.equal(ui.element("srv").disabled, true);
  // The room finished (busy=false) but the server is not running yet: still locked, still "Start".
  api.room = {busy: false, phase: "running"};
  setApi();
  ui.run(`showRoom(${JSON.stringify(api.room)})`);
  assert.equal(ui.element("srv").disabled, true);
  await ui.run("poll()");
  assert.equal(ui.element("srv").disabled, true);
  assert.equal(ui.element("srv").textContent, "Start server");
  api.server.running = true;
  setApi();
  await ui.run("poll()");
  assert.equal(ui.element("srv").disabled, false);
  assert.equal(ui.element("srv").textContent, "Stop server");
});

test("state changes restart the pulse and swap the visitor text only after the pulse", () => {
  const ui = headerSetup(false);
  assert.equal(ui.classes.has("pulse"), false, "first render must not pulse");
  ui.run("status.server.running = false; drawHeader()");
  const box = ui.element("connection-status");
  assert.equal(ui.classes.has("pulse"), true);
  assert.equal(box.classes.has("fading"), true);
  assert.match(box.innerHTML, /Server online!/, "old text stays while fading out");
  ui.run("drawHeader()");
  assert.match(box.innerHTML, /Server online!/, "unchanged redraws do not swap mid-pulse");
  assert.equal(ui.timers.at(-1).ms, 1000, "new text fades in during the end of the 1.6s pulse");
  ui.flushTimers();
  assert.equal(box.innerHTML, "Server offline! Waiting for host to start...");
  assert.equal(box.classes.has("fading"), false);
  ui.run("status.server.running = true; drawHeader(); status.server.running = false; drawHeader()");
  ui.flushTimers();
  assert.equal(box.innerHTML, "Server offline! Waiting for host to start...", "latest state wins");
});

test("failed status polling marks the header offline instead of leaving stale online guidance", async () => {
  for (const failure of [
    "fetch = async () => { throw new Error('Disconnected'); }",
    "fetch = async () => ({ok: false, status: 503})",
  ]) {
    const ui = headerSetup(false);
    ui.run(`console = {warn() {}}; ${failure}`);
    await ui.run("poll()");
    assert.equal(ui.classes.has("offline"), true);
    ui.flushTimers();
    assert.equal(ui.element("connection-status").innerHTML, "Server offline! Waiting for host to start...");
  }
});

test("only visitors get the full-height closed header while the server is offline", () => {
  for (const admin of [false, true]) {
    const ui = headerSetup(admin, admin ? "localhost" : "room.local");
    const root = ui.element(":root").classes;
    assert.equal(root.has("closed"), false);
    ui.run("status.server.running = false; drawHeader()");
    assert.equal(root.has("closed"), !admin);
    ui.run("status.server.running = true; drawHeader()");
    assert.equal(root.has("closed"), false);
  }
});

test("server stop/start notices are host-only; other room notices still reach visitors", () => {
  for (const admin of [false, true]) {
    const ui = headerSetup(admin, admin ? "localhost" : "room.local");
    for (const phase of ["stopping", "starting", "stopped"]) {
      ui.run(`showRoom({busy: ${phase !== "stopped"}, phase: "${phase}", progress: 0})`);
      assert.equal(ui.element("room").hidden, !admin, phase);
    }
    ui.run(`showRoom({busy: true, phase: "generating", progress: 0})`);
    assert.equal(ui.element("room").hidden, false);
  }
});

test("host cards offer a player-has-left checkbox with a countdown bar; visitors do not", async () => {
  const ui = setup();
  await ui.run("loadSeed()");
  ui.run(`document.querySelectorAll = selector => selector === ".left-bar[data-slot]" ? globalThis.barList || [] : [];`);
  ui.run(`status = {
    admin: true, server: {running: true, lan: "192.0.2.10", port: 38286}, downloads: [],
    players: {"1": {checked: [], tracking: true}, "2": {checked: [], tracking: true}},
    left: {"1": {next_in: 75, remaining: 3}, "2": {next_in: 0, remaining: 0}}, left_interval: 90
  }; drawCards();`);
  const html = ui.element("cards").innerHTML;
  assert.match(html, /class="left-bar on" data-slot="1"><div><\/div><\/div>\s*<label class="left-toggle"><input type="checkbox" data-left="1" checked >/);
  assert.match(html, /3 check\(s\) for other players remain · next unlock in <span data-left-next="1">1 min 15 s<\/span>/);
  // Finished: locked, still checked, bar hidden.
  assert.match(html, /class="left-bar " data-slot="2">[\s\S]*data-left="2" checked disabled>[\s\S]*All their checks for other players are unlocked\./);
  assert.match(html, /class="pill left"/);
  // The countdown drains between polls and wraps to a full bar when the unlock is due.
  ui.run(`globalThis.barList = [1, 2].map(slot => { const b = {dataset: {slot: String(slot)}, firstElementChild: {style: {}}};
    b.parentElement = {querySelector: () => b.next ??= {}}; return b; });`);
  ui.run(`performance.now = () => 30000; tickLeft();`);
  assert.equal(ui.run("barList[0].firstElementChild.style.width"), `${100 * 45 / 90}%`);
  assert.equal(ui.run("barList[0].next.textContent"), "45 s");
  assert.equal(ui.run("barList[1].firstElementChild.style.width"), undefined);
  ui.run(`performance.now = () => 76000; tickLeft();`);
  assert.equal(ui.run("barList[0].firstElementChild.style.width"), `${100 * 89 / 90}%`);
  // Paused (tracker offline): the bar stays empty instead of wrapping.
  ui.run(`performance.now = () => 0; status.players["1"].tracking = false; drawCards(); performance.now = () => 76000; tickLeft();`);
  assert.equal(ui.run("barList[0].firstElementChild.style.width"), "0%");
  ui.run(`status.left = {}; drawCards();`);
  assert.match(ui.element("cards").innerHTML, /class="left-bar " data-slot="1">[\s\S]*data-left="1"  >[\s\S]*every 1 min 30 s/);
  ui.run(`status.admin = false; status.left = {"1": {next_in: 1, remaining: 1}}; drawCards();`);
  assert.doesNotMatch(ui.element("cards").innerHTML, /data-left|left-bar|Player has left|pill left/);
  assert.equal(ui.run("duration(3600)"), "1 h");
  assert.equal(ui.run("duration(1)"), "1 s");
});
