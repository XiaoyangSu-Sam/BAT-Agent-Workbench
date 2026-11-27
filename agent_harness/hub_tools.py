"""Execute approved local source on the session's kernel; data files stay in place."""
import base64
import json

from .errors import HarnessError
from .runtime.paths import digest, inside, text_file


# This program runs in the selected kernel. A child interpreter gives scripts their
# own argv, __main__ and exit code without replacing the interactive kernel state.
_SCRIPT_RUNNER = """
import base64, json, os, subprocess, sys, tempfile
loader = (
    "import sys, os; sys.argv.pop(0); "
    "__file__ = os.path.abspath(sys.argv[0]); "
    "exec(compile(sys.stdin.buffer.read().decode('utf-8'), __file__, 'exec'))"
)
with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
    try:
        child = subprocess.run([sys.executable, '-u', '-c', loader, filename, *args],
                               input=source.encode('utf-8'), stdout=out, stderr=err, timeout=timeout,
                               env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        status, timed_out = child.returncode, False
    except subprocess.TimeoutExpired:
        status, timed_out = None, True
    out.seek(0)
    err.seek(0)
    stdout, stderr = out.read(limit+1), err.read(limit+1)
    print(json.dumps(dict(exit_code=status, timed_out=timed_out, ok=status == 0,
          stdout=base64.b64encode(stdout[:limit]).decode('ascii'),
          stderr=base64.b64encode(stderr[:limit]).decode('ascii'),
          output_truncated=max(len(stdout), len(stderr)) > limit)))
"""


def _script_report(result):
    # A truncated envelope or a timeout cannot establish a safe point to rerun from.
    report = json.loads(result["stdout"])
    if (not result["ok"] or not isinstance(report, dict) or type(report.get("exit_code")) is not int
            or report.get("timed_out") is not False):
        raise ValueError("Incomplete script result")
    for stream in ("stdout", "stderr"):
        report[stream] = base64.b64decode(report[stream], validate=True).decode("utf-8", errors="replace")
    return report


class HubTools:
    def __init__(self, sessions, key, files):
        self.sessions = sessions
        self.key = key
        self.files = files

    def source(self, path, expected_sha256, suffix):
        source = inside(self.files.root, path, True)
        if source.suffix.lower() != suffix:
            raise HarnessError("invalid_source", f"Select a local {suffix} file.", "tool")
        data, text = text_file(source)
        if digest(data) != expected_sha256:
            raise HarnessError("file_conflict", "Read the latest local source before executing it.", "tool")
        return source, text

    async def run_jupyter_cell(self, code):
        return await self.sessions.execute(self.key, code)

    async def run_jupyter_script(self, path, expected_sha256, args=None):
        source, text = self.source(path, expected_sha256, ".py")
        config = self.sessions.settings.execution
        limit = config.max_output_chars
        scope = dict(filename=source.name, source=text, args=args or [], limit=limit, timeout=config.timeout)
        # Allow room for two bounded, base64-encoded streams plus the small JSON envelope.
        return await self.sessions.execute(self.key, f"exec({_SCRIPT_RUNNER!r}, {scope!r})",
                                           max_output=3 * limit + 1024, decode=_script_report)

    async def run_jupyter_notebook_cell(self, path, cell_index, expected_sha256):
        _, text = self.source(path, expected_sha256, ".ipynb")
        try:
            notebook = json.loads(text)
            cell = notebook["cells"][cell_index]
            if cell["cell_type"] != "code":
                raise ValueError
            source = cell["source"]
            if isinstance(source, list) and all(isinstance(line, str) for line in source):
                source = "".join(source)
            if not isinstance(source, str):
                raise ValueError
        except (KeyError, TypeError, ValueError, IndexError):
            raise HarnessError("invalid_notebook_cell", "Select a code cell in a valid local notebook.", "tool") from None

        result = await self.sessions.execute(self.key, source)
        cell["outputs"] = result["outputs"]
        cell["execution_count"] = result["execution_count"]
        try:
            saved = self.files.write_file(path, json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", expected_sha256)
        except (HarnessError, OSError):
            raise HarnessError("hub_output_not_saved",
                               "The cell ran on JupyterHub, but its output could not be saved to the same local notebook version. Review the file before rerunning.",
                               "jupyterhub") from None
        return {**result, "path": saved["path"], "sha256": saved["sha256"], "cell_index": cell_index}
