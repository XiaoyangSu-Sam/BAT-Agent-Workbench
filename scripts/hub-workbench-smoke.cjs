// Start scripts/workbench_fixture.py and pass the installed Playwright module path.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(path.resolve(process.argv[2] || "node_modules/playwright"));

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(6500);
    const pageErrors = [];
    page.on("pageerror", error => pageErrors.push(error.message));
    const local = { target: "local", connected: false, folder: "", kernel: "" };
    const targets = new Map();
    const discoveries = [];
    let remoteSession, holdNextRead, releaseRead;
    await page.route(/\/api\/sessions\/[^/]+\/hub(?:\/\w+)?$/, async route => {
      const [, id, action] = new URL(route.request().url()).pathname.match(/\/sessions\/([^/]+)\/hub(?:\/(\w+))?$/);
      const body = route.request().postDataJSON();
      let result = targets.get(id) || local;
      if (action === "discover") {
        discoveries.push(body);
        result = { kernels: ["python3", "analysis"] };
      } else if (action === "connect") {
        remoteSession = id;
        result = { target: "jupyterhub", connected: true, ...body };
        targets.set(id, result);
      } else if (action === "disconnect") {
        targets.delete(id);
        result = local;
      } else if (holdNextRead) {
        holdNextRead();
        holdNextRead = null;
        await new Promise(resolve => { releaseRead = resolve; });
      }
      await route.fulfill({ json: result });
    });
    await page.goto("http://127.0.0.1:18767");
    await page.locator("#workbench:visible, #connectionScreen:visible").waitFor();
    if (await page.locator("#connectionScreen").isVisible()) await page.click("#previewWorkspace");
    await page.locator("#workbench").waitFor({state: "visible"});
    await page.click("#newSession");
    const target = page.locator("#hubTarget");
    await target.filter({hasText: "Local"}).click();
    await page.fill("#hubUrl", "https://hub.example.invalid");
    await page.fill("#hubUsername", "fixture-user");
    await page.fill("#hubPassword", "fixture-password");
    await page.click("#hubDiscover");
    await page.locator("#hubKernel").waitFor({state: "visible"});
    assert.equal(await page.inputValue("#hubPassword"), "");
    assert.deepEqual(discoveries[0], {
      url: "https://hub.example.invalid", username: "fixture-user", password: "fixture-password", token: ""
    });
    await page.selectOption("#hubKernel", "analysis");
    await page.fill("#hubFolder", "examples/reports");
    await page.click("#hubConnect");
    await page.locator("#hubDialog").waitFor({state: "hidden"});
    await target.filter({hasText: "JupyterHub"}).waitFor();
    assert.deepEqual(targets.get(remoteSession), {
      target: "jupyterhub", connected: true, kernel: "analysis", folder: "examples/reports"
    });
    await page.reload();
    await target.filter({hasText: "JupyterHub"}).waitFor();
    await page.getByRole("button", {name: /Write and run Python/}).click();
    assert.match(await page.inputValue("#prompt"), /run_jupyter_script/);
    await page.evaluate(() => renderEvent({
      seq: 1, time: 1, type: "approval_required", data: {
        approval_id: "test", name: "run_jupyter_cell", kind: "execute",
        execution_target: "JupyterHub", kernel: "analysis", folder: "examples/reports",
        arguments: { code: "print(42)" }
      }
    }, false));
    const approval = await page.textContent("#approvalText");
    for (const value of ["JupyterHub", "analysis", "examples/reports", "print(42)"]) assert.ok(approval.includes(value));
    await page.evaluate(() => renderEvent({
      seq: 2, time: 2, type: "tool_result", data: {ok: true, name: "run_jupyter_cell", call_id: "test",
        result: {ok: true, execution_count: 3, stdout: "", stderr: "",
          outputs: [{output_type: "execute_result", data: {"text/plain": "42"}}]}}
    }, false));
    const output = await page.textContent("#output");
    assert.match(output, /42/);
    assert.doesNotMatch(output, /undefined/);

    // A late conversation response must not restore its messages or approvals in the new chat.
    let releaseConversation;
    const delayedConversation = new Promise(resolve => {
      page.route("**/api/sessions/" + remoteSession, async route => {
        await new Promise(resume => { releaseConversation = resume; resolve(); });
        await route.fulfill({json: {messages: [{role: "user", content: "Late remote message"}], runs: []}});
      }, {times: 1});
    });
    const delayedRead = new Promise(resolve => { holdNextRead = resolve; });
    await page.click("#newSession");
    await page.locator(`[data-session-id="${remoteSession}"]`).click();
    await Promise.all([delayedRead, delayedConversation]);
    await page.click("#newSession");
    const returned = page.waitForResponse(response => response.url().endsWith("/sessions/" + remoteSession + "/hub"));
    const returnedConversation = page.waitForResponse(response => response.url().endsWith("/sessions/" + remoteSession));
    releaseRead();
    releaseConversation();
    await Promise.all([(await returned).finished(), (await returnedConversation).finished()]);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    await target.filter({hasText: "Local"}).waitFor();
    assert.equal(await target.getAttribute("data-state"), "local");
    assert.equal(await page.locator("#messages").getByText("Late remote message").count(), 0);
    await page.getByRole("button", {name: /Write and run Python/}).click();
    assert.match(await page.inputValue("#prompt"), /run_python_script/);

    targets.set(remoteSession, { ...targets.get(remoteSession), connected: false });
    await page.locator(`[data-session-id="${remoteSession}"]`).click();
    await target.filter({hasText: /reconnect/i}).click();
    await page.selectOption("#hubAuth", "token");
    await page.fill("#hubToken", "fixture-token");
    await page.keyboard.press("Escape");
    await page.locator("#hubDialog").waitFor({state: "hidden"});
    assert.equal(await page.inputValue("#hubToken"), "");
    await target.click();
    await page.selectOption("#hubAuth", "token");
    await page.fill("#hubUrl", "https://hub.example.invalid");
    await page.fill("#hubToken", "fixture-token");
    await page.click("#hubDiscover");
    await page.locator("#hubKernel").waitFor({state: "visible"});
    assert.deepEqual(discoveries.at(-1), {
      url: "https://hub.example.invalid", username: "", password: "", token: "fixture-token"
    });
    await page.setViewportSize({width: 390, height: 700});
    assert.ok(await page.locator("#hubDialog").evaluate(node => node.scrollWidth <= node.clientWidth + 1));
    await page.click("#hubLocal");
    await page.locator("#hubDialog").waitFor({state: "hidden"});
    await target.filter({hasText: "Local"}).waitFor();
    assert.equal(targets.has(remoteSession), false);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    targets.set(remoteSession, {target: "jupyterhub", connected: true, kernel: "python3", folder: ""});
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await target.filter({hasText: "JupyterHub"}).waitFor();
    const warning = "Could not confirm kernel cleanup. Check your running sessions in JupyterLab.";
    await page.route("**/api/workspace/open", async route => {
      const response = await route.fetch();
      await route.fulfill({response, json: {...await response.json(), hub_cleanup_warning: warning}});
    });
    const fixture = await (await page.request.get("http://127.0.0.1:18768/metrics")).json();
    const anotherWorkspace = fs.mkdtempSync(path.join(fixture.root, "hub-workspace-"));
    await page.click("#filesActivity");
    await page.click("#openFolder");
    await page.fill("#textDialogInput", anotherWorkspace);
    await page.locator("#textDialogForm .primary").click();
    await page.locator("#textDialog").waitFor({state: "hidden"});
    await page.click("#closeEditor");
    await target.filter({hasText: "Local"}).waitFor();
    assert.equal(await page.textContent("#error"), warning);
    assert.equal(await page.evaluate(() => /fixture-(password|token)/.test(JSON.stringify([localStorage, sessionStorage]))), false);
    assert.deepEqual(pageErrors, []);
    console.log("PASS: JupyterHub discovery, authentication modes, remote folder, reload, approval details, stale session responses, reconnect, credential clearing, local selection and mobile layout.");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
