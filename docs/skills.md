# Skills

A Skill is a reusable instruction directory, optionally with scripts, notebooks and references. Instructions guide the agent; they are not automatically executed.

~~~text
skills/
  sum-numbers/
    SKILL.md
    scripts/
      calculate.py
    notebooks/
    references/
~~~

SKILL.md must have that exact filename. The folder name is the Skill id.

~~~markdown
---
name: Sum numbers
description: Calculate the sum from 1 to N and verify the result with Python.
---
Read scripts/calculate.py before running it. Ask for N if it is missing.
Run the script with run_python_script using skill_id sum-numbers.
Report the actual output and exit code.
~~~

YAML frontmatter is recommended. Plain Markdown is supported; the first heading becomes the name and a non-heading paragraph supplies the description.

## Create through conversation

Ask: “Create a Skill called sum-numbers, with SKILL.md and scripts/calculate.py. The script should accept N and print the sum from 1 to N. Test it for N = 100.”

The agent uses write_skill_file. A valid SKILL.md registers immediately; resources can then be written below scripts/, notebooks/ and references/. The registered Skills panel refreshes after a Skill write. No server restart is needed. Existing files require their current SHA256 to prevent overwriting concurrent changes.

For manual installation, copy a complete Skill directory into skills/, then Refresh Skills. Invalid instructions show a warning. The registry is capped at 200 Skills and lists up to 200 resource names per Skill.

## Read, edit and execute

| Tool | Purpose |
| --- | --- |
| list_skills | List registered ids/names/descriptions. |
| read_skill | Read instructions, resource names and SKILL.md SHA256. |
| read_skill_resource | Read a resource and its SHA256. |
| write_skill_file | Create/update instructions or resources; refresh registration. |
| run_python_script | Execute a workspace script or a Skill’s scripts/*.py using skill_id. |
| run_notebook | Execute a workspace notebook or a Skill’s notebooks/*.ipynb. |

Example arguments:

~~~json
{"skill_id":"sum-numbers","path":"scripts/calculate.py","args":["100"]}
~~~

Execution uses the selected workspace as cwd, not the Skill directory. Scripts should resolve resources explicitly or accept input paths. Skill instructions cannot change the approval policy. By default, execution requires approval; the all policy also requires approval for writing a Skill.

Notebook support is optional:

~~~powershell
python -m pip install -e ".[notebook]"
python -m agent_harness
~~~

Notebook output is saved separately under workspace/notebook-results/. Source notebooks are preserved. Install any Skill-specific dependencies into this project’s environment before execution.

Skill ids created by the write tool use 1–80 letters/numbers/hyphens/underscores and start with a letter or number. Writes cannot escape the Skill directory or target protected files. Paths are guarded; subprocess execution still has the current user’s operating-system privileges.

## Import Skill folders

Open **Skills → Import Skills**. This also works in the workbench without a model. Select a source, preview the available Skills, select one or more rows (or all of them) and install. The folder containing each `SKILL.md` becomes `skills/<folder-name>/`. A repository ZIP can have a wrapper directory. A ZIP with `SKILL.md` at its root uses the ZIP filename without its extension as the Skill id.

| Source | How to use it |
| --- | --- |
| Configured repository | Save the HTTPS or SSH Git clone URL and an optional subfolder such as `skills`. BAT reads the default branch using the credentials already available to Git on this machine. First verify that Git can access the repository under the account that runs BAT. |
| ZIP file | Select the downloaded ZIP from your computer. It can contain several independent Skill directories. The upload limit is 64 MiB. |
| Extracted folder | Enter the directory on the BAT machine. It can be one Skill or a parent directory containing several. |

Chat can also preview and install by name. Try “List the Skills in the configured repository, then install the one called chart” or “Install all Skills from the workspace file `sample.zip`.” Chat installation follows the conversation's execution approval setting. Use the panel for a ZIP outside the workspace.

For a large repository, set the Skills subfolder to narrow the exported contents. A source is limited to 10,000 entries and 64 MiB of expanded files. The installed registry supports 200 Skills. Chat can search a large installed catalog by id, name or description. If a Skill includes Git LFS assets, download the actual files with your normal Git client and import the hydrated folder or a ZIP made from it.

Repository previews and browser ZIP uploads use a temporary snapshot. For a folder or workspace file, BAT reads the selected paths when you install. Finish editing the source before importing it.

**Installed** means BAT copied the complete folder and refreshed the registry. **Identical** means the same files are already present. **Conflict** means the destination folder has different contents, including extra local files. BAT keeps that folder and continues with other new Skills. When two source folders would have the same destination name, select only the one you intend to install.

The importer copies nested resources and binary assets as files. It uses Python and the existing Git executable for repository access. Importing does not execute Skill scripts or install runtime dependencies. A particular Skill may still require packages when you choose to run it.

Store a real repository URL in the local workbench settings. Use Git's configured credential helper or SSH agent for authentication. The public source update contains the importer, and the configured repository and imported Skills stay on the destination installation.

## Copy an existing Skill

Place the complete folder directly under skills/, so its path is skills/<id>/SKILL.md. A ZIP wrapper such as skills/archive-name/<id>/SKILL.md will not register. Keep scripts/, references/ and notebooks/ alongside SKILL.md. Click Refresh Skills after copying. This registry is separate from VS Code/Copilot .github/skills/.

Use config/execution.yaml to set python_executable to an interpreter with the Skill dependencies. The server interpreter is used when it is empty. Python Playwright is only needed for Skills that import it; it is not needed by the workbench. No npm/npx command is required by BAT Agent. Filesystem and network permissions still apply.
