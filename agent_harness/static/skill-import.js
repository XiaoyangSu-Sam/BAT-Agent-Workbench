"use strict";

(() => {
  const dialog = $("skillImportDialog");
  dialog.innerHTML = `
    <div class="dialog-head"><h3 id="skillImportHeading">Import Skills</h3>
      <button id="skillImportClose" type="button" class="icon-btn" aria-label="Close Skill import">×</button></div>
    <fieldset id="skillImportControls">
      <label>Source<select id="skillImportSource">
        <option value="zip">ZIP file</option><option value="folder">Extracted folder</option>
        <option value="repository">Configured repository</option>
      </select></label>
      <div id="skillImportZipFields">
        <label>ZIP file<input id="skillImportZip" type="file" accept=".zip,application/zip"></label>
        <small>Choose a ZIP from Downloads or another folder. Maximum size is 64 MiB.</small>
      </div>
      <div id="skillImportFolderFields" hidden>
        <label>Folder path<input id="skillImportFolder" spellcheck="false" autocomplete="off"></label>
        <small>Enter the full path on the BAT machine or a path relative to the current workspace.</small>
      </div>
      <div id="skillImportRepoFields" hidden>
        <label>Repository URL<input id="skillImportUrl" spellcheck="false" autocomplete="off"></label>
        <label>Skills subfolder (optional)<input id="skillImportSubdir" spellcheck="false" autocomplete="off"></label>
        <small>Saved on this BAT installation. Use an HTTPS or SSH clone address with your existing Git credentials.</small>
        <button id="skillImportSave" type="button" class="secondary">Save repository</button>
      </div>
      <button id="skillImportPreview" type="button" class="secondary">Preview Skills</button>
      <section id="skillImportSelection" aria-labelledby="skillImportSelectionHeading" hidden>
        <h4 id="skillImportSelectionHeading">Choose Skills</h4>
        <label class="import-check"><input id="skillImportAll" type="checkbox">Select all</label>
        <div id="skillImportList"></div>
        <button id="skillImportInstall" type="button" class="primary" disabled>Install selected</button>
      </section>
    </fieldset>
    <p id="skillImportMessage" role="status"></p><ul id="skillImportResults" aria-label="Import results"></ul>`;

  const source = $("skillImportSource");
  const list = $("skillImportList");
  const selectAll = $("skillImportAll");
  const controls = $("skillImportControls");
  let saved = {url: "", subdir: ""};
  let ticket = null;

  function repositoryFields() {
    return {url: $("skillImportUrl").value.trim(), subdir: $("skillImportSubdir").value.trim()};
  }

  function message(text, failure = false) {
    $("skillImportMessage").setAttribute("role", failure ? "alert" : "status");
    $("skillImportMessage").textContent = text;
  }

  function updateSource() {
    const fields = repositoryFields();
    const changed = fields.url !== saved.url || fields.subdir !== saved.subdir;
    $("skillImportZipFields").hidden = source.value !== "zip";
    $("skillImportFolderFields").hidden = source.value !== "folder";
    $("skillImportRepoFields").hidden = source.value !== "repository";
    $("skillImportSave").disabled = !changed;
    $("skillImportPreview").disabled = ({
      repository: !saved.url || changed,
      folder: !$("skillImportFolder").value.trim(),
      zip: !$("skillImportZip").files.length
    })[source.value];
  }

  function resetPreview() {
    ticket = null;
    list.replaceChildren();
    updateSelection();
    $("skillImportSelection").hidden = true;
    $("skillImportResults").replaceChildren();
    message("");
    updateSource();
  }

  function updateSelection() {
    const count = list.querySelectorAll("input:checked").length;
    selectAll.checked = count > 0 && count === list.childElementCount;
    selectAll.indeterminate = count > 0 && !selectAll.checked;
    $("skillImportInstall").disabled = !count;
    $("skillImportInstall").textContent = count ? "Install selected (" + count + ")" : "Install selected";
  }

  async function perform(action) {
    if (controls.disabled) return;
    controls.disabled = $("skillImportClose").disabled = true;
    try { await action(); }
    catch (err) { message(err.message, true); }
    finally { controls.disabled = $("skillImportClose").disabled = false; }
  }

  async function preview() {
    resetPreview();
    message("Looking for Skills…");
    let data;
    if (source.value === "zip") {
      const file = $("skillImportZip").files[0];
      if (file.size > 64 * 1024 * 1024) throw new Error("Choose a ZIP file of 64 MiB or less.");
      data = await api("/skill-import/upload?name=" + encodeURIComponent(file.name), file);
    } else {
      const body = {source: source.value};
      if (source.value === "folder") body.path = $("skillImportFolder").value.trim();
      data = await api("/skill-import/preview", body);
    }
    if (!data.skills.length) { message("No folders containing SKILL.md were found."); return; }
    ticket = data.ticket;
    for (const skill of data.skills) {
      const row = el("label", undefined, "import-check");
      const checkbox = el("input");
      checkbox.type = "checkbox";
      checkbox.value = skill.path;
      checkbox.checked = data.skills.length === 1;
      const name = el("span", skill.id);
      name.append(el("small", skill.path));
      row.append(checkbox, name);
      list.append(row);
    }
    $("skillImportSelection").hidden = false;
    updateSelection();
    message(data.skills.length === 1 ? "1 Skill found." : data.skills.length + " Skills found.");
  }

  async function install() {
    const selected = Array.from(list.querySelectorAll("input:checked"), input => input.value);
    message("Installing selected Skills…");
    let data;
    try { data = await api("/skill-import/apply", {ticket, selected}); }
    catch (err) {
      if (err.code !== "skill_preview_expired") throw err;
      await preview();
      if (ticket) message("The previous preview expired. Review the refreshed selection before installing.");
      return;
    }
    resetPreview();
    const labels = {installed: "Installed", identical: "Already installed (identical)", conflict: "Conflict (existing folder kept)"};
    for (const item of data.results) {
      const row = el("li");
      row.append(el("strong", item.id), el("span", labels[item.status]), el("small", item.path));
      row.dataset.status = item.status;
      $("skillImportResults").append(row);
    }
    message("Import complete.");
    try { await loadSkills(); }
    catch (err) { message("Import finished. Could not refresh the Skills panel. " + err.message, true); }
  }

  function showRepository(config) {
    saved = config;
    $("skillImportUrl").value = config.url;
    $("skillImportSubdir").value = config.subdir;
    updateSource();
  }

  $("openSkillImport").onclick = () => {
    source.value = "zip";
    $("skillImportZip").value = $("skillImportFolder").value = "";
    showRepository({url: "", subdir: ""});
    resetPreview();
    dialog.showModal();
    perform(async () => {
      const config = await api("/skill-source");
      if (config.url) source.value = "repository";
      showRepository(config);
    });
  };
  $("skillImportClose").onclick = () => dialog.close();
  dialog.oncancel = event => { if (controls.disabled) event.preventDefault(); };
  source.onchange = $("skillImportZip").onchange = resetPreview;
  $("skillImportFolder").oninput = $("skillImportUrl").oninput = $("skillImportSubdir").oninput = resetPreview;
  $("skillImportSave").onclick = () => perform(async () => {
    showRepository(await api("/skill-source", repositoryFields(), "PUT"));
    message(saved.url ? "Repository saved." : "Repository cleared.");
  });
  $("skillImportPreview").onclick = () => perform(preview);
  list.onchange = updateSelection;
  selectAll.onchange = () => {
    for (const input of list.querySelectorAll("input")) input.checked = selectAll.checked;
    updateSelection();
  };
  $("skillImportInstall").onclick = () => perform(install);
})();
