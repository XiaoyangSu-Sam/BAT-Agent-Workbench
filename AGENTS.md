# Working on BAT Agent

Use this guide when changing the BAT Agent repository. Start with the user's requested outcome and the applicable instructions in the destination checkout. The [README](README.md) covers everyday use.

## Find the implementation

| If the change concerns | Start here |
| --- | --- |
| Chat Completions, tool calls, model settings | `agent_harness/runtime/model.py`, `agent_harness/runtime/context.py`, `agent_harness/config.py`. For NVIDIA, read the [Nemotron contract](docs/nemotron-compatibility.md). |
| Tool definitions, approvals or process execution | `agent_harness/runtime/tools.py`, `turn.py`, `manager.py` and `processes.py` in the same directory. The runtime owns approval decisions. |
| API, conversation storage or UI | `agent_harness/server.py`, `agent_harness/store.py`, `agent_harness/static/`. Editor and Markdown bundle sources live in `web/`. |
| Source updates and releases | [Existing environment](docs/existing-environment.md), [development and release workflow](docs/development.md), [Copilot setup](COPILOT_SETUP.md). |

## Make and verify a change

1. Check the active workspace and `git status --short --branch`. Read relevant files and nearby tests. Preserve unrelated edits. For BAT's file tools, use the SHA256 from the latest read when changing an existing file.
2. Use the project's existing approved Python 3.11+ interpreter. Keep its environment in place. From the checkout root, run the following in PowerShell, using the full interpreter path for both commands.

   ```powershell
   $BatPython = Read-Host "Full path to the existing approved python.exe"
   & $BatPython scripts/check_environment.py
   & $BatPython -m pytest -q
   ```

   Check that the environment report says `"ok": true`. For a targeted change, run the relevant test module first. For workbench UI changes, also run the [browser smoke check](docs/development.md#browser-smoke-check).
3. Use disposable workspace data for tests. Keep credentials, real configuration, conversations and private Skills out of diffs and reports. Keep public documentation and UI text in English.
4. Report the changed paths, the commands that actually ran, their results, and what still needs a live endpoint or user review. A mock tool round trip and a successful live model request establish different things.

## Publishing code and prompts

Use fictional values and repository-relative paths in public code, tests, examples, screenshots and instructions. Review Git authorship, commit messages and tag annotations before publishing. Keep identifying details, real workflows and machine settings in a private handoff outside every repository. Refer to credentials through environment variables or a local secret store.

For updates, publish the changed-file list and a reusable prompt with placeholders. Prepare the filled-in operator prompt separately and share it only through the intended private channel. Follow the [release workflow](docs/development.md#release-workflow) to build and verify the exact files in the archive.

## When BAT is editing BAT

Select a separate BAT source checkout as the workspace and start a new conversation. In BAT v0.7.3, explicitly ask the assistant to read this file with `read_file`. The runtime does not automatically load repository instructions. Keep the original server running for the editing session and use a separate process to test the candidate. The [local iteration guide](docs/harness-development.md) gives a first task, a prompt to paste, and a proposed Computer Use implementation.
