// node scripts/skill-import-smoke.cjs <installed Playwright module> <project Python>
const assert = require("node:assert/strict");
const {execFileSync, spawn} = require("node:child_process");
const {once} = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");
const {chromium} = require(path.resolve(process.argv[2] || "node_modules/playwright"));
const python = process.argv[3];
if (!python) throw new Error("Pass the project's existing Python interpreter as the second argument.");

// An ephemeral loopback port and shutdown on stdin EOF keep this fixture independent of other checks.
const serverCode = `
import socket, sys, threading, uvicorn
from agent_harness.server import create_app
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(create_app(sys.argv[1]), log_level="warning"))
    def stop_on_eof():
        sys.stdin.buffer.read()
        server.should_exit = True
    threading.Thread(target=stop_on_eof, daemon=True).start()
    sock.listen()
    print(sock.getsockname()[1], flush=True)
    server.run(sockets=[sock])
`;

(async () => {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "bat-skill-import-"));
  let browser, server, exited;
  try {
    const root = path.join(scratch, "app");
    const origin = path.join(scratch, "origin");
    const catalog = path.join(origin, "catalog");
    const zip = path.join(scratch, "Downloaded catalog.zip");
    const binary = Buffer.from([0, 255, 13, 10, 1]);
    for (const id of ["alpha", "bravo"]) {
      const skill = path.join(catalog, id);
      fs.mkdirSync(path.join(skill, "assets"), {recursive: true});
      fs.writeFileSync(path.join(skill, "SKILL.md"), "# " + id + "\n");
      fs.writeFileSync(path.join(skill, "assets", "template.bin"), binary);
    }
    const folder = path.join(scratch, "extracted", "charlie");
    fs.mkdirSync(path.join(root, "workspace"), {recursive: true});
    fs.mkdirSync(folder, {recursive: true});
    fs.writeFileSync(path.join(folder, "SKILL.md"), "# charlie\n");
    const ssh = path.join(scratch, "fixture-ssh");
    fs.writeFileSync(ssh, '#!/bin/sh\nexec git upload-pack "$BAT_TEST_ORIGIN"\n');
    const env = {...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: path.join(scratch, "gitconfig"),
      GIT_SSH_VARIANT: "ssh", BAT_TEST_ORIGIN: origin,
      GIT_SSH_COMMAND: "sh '" + ssh.split(path.sep).join("/").replace(/'/g, "'\\''") + "'"};
    for (const key of ["AGENT_HARNESS_MODEL_PROVIDER", "LLM_ENDPOINT", "LLM_MODEL", "LLM_API_KEY"]) delete env[key];
    const git = (...args) => execFileSync("git", ["-C", origin, ...args], {env, stdio: "pipe"});
    git("init", "-q");
    git("add", ".");
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Add example Skills");
    git("archive", "--format=zip", "--prefix=downloaded-main/", "--output=" + zip, "HEAD");

    server = spawn(python, ["-u", "-c", serverCode, root], {
      cwd: path.resolve(__dirname, ".."), env, windowsHide: true, stdio: ["pipe", "pipe", "inherit"]
    });
    exited = once(server, "exit");
    const output = readline.createInterface({input: server.stdout});
    const [port] = await Promise.race([once(output, "line"), exited.then(([code]) => {
      throw new Error("Fixture exited before startup (code " + code + ").");
    })]);
    assert.match(port, /^\d+$/);
    browser = await chromium.launch({headless: true});
    const page = await browser.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto("http://127.0.0.1:" + port);
    await page.click("#previewWorkspace");
    await page.click("#skillsToggle");
    await page.click("#openSkillImport");
    const message = page.locator("#skillImportMessage");
    const dialog = page.locator("#skillImportDialog");
    const select = page.locator("#skillImportSource");
    const preview = page.locator("#skillImportPreview");
    const install = page.locator("#skillImportInstall");
    const checkbox = id => page.locator("#skillImportList label").filter({hasText: id}).locator("input");
    const result = (id, status) => page.locator('#skillImportResults li[data-status="' + status + '"]').filter({hasText: id});

    await select.selectOption("repository");
    await page.fill("#skillImportUrl", "git@skills.example:catalog/smoke.git");
    await page.fill("#skillImportSubdir", "catalog");
    assert.ok(await preview.isDisabled());
    await page.click("#skillImportSave");
    await message.filter({hasText: "Repository saved."}).waitFor();
    await page.click("#skillImportClose");
    await page.click("#openSkillImport");
    await preview.click();
    await checkbox("alpha").check();
    assert.equal(await checkbox("bravo").isChecked(), false);
    await install.click();
    await result("alpha", "installed").waitFor();
    assert.ok(!fs.existsSync(path.join(root, "skills", "bravo")));
    assert.deepEqual(fs.readFileSync(path.join(root, "skills/alpha/assets/template.bin")), binary);

    await select.selectOption("zip");
    await page.setInputFiles("#skillImportZip", zip);
    await preview.click();
    await page.getByText("downloaded-main/catalog/bravo", {exact: true}).waitFor();
    await page.check("#skillImportAll");
    await install.click();
    await result("alpha", "identical").waitFor();
    await result("bravo", "installed").waitFor();
    assert.deepEqual(fs.readFileSync(path.join(root, "skills/bravo/assets/template.bin")), binary);
    const localFile = path.join(root, "skills/bravo/SKILL.md");
    fs.writeFileSync(localFile, "# Local edits\n");
    await preview.click();
    await checkbox("bravo").waitFor();
    await page.check("#skillImportAll");
    await install.click();
    await result("bravo", "conflict").waitFor();
    assert.equal(fs.readFileSync(localFile, "utf8"), "# Local edits\n");

    await select.selectOption("folder");
    await page.fill("#skillImportFolder", "missing-folder");
    await preview.click();
    await page.locator('#skillImportMessage[role="alert"]').waitFor();
    await page.fill("#skillImportFolder", folder);
    await preview.click();
    await checkbox("charlie").waitFor();
    assert.ok(await checkbox("charlie").isChecked());
    // A lost ticket re-previews the source and requires a fresh install click.
    await page.route("**/api/skill-import/apply", route => route.continue({
      postData: JSON.stringify({...route.request().postDataJSON(), ticket: "lost-fixture-ticket"})
    }), {times: 1});
    await install.click();
    await message.filter({hasText: "previous preview expired"}).waitFor();
    assert.ok(!fs.existsSync(path.join(root, "skills/charlie")));
    await page.setViewportSize({width: 390, height: 700});
    assert.ok(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth + 1));
    await install.click();
    await result("charlie", "installed").waitFor();
    await page.click("#skillImportClose");
    await page.locator("#skillCount").filter({hasText: /^3$/}).waitFor();
    await page.locator(".skill-card").filter({hasText: "charlie"}).getByText("View instructions").click();
    await page.locator("#skillInstructions").filter({hasText: "# charlie"}).waitFor();
    await page.click("#closeSkill");
    await page.evaluate(() => {
      document.querySelector("#skills").replaceChildren();
      renderEvent({seq: 1, time: 1, type: "tool_result", data: {name: "install_skills", ok: true, result: {}}}, false);
    });
    await page.locator(".skill-card").filter({hasText: "charlie"}).waitFor();
    assert.deepEqual(errors, []);
    console.log("PASS: offline repository configuration, single and bulk install, real ZIP and folder imports, binary assets, repeat import, conflict preservation, expired-preview recovery, mobile layout and registry refresh.");
  } finally {
    try {
      if (browser) await browser.close();
    } finally {
      if (server) { server.stdin.end(); await exited; }
      assert.equal(path.dirname(path.resolve(scratch)), path.resolve(os.tmpdir()));
      fs.rmSync(scratch, {recursive: true, force: true, maxRetries: 5, retryDelay: 100});
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
