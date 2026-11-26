# Setup and updates

## Choose Python

BAT Agent requires Python 3.11 or later. By default, Skills run with the same interpreter as the server. To use a different interpreter for Skills, set `python_executable` in `config/execution.yaml`.

If you already have a project environment, run `scripts/check_environment.py` with that interpreter. When the report shows `"ok": true`, you can start BAT directly from the source folder. See [Updating an existing installation](existing-environment.md) for preserving local configuration and data.

## Install dependencies

For a new Windows installation, open PowerShell in the source folder and run:

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e .
```

If you use uv, the equivalent install command is `uv pip install --python .venv/Scripts/python.exe -e .`. For an offline install, supply an existing wheel directory containing the runtime and build dependencies.

```powershell
$WheelDirectory = Read-Host "Path to the wheel directory"
./.venv/Scripts/python.exe -m pip install --no-index --find-links $WheelDirectory -e .
```

Notebook execution also needs the `notebook` extra, installed with `python -m pip install -e ".[notebook]"` using the chosen interpreter. Install any additional dependencies used by a Skill in its execution environment. The bundled web interface runs without a frontend build or browser download.

## Check and run

```powershell
./.venv/Scripts/python.exe scripts/check_environment.py
./.venv/Scripts/python.exe -m agent_harness
```

If your interpreter lives elsewhere, use its executable for both commands. The local check inspects required dependencies, bundled UI assets, configuration and Skill layout. Exit code 0 means the prerequisites passed. Optional features report their own status. Use **Test connection** in the workbench to check the model service.

Open the [local workbench](http://127.0.0.1:8765). To change the port, add `--port 8766`. To select the application folder explicitly, pass its absolute path with `--root`. Stop the server with Ctrl+C.

On a fresh installation, enter a model connection or choose **Open workspace without a model**. Leave the API key field blank if your service accepts unauthenticated requests.

## Update or replace an existing folder

Extract a trusted release into a separate directory. Stop the destination's server and save open files. From the extracted release, preview the deployment, review the target and replacement paths, then apply the update.

```powershell
$Target = Read-Host "Path to the BAT Agent installation to update"
python scripts/deploy.py --target $Target
python scripts/deploy.py --target $Target --apply
```

The helper compares source files with the release manifest. It stages verified files, creates a timestamped backup next to the destination, and replaces `agent_harness/`, `docs/`, `scripts/`, `web/` and the release's root product and package files. Files present only in the old managed directories move into that backup. The helper rejects linked paths and overlap between source and destination.

By default the helper preserves Git settings, the Python environment, active configuration, conversations, existing Skills and workspace files. It adds missing sample files, seeds a missing root `AGENTS.md` and merges Git ignore and attribute rules.

If you want to start with a fresh configuration and conversation store, preview that change with `python scripts/deploy.py --target $Target --reset-local`. Add `--apply` to move active model, environment and execution YAML files plus `.harness/` into the backup. Existing `LLM_*` environment variables still apply at startup.

Run the environment check in the updated installation, then start the server. The backup can contain private data. For a rollback, stop the server, consult the backup's `DEPLOYMENT.json`, remove the paths it lists as installed and restore the saved paths. A failed apply attempts to undo its changes and reports the backup location.
