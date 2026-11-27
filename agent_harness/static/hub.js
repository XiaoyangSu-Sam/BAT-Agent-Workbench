"use strict";

const hub = (() => {
  const dialog = $("hubDialog");
  const localPythonPrompt = $("pythonSuggestion").dataset.prompt;
  const local = {target: "local", connected: false, kernel: "", folder: ""};
  let status = local;
  let viewVersion = 0;
  let statusRequest = 0;
  let working = false;

  dialog.innerHTML = `
    <div class="dialog-head"><h3 id="hubHeading">Execution target</h3>
      <button id="hubClose" type="button" class="icon-btn" aria-label="Close execution settings">×</button></div>
    <p id="hubCurrent" role="status"></p>
    <p class="muted">A JupyterHub kernel runs code for this conversation. Edit project files in BAT and transfer files through JupyterLab.</p>
    <form id="hubForm" method="dialog" autocomplete="off">
      <fieldset id="hubSettings">
        <div id="hubSignIn">
          <label>JupyterHub URL<input id="hubUrl" type="url" required maxlength="4096" placeholder="https://hub.example.org" spellcheck="false" autocapitalize="none"></label>
          <label>Sign in with<select id="hubAuth"><option value="password">Username and password</option><option value="token">API token</option></select></label>
          <div id="hubCredentials">
            <label>Username<input id="hubUsername" required maxlength="512" spellcheck="false" autocapitalize="none"></label>
            <label>Password<input id="hubPassword" type="password" required maxlength="8192"></label>
          </div>
          <label id="hubTokenField" hidden>API token<input id="hubToken" type="password" required maxlength="8192" disabled></label>
        </div>
        <div id="hubSelection" hidden>
          <label>Kernel<select id="hubKernel"></select></label>
          <label>Remote working directory<input id="hubFolder" maxlength="4096" placeholder="JupyterLab root" spellcheck="false" aria-describedby="hubFolderHelp"></label>
          <small id="hubFolderHelp">Enter a directory relative to the JupyterLab root, or leave this empty to use the root.</small>
        </div>
      </fieldset>
      <p id="hubMessage" role="status"></p>
      <div class="hub-actions">
        <button type="button" class="secondary" id="hubLocal" hidden>Use local</button>
        <button type="button" class="text-btn" id="hubBack" hidden>Back</button>
        <button type="submit" class="primary" id="hubDiscover">Discover kernels</button>
        <button type="submit" class="primary" id="hubConnect" hidden>Connect</button>
      </div>
    </form>`;

  function message(text, failed = false) {
    $("hubMessage").setAttribute("role", failed ? "alert" : "status");
    $("hubMessage").textContent = text;
  }

  function clearCredentials() {
    for (const id of ["hubUsername", "hubPassword", "hubToken"]) $(id).value = "";
  }

  function setWorking(value) {
    working = value;
    for (const id of ["hubSettings", "hubDiscover", "hubConnect", "hubBack", "hubLocal", "hubClose"]) $(id).disabled = value;
  }

  function authFields() {
    const tokenMode = $("hubAuth").value === "token";
    $("hubCredentials").hidden = tokenMode;
    $("hubTokenField").hidden = !tokenMode;
    $("hubUsername").disabled = $("hubPassword").disabled = tokenMode;
    $("hubToken").disabled = !tokenMode;
  }

  function showKernels(choosing) {
    $("hubSignIn").hidden = $("hubDiscover").hidden = choosing;
    $("hubSelection").hidden = $("hubConnect").hidden = $("hubBack").hidden = !choosing;
    // Hidden required inputs must not participate in validation during connection.
    for (const input of $("hubSignIn").querySelectorAll("input,select")) input.disabled = choosing;
    if (!choosing) authFields();
  }

  function display(next, state) {
    status = next;
    if (next) state = next.target === "local" ? "local" : next.connected ? "connected" : "reconnect";
    const labels = {
      local: ["Run on Local", "This conversation runs code locally."],
      connected: ["Run on JupyterHub", "Connected to JupyterHub."],
      reconnect: ["JupyterHub · Reconnect", "JupyterHub is disconnected. Sign in again to run code."],
      checking: ["Checking target…", "Checking this conversation’s execution target…"],
      unknown: ["Target unavailable", "Could not read this conversation’s execution target."]
    };
    const [label, description] = labels[state];
    const details = next?.target === "jupyterhub" && next.kernel
      ? " Kernel: " + next.kernel + " · Directory: " + (next.folder || "JupyterLab root") : "";
    $("hubCurrent").textContent = description + details;
    $("hubTarget").textContent = label + " ▾";
    $("hubTarget").title = description + details;
    $("hubTarget").dataset.state = state;
    $("pythonSuggestion").dataset.prompt = next?.target === "jupyterhub"
      ? "Create a Python file in the local workspace that prints a greeting. Read it back, run it with run_jupyter_script using its SHA256, and report the kernel output."
      : localPythonPrompt;
    $("hubLocal").hidden = !sessionId || state === "local" || state === "checking";
  }

  const path = (id, action = "") => "/sessions/" + encodeURIComponent(id) + "/hub" + action;
  async function refresh() {
    const id = sessionId;
    if (!id || working) return;
    const mine = ++statusRequest;
    try {
      const next = await api(path(id));
      if (mine === statusRequest && id === sessionId) display(next);
    } catch {
      if (mine === statusRequest && id === sessionId) display(null, "unknown");
    }
  }

  function resetDialog() {
    ++viewVersion;
    setWorking(false);
    $("hubForm").reset();
    clearCredentials();
    $("hubKernel").replaceChildren();
    showKernels(false);
    message("");
  }

  function sessionChanged() {
    ++statusRequest;
    if (dialog.open) dialog.close();
    resetDialog();
    display(sessionId ? null : local, "checking");
    refresh();
  }

  // A POST can update the server's target. Keep the dialog open until it resolves.
  async function perform(action) {
    if (working || busy) return;
    const mine = viewVersion;
    const current = () => mine === viewVersion && dialog.open;
    setWorking(true);
    ++statusRequest;
    try {
      await action(current);
    } catch (error) {
      if (current()) {
        if (["hub_discovery_required", "hub_connect_failed"].includes(error.code)) showKernels(false);
        message(error.message, true);
      }
    } finally {
      if (mine === viewVersion) {
        setWorking(false);
        if (dialog.open && !document.activeElement?.matches("#hubDialog input, #hubDialog select"))
          ($("hubSelection").hidden ? $("hubUrl") : $("hubKernel")).focus();
        refresh();
      }
    }
  }

  async function discover(current) {
    const url = new URL($("hubUrl").value.trim());
    const credentials = { url: $("hubUrl").value.trim(), username: $("hubUsername").value.trim(),
      password: $("hubPassword").value, token: $("hubToken").value.trim() };
    clearCredentials();
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash)
      throw new Error("Enter the HTTPS Hub address without credentials, a query or a fragment.");
    message("Discovering kernels…");
    if (!sessionId) {
      const title = $("prompt").value.trim().slice(0, 60) || "JupyterHub conversation";
      const created = await api("/sessions", { title, approval_mode: draftApprovalMode });
      if (!current()) return;
      sessionId = created.id;
      draftApprovalMode = null;
      localStorage.setItem("bat.session", sessionId);
      sessionRecords.unshift(created);
      renderSessions();
    }
    const data = await api(path(sessionId, "/discover"), credentials);
    if (!current()) return;
    if (!data.kernels?.length) throw new Error("The server returned no kernels. Check your JupyterLab server, then sign in again.");
    $("hubKernel").replaceChildren(...data.kernels.map(name => new Option(name, name)));
    if (data.kernels.includes(status?.kernel)) $("hubKernel").value = status.kernel;
    if (!$("hubFolder").value) $("hubFolder").value = status?.folder || "";
    showKernels(true);
    message("");
  }

  async function changeTarget(current, action) {
    const connecting = action === "connect";
    message(connecting ? "Connecting to the kernel…" : "Switching to local execution…");
    const body = connecting ? {kernel: $("hubKernel").value, folder: $("hubFolder").value.trim()} : {};
    try {
      const next = await api(path(sessionId, "/" + action), body);
      if (current()) { display(next); dialog.close(); }
    } finally {
      // Even a failed connection can pin the target and reset an earlier execution grant.
      await listSessions().catch(e => error(e.message));
    }
  }

  $("hubTarget").onclick = () => {
    if (busy) return;
    resetDialog();
    dialog.showModal();
    $("hubUrl").focus();
    refresh();
  };
  $("hubClose").onclick = () => { clearCredentials(); dialog.close(); };
  dialog.oncancel = event => {
    if (working) event.preventDefault();
    else clearCredentials();
  };
  dialog.onclose = () => { if (!dialog.open) resetDialog(); };
  $("hubAuth").onchange = () => { clearCredentials(); authFields(); };
  $("hubBack").onclick = () => { showKernels(false); message(""); $("hubUrl").focus(); };
  $("hubForm").onsubmit = event => {
    event.preventDefault();
    perform($("hubSelection").hidden ? discover : current => changeTarget(current, "connect"));
  };
  $("hubLocal").onclick = () => perform(current => changeTarget(current, "disconnect"));
  window.addEventListener("focus", refresh);
  setInterval(() => {
    if (!document.hidden && status?.target === "jupyterhub") refresh();
  }, 15000);
  display(local);

  return {
    sessionChanged,
    runChanged(active) {
      $("hubTarget").disabled = active;
      if (active && dialog.open) { dialog.close(); resetDialog(); }
      if (!active) refresh();
    },
    cellOutput(result) {
      let text = "";
      const line = value => { text += (text && !text.endsWith("\n") ? "\n" : "") + value + "\n"; };
      for (const item of result.outputs) {
        if (item.output_type === "stream") text += (item.name === "stderr" ? "[stderr] " : "") + item.text;
        else if (item.output_type === "error") line(item.ename + ": " + item.evalue);
        else if (item.data?.["text/plain"]) line(item.data["text/plain"]);
        else if (item.data?.["image/png"]) line("[PNG image]");
      }
      if (result.truncated) line("[Output truncated]");
      const count = result.execution_count == null ? "" : " " + result.execution_count;
      line("Kernel execution" + count + (result.ok ? " completed." : " failed."));
      return text.trimEnd();
    },
    approvalTarget(data) {
      if (!data.execution_target) return "";
      const details = ["Execution target: " + data.execution_target];
      if (data.kernel) details.push("Kernel: " + data.kernel);
      if (typeof data.folder === "string") details.push("Directory: " + (data.folder || "JupyterLab root"));
      return details.join("\n") + "\n\n";
    }
  };
})();
