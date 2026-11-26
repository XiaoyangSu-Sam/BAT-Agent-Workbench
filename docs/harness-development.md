# Use BAT Agent to develop BAT Agent

The running workbench can edit another checkout of its own source with its existing tools. The model proposes calls, the runtime applies the conversation's approval policy, and the tools return file contents, diffs and process results. A new Python process imports the edited code. This workflow works independently of Plan Mode or desktop control.

## Set up the editing session once

1. Keep the current workbench running. In a terminal at the BAT repository root, check `git status --short --branch`. To start from the current commit, create a separate checkout with `git worktree add -b bat/self-dev ../bat-self-dev HEAD`. These example names must be available. Uncommitted edits in the original checkout stay there. If you already have a separate development checkout, use it directly.
2. In the running workbench, choose **Change folder**, enter the absolute path of the development checkout, then choose **New conversation**. BAT has one active workspace. Finish any other task before switching. The checkout supplies source files. The original workbench supplies its configured model and interpreter.
3. Select a saved model with native tool support. **Test connection** checks text chat. **Test model and tools** in Native mode checks that this endpoint can request a tool and use its returned value. Choose **Approval for me** if you want the model to edit files and present each command before execution.

## Example development task

An easy change to verify is a CLI version flag. The v0.7.3 command supports `--help` and has no `--version` option. Paste this into the new BAT chat:

```text
Read AGENTS.md in the active workspace with read_file. Confirm this checkout
contains agent_harness/__main__.py and inspect git status. Implement only
python -m agent_harness --version. Print the existing package version and
exit successfully without starting a server or requiring model configuration.
Keep normal startup and --help working. Add a focused regression test.

Use the existing configured Python. You can get its full path by running
scripts/check_environment.py with run_python_script. Use run_command with
that executable to run the new test and the CLI from this checkout. Inspect
stdout and exit codes. Report the changed paths, diff and actual results.
Leave the changes local for review and keep the running workbench up.
```

For later tasks, replace the feature and acceptance checks in the first paragraph. If the candidate needs a UI test, start the same Python interpreter from the **development checkout** in a second terminal with `-m agent_harness --root . --port 8877` (choose a free port). This uses the candidate's local configuration and SQLite state. Use **Open workspace without a model** for an offline UI check. For a live check, configure the model connection locally. Confirm the expected behavior, including a request to `/health`, then stop that candidate with Ctrl+C. Review the patch before importing it into the usual installation and restarting that instance. To abandon a candidate, keep using the original checkout.

## Proposed next feature: browser Computer Use

A Skill can use Playwright to run a scripted browser test. With interactive Computer Use, the model observes the current view, requests one action and inspects the result before choosing the next step. A Skill can explain the workflow. BAT must supply the driver, a way to send observations, and a way to stop the session. OpenAI documents this division of work for its own API. [Computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)

Build and test these increments separately:

| Increment | Implementation | Acceptance check |
| --- | --- | --- |
| 1. Prove image input on the actual endpoint | Use the built-in **Test image and tools** check in model settings with the saved Native connection. See the [diagnostic contract](nemotron-compatibility.md#verify-a-live-deployment). | The live model reads the random digits, requests a synthetic tool, and uses its returned value in the next reply. Record the result on your deployment. |
| 2. Observe and act in one browser page | Register an optional browser tool backed by Playwright. Give each run its own browser context. Start with observe, click and type. Send a bounded screenshot as an image, label its dimensions, and map coordinates to the captured viewport. Keep image bytes in memory and out of normal activity and conversation logs. | On a local fixture page, the model reads a randomized button label, clicks it and reports a new token visible only after the click. Verify the resulting page state independently. |
| 3. Handle mistakes and cancellation | Bind actions to the page and observation that produced them. Reject stale observations and unexpected destinations. Route actions through the runtime approval policy. Set a step limit and close the run's browser context on completion, denial or Stop. | Exercise a stale click, a denied action, a timeout and Stop. Verify the page receives no extra input afterwards. |
| 4. Add Windows apps if needed | Provide a separate optional driver using Windows UI Automation and capture of the selected window. Start with one disposable test app. | Read a visible value, edit one field and verify the result. Switch focus mid-task and confirm the driver declines input to a different window. |

BAT currently serializes tool results as JSON text. For native tools, keep the matched `tool_call_id` and a short text result. Extend message assembly to supply the image as a multimodal observation on the next request, preserving the model's tool/reasoning contract. Test history replay and context accounting without inserting full base64 strings into persisted transcripts. Give the model captured page content as untrusted observations. The existing approval setting also applies to shell commands, which run with the user's OS permissions.

NVIDIA documents image content parts for Nemotron 3.5 Super VL. Image and tool support on a particular gateway, permitted image size, and click accuracy still need live measurement. [NVIDIA NIM image request example](https://docs.nvidia.com/nim/large-language-models/2.0.13/get-started/advanced/get-started-nemotron-3.5-super-vl#send-an-image-or-video) · [BAT's Nemotron transport contract](nemotron-compatibility.md)
