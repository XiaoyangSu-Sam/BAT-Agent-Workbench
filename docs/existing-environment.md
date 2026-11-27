# Update an existing BAT Agent installation

For a customized installation, use the [v0.7.6 feature patch and merge instructions](updates/v0.7.6.md). The source and the Python environment can be updated separately. The procedure below covers full source updates.

## Before the update

Stop BAT Agent and save your edits. Back up the installation and local data outside the repository. Include `config/*.yaml`, custom Skills, workspace files and the complete stopped `.harness/` directory. SQLite may keep recent writes in files alongside the database. Check the release guide for schema changes before starting the new version.

## Apply the source changes

For an extracted release, follow the [deployment helper instructions](setup.md#update-or-replace-an-existing-folder). The helper verifies source hashes and previews changes before applying them.

For a manual update, review the changed-file list for the release and merge any local edits. Keep the runtime, bundled UI assets, package metadata and example configuration from the same version. Keep your active configuration, Python environment, Git settings, project instructions, workspace and custom Skills. Release hashes describe the original files and will differ for files that you customize.

## Check and start

From the updated source root, run both commands with the same Python 3.11+ interpreter.

```powershell
$BatPython = Read-Host "Full path to the project's python.exe"
& $BatPython scripts/check_environment.py
```

When the report shows `"ok": true`, start the server.

```powershell
& $BatPython -m agent_harness
```

If dependencies are missing or incompatible, use the [installation steps](setup.md). Open the [local workbench](http://127.0.0.1:8765) and run the [startup checklist](deployment-checklist.md). To roll back a release with a database change, stop the server and restore the previous source and its matching state backup.
