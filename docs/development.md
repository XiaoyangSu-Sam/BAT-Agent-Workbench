# Development and documentation

## Current behavior

BAT Agent is a single-user local application with one active workspace. Conversation is the primary screen. History is searchable on the left. Files open only when requested, in a right pane that can resize, expand, or close without discarding open drafts.

The model writes a Python file with write_file or edit_file, then requests run_python_script to execute it with the configured interpreter. A displayed code block is not evidence that a file was created or executed. Report actual tool results, exit status and relevant output. Skills supply instructions and resources; their dependencies must be available to the interpreter that runs them. This release does not add a package installer or change Playwright setup.

New conversations inherit the project approval policy. An explicit selection applies only to that conversation and clears its earlier Allow all grant. A pending execution approval can grant Allow all when the policy permits it. The grant persists across reloads and restarts. Keep approval decisions in the runtime and outside model output or Skill instructions.

## Next work

To check a model connection before developing a new mode, run **Test connection** and **Test model and tools**, then try a multi-turn text exchange. For Nemotron deployments, see the [compatibility contract](nemotron-compatibility.md).

1. **Plan Mode next.** Ordinary chat already asks the model to identify the goal, constraints and success checks for multi-step work. Add an explicit mode that exposes only read tools. Return a short plan with decisions, open questions and acceptance criteria. A user action switches from planning to execution, where the selected approval policy applies. Test the restriction at the runtime boundary for both native and JSON tools.
2. **Conversation compaction after that.** Start with compaction on demand within one conversation. Preserve the original transcript, retain recent complete turns and tool call/result pairs, and summarize decisions, constraints, changed files, test evidence and open work. Define and test when an automatic threshold should offer compaction. Store the summary with provenance and support rebuilding it from the transcript. Keep permission state and secrets outside the summary. Measure long-chat recovery and token use before enabling automation.

Compaction keeps an individual conversation useful as it grows. Cross-conversation memory is a separate feature with its own scope and controls.

## README rules

- Write public UI, source comments and documentation in English.
- Write the [README](../README.md) for people using BAT. Near the top, direct coding agents to [AGENTS.md](../AGENTS.md) for repository instructions. Review the two files together when commands or entry points change.
- Keep the README focused on the product name, version, features, startup command and links to the guides.
- Put setup details, API contracts, architecture, validation and future plans in docs. Link them rather than duplicating long instructions.
- Prefer the existing project Python environment. Do not require a new environment or dependency sync for a source-only update.
- Describe tested behavior accurately. Distinguish local mock tests, actual subprocess execution and live service verification. Do not call planned features implemented or claim a service was tested without evidence.
- Use placeholders for configuration. Keep private addresses, credentials, conversations, workspace contents and machine-specific paths out of published documentation.

## Release workflow

1. Inspect the current branch and local changes. Preserve unrelated work. Identify the prior release baseline.
2. Implement behavior, meaningful regression checks and matching documentation. Update version metadata together; keep dependency changes explicit.
3. Run Python tests and the local browser fixture. Check chat, editing, approvals, reload, workspace switching and narrow-screen layout. Rebuild bundled assets from web sources when they change.
4. Write a guide in docs/updates listing the release's added, modified and deleted files. Use repository-relative links that also work when viewed at the release tag. Include a short, reusable update prompt.
5. Stage the reviewed source changes. To refresh the manifest and include a new guide, run `python scripts/package_release.py --refresh-manifest --include docs/updates/vX.Y.Z.md`, replacing `vX.Y.Z` with the release tag. Repeat `--include` for each intentionally added file. Review and stage `SOURCE-MANIFEST.json`, then run `python scripts/package_release.py` to build the release using the approved file list and verify the hashes. Keep deployment-specific material outside the repository.
6. Review the staged diff, publishing identity and secret scan. Commit and tag the tested source; publish only when authorized. Do not publish local state or private backups.

Keep deployment-specific prompts and working notes outside the release repository. Before updating an installed app, back up ignored configuration, Skills, workspace data and the stopped SQLite state separately.

## Browser smoke check

From the repository root, start `scripts/workbench_fixture.py` with the project interpreter. In a second terminal at the same root, run `node scripts/workbench-smoke.cjs <path-to-installed-playwright-module>`. Use the existing Chromium installation for that Playwright version. The fixture uses loopback ports 18767 and 18768 and disposable data. Stop it with Ctrl+C after the check.

For a BAT-driven change, follow the [local iteration guide and Computer Use proposal](harness-development.md).
