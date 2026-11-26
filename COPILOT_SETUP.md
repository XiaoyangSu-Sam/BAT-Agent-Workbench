# Set up BAT Agent with a coding assistant

Open the BAT Agent checkout in your editor. Give the assistant the following prompt.

```text
Read AGENTS.md and docs/setup.md. Check the project's Python interpreter
with scripts/check_environment.py. If dependencies are missing, identify
them and follow the installation steps for this environment.

Start BAT Agent, check /health and open the workspace without a model.
Use a disposable file to verify that the editor can open and save it.
Report the version and the checks you ran. Keep credentials, local
configuration, conversations and workspace data out of commits and logs.
```

Enter the endpoint, model ID and any API key directly in the workbench. **Test connection** checks a text reply. **Test model and tools** checks whether a saved model can call a tool and use its result. To update from v0.7.4, use the [v0.7.5 changed-file list and update prompt](docs/updates/v0.7.5.md).
