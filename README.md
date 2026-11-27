# BAT Agent

v0.7.7

BAT Agent is a local coding workbench. Chat with a model about the files in your workspace, ask it to make an edit, or run a Python script and inspect the result. Conversation history is on the left. Files open in a right-hand editor that you can expand or close.

**Working on this repository as a coding agent?** Read [AGENTS.md](AGENTS.md) before making changes.

## Get started

From the source folder, check the Python environment you already use for this project.

```powershell
$BatPython = Read-Host "Full path to the project's python.exe"
& $BatPython scripts/check_environment.py
```

When the check reports `"ok": true`, start the workbench.

```powershell
& $BatPython -m agent_harness
```

Open [the local workbench](http://127.0.0.1:8765). Leave the terminal open while you work. Ctrl+C stops the server. For a first installation or a failed dependency check, see [Setup](docs/setup.md). To update an existing installation, see [Updating in place](docs/existing-environment.md).

## Work with a model

Enter the endpoint and model ID from your OpenAI-compatible service. Supply an API key if the service requires one. **Find models** can list available IDs. Choose **Test connection**, then **Open workspace**. You can save multiple connections and switch models from chat.

Choose **Open workspace without a model** to browse files, edit code and inspect installed Skills. To try a coding task once connected, ask: “Read this script, explain what it does, and run its tests.” The activity panel shows tool calls and their results. You choose when the agent needs approval to edit or execute.

## Guides

- [Using chat, files and approvals](docs/user-guide.md)
- [Enable web search and get source links in chat](docs/web-search.md)
- [Import Skills from a Git repository, ZIP or folder](docs/skills.md#import-skill-folders)
- [Diagnosing a connection](docs/connections.md)
- [Run local project code on JupyterHub](docs/jupyterhub.md)
- [Update a customized installation with the v0.7.7 feature patch](docs/updates/v0.7.7.md)
- [Security](SECURITY.md) and [changelog](CHANGELOG.md)

BAT Agent is a single-user application. Scripts you approve run with your account's permissions.
