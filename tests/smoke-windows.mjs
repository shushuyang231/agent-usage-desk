import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const [program, screenshot] = process.argv.slice(2);
assert.ok(program && process.platform === "win32", "Supply the packaged Windows EXE");

const tempRoot = await realpath(tmpdir());
const state = await mkdtemp(path.join(tempRoot, "agent-usage-smoke-"));
const codex = path.join(state, "codex");
const marker = path.join(state, "active-url.txt");
let app, socket, spawnError;

function localDay(offset) {
  const date = new Date();
  date.setHours(12, 0, 0, 0);
  date.setDate(date.getDate() + offset);
  return date;
}

function count(offset, model, input_tokens, cached_input_tokens, output_tokens) {
  const usage = {input_tokens, cached_input_tokens, output_tokens};
  return {type: "event_msg", timestamp: localDay(offset).toISOString(), payload: {
    type: "token_count", info: {model, last_token_usage: usage, total_token_usage: usage},
  }};
}

async function waitFor(action, description) {
  const deadline = Date.now() + 30000;
  let lastError;
  while (Date.now() < deadline) {
    if (spawnError) throw spawnError;
    if (app?.exitCode && app.exitCode !== 0) throw new Error(`Packaged app exited with ${app.exitCode}`);
    try {
      const value = await action();
      if (value) return value;
    } catch (error) { lastError = error; }
    await delay(180);
  }
  throw new Error(description, {cause: lastError});
}

async function request(url) {
  const response = await fetch(url, {signal: AbortSignal.timeout(2000)});
  assert.ok(response.ok, `Unexpected HTTP status ${response.status}`);
  return response.json();
}

async function testPort() {
  const configured = process.env.AGENT_USAGE_TEST_WEBVIEW_PORT;
  if (configured) {
    const port = Number(configured);
    assert.ok(Number.isInteger(port) && port >= 1024 && port <= 65535);
    return port;
  }
  const reservation = createServer().listen(0, "127.0.0.1");
  await new Promise((resolve, reject) => reservation.once("listening", resolve).once("error", reject));
  const port = reservation.address().port;
  await new Promise((resolve) => reservation.close(resolve));
  return port;
}

try {
  const sessions = path.join(codex, "sessions");
  await mkdir(sessions, {recursive: true});
  const records = [
    {type: "session_meta", payload: {id: "usage-fixture", cwd: path.join(state, "Demo project")}},
    count(-1, "gpt-6.2-sol-medium", 3_000, 2_000, 50),
    count(0, "gpt-6.2-sol-high", 300_000, 10_000, 100),
    count(0, "codex-auto-review", 19, 0, 1),
  ];
  await writeFile(path.join(sessions, "rollout-usage-fixture.jsonl"), records.map((row) => JSON.stringify(row)).join("\n"));

  const port = await testPort();
  app = spawn(path.resolve(program), ["--state", state, "--codex-root", codex, "--gemini-root", path.join(state, "agy")], {
    windowsHide: true, stdio: "ignore", env: {
      ...process.env, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${port} --disable-background-networking`,
    },
  });
  app.once("error", (error) => { spawnError = error; });
  const url = await waitFor(async () => (await readFile(marker, "utf8")).trim(), "Local dashboard failed to start");
  assert.match(url, /^http:\/\/127\.0\.0\.1:\d+\/[A-Za-z0-9_-]{32}\/$/);
  const snapshot = await waitFor(async () => (await request(new URL("api/data", url))).snapshot, "No usage snapshot");
  assert.equal(snapshot.sources.codex.read, 1);
  assert.equal(snapshot.sources.agy.state, "missing");
  assert.equal(snapshot.rows.length, 3);

  const page = await waitFor(async () => (await request(`http://127.0.0.1:${port}/json/list`)).find((entry) => entry.url === url),
    "The packaged dashboard did not load in WebView2");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, {once: true});
    socket.addEventListener("error", reject, {once: true});
  });
  let nextId = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const {id, result, error} = JSON.parse(event.data);
    pending.get(id)?.(result, error);
  });
  function command(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++nextId;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`WebView2 did not answer ${method}`));
      }, 5000);
      pending.set(id, (result, error) => {
        clearTimeout(timer);
        pending.delete(id);
        if (error) reject(new Error(error.message));
        else resolve(result);
      });
      socket.send(JSON.stringify({id, method, params}));
    });
  }
  async function run(expression) {
    const response = await command("Runtime.evaluate", {expression, returnByValue: true});
    assert.ok(!response.exceptionDetails, response.exceptionDetails?.exception?.description || "Dashboard script failed");
    return response.result.value;
  }
  const click = (selector) => run(`document.querySelector(${JSON.stringify(selector)}).click()`);
  const content = (selector) => run(`document.querySelector(${JSON.stringify(selector)}).textContent`);
  const select = (id, value) => run(`{
    const control = document.getElementById(${JSON.stringify(id)});
    control.value = ${JSON.stringify(value)};
    control.dispatchEvent(new Event("change"));
  }`);
  const closeCost = () => click("#cost-dialog button[type=submit]");

  await waitFor(() => run("Boolean(window.chrome?.webview && !document.querySelector('#total-cost').disabled)"), "Dashboard did not render usage");
  await select("range", "today");
  await click("#total-cost");
  assert.equal(await content("#cost-amount"), "$1.1635");
  assert.equal(await content("#cost-priced"), "300,100");
  assert.equal(await content("#cost-unpriced"), "20");
  assert.match(await content("#cost-rates"), /gpt-6\.2-sol-high/);
  assert.match(await content("#cost-rates"), /codex-auto-review/);
  assert.match(await content("#cost-rates"), /单次输入 > 272,000/);
  assert.match(await content("#source-health"), /找不到数据目录/);
  await closeCost();

  await select("range", "yesterday");
  await click("#table-body .cost-link");
  assert.equal(await content("#cost-amount"), "$0.0027");
  assert.match(await content("#cost-rates"), /gpt-6\.2-sol-medium/);
  assert.doesNotMatch(await content("#cost-rates"), /codex-auto-review/);
  await closeCost();
  await select("model-filter", "codex-auto-review");
  assert.match(await content("#source-health"), /当前筛选范围内无用量/);
  await click("#clear");
  await select("range", "week");
  const monday = localDay(-((localDay(0).getDay() + 6) % 7));
  const localISO = (date) => [date.getFullYear(), date.getMonth() + 1, date.getDate()]
    .map((value) => String(value).padStart(2, "0")).join("-");
  assert.equal(await content("#chart-caption"), `${localISO(monday)} 至 ${localISO(localDay(0))}`);
  await click("#clear");
  await click("[data-tab=projects]");
  await click("#table-body .cost-link");
  assert.equal(await content("#cost-amount"), "$1.1662");
  await closeCost();

  if (await run("!document.querySelector('#appearance').hidden")) {
    await click("#appearance");
    assert.equal(await run("document.querySelectorAll('[data-theme-choice]').length"), 6);
    await click("[data-theme-choice=flowers]");
    assert.equal(await run("document.documentElement.dataset.theme"), "flowers");
  }
  if (screenshot) {
    await click("[data-tab=overview]");
    await click("#total-cost");
    const {data} = await command("Page.captureScreenshot", {format: "png"});
    await writeFile(screenshot, Buffer.from(data, "base64"));
    await closeCost();
  }

  await run("setTimeout(() => document.querySelector('#quit').click(), 100)");
  await waitFor(() => app.exitCode === 0, "Packaged app did not shut down cleanly");
  await assert.rejects(readFile(marker), {code: "ENOENT"});
  console.log("Windows WebView2 smoke test passed: startup, pricing details, date controls, source status and shutdown.");
} finally {
  socket?.close();
  if (app?.pid && app.exitCode === null && app.signalCode === null) {
    spawnSync("taskkill.exe", ["/PID", String(app.pid), "/T", "/F"], {stdio: "ignore", windowsHide: true});
  }
  try {
    const resolved = await realpath(state);
    if (path.dirname(resolved) === tempRoot && path.basename(resolved).startsWith("agent-usage-smoke-")) {
      await rm(resolved, {recursive: true, force: true, maxRetries: 5, retryDelay: 200});
    }
  } catch (error) {
    console.warn("Could not clean the temporary smoke test directory:", error.message);
  }
}
