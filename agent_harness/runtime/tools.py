import inspect
from jsonschema import Draft202012Validator
from .files import FileTools
from .processes import ProcessTools
from .web_search import MAX_QUERY_CHARS, configured_search_provider, web_search
from ..errors import HarnessError
from ..hub_tools import HubTools
from ..skill_import_routes import run_worker

S = {"type": "string"}
def string(maximum=10000, minimum=0):
    return dict(type="string", minLength=minimum, maxLength=maximum)

class ToolRegistry:
    def __init__(self, settings, skills, *, hubs=None, session_id=None, imports=None):
        self.entries = {}
        self.files = FileTools(settings.workspace, settings.state_dir)
        self.hub_status = hubs.status(session_id) if hubs is not None else {"target": "local"}
        def add(name, desc, properties, required, fn, kind="read"):
            schema = dict(type="object", properties=properties, required=required, additionalProperties=False)
            self.entries[name] = dict(description=desc, schema=schema, fn=fn, kind=kind)
        path = string(1000, 1)
        sha = {"type":"string", "pattern":"^(|[a-f0-9]{64})$"}
        add("list_files", "List files under a workspace directory.", {"path":path}, [], self.files.list_files)
        add("read_file", "Read UTF-8 text and its SHA256 before editing.",
            {"path":path, "start_line":{"type":"integer","minimum":1},
             "end_line":{"type":"integer","minimum":1}}, ["path"], self.files.read_file)
        add("search_files", "Search literal text in workspace files.",
            {"query":string(500,1),"path":path}, ["query"], self.files.search_files)
        if configured_search_provider():
            add("web_search", "Send a public search query to the configured search provider and return up to five source titles, URLs and snippets.",
                {"query":string(MAX_QUERY_CHARS,1)}, ["query"], web_search, "execute")
        add("write_file", "Create a file; overwrite requires the current SHA256 from read_file.",
            {"path":path,"content":string(1000000),"expected_sha256":sha},
            ["path","content"], self.files.write_file, "write")
        add("edit_file", "Replace exactly one occurrence of old_text. Requires current SHA256.",
            {"path":path,"old_text":string(100000,1),"new_text":string(100000),"expected_sha256":sha},
            ["path","old_text","new_text","expected_sha256"], self.files.edit_file, "write")
        add("list_skills", "Find installed Skills by id, name or description. Use next_offset for more results.",
            {"query": string(500), "offset": {"type": "integer", "minimum": 0}}, [], skills.find_skills)
        if imports is not None:
            async def available(**args):
                return await run_worker(lambda: imports.chat_preview(session_id, **args))
            async def install(ticket, selected):
                return await run_worker(imports.chat_apply, session_id, ticket, selected)
            add("list_available_skills", "Preview Skills from the configured repository or a workspace-relative folder or ZIP. Search by query. Reuse ticket and next_offset for further pages.",
                {"source": {"type": "string", "enum": ["repository", "workspace"]}, "path": path, "ticket": string(128, 1),
                 "query": string(500), "offset": {"type": "integer", "minimum": 0}}, [], available)
            add("install_skills", "Install complete folders by exact preview path. ['*'] installs the entire source, including unlisted pages: use only when explicitly requested. Differing local folders are preserved.",
                {"ticket": string(128, 1), "selected": {"type": "array", "items": string(4096, 1), "minItems": 1, "maxItems": 10000}},
                ["ticket", "selected"], install, "execute")
        add("read_skill", "Load instructions and resource names for an installed skill.",
            {"skill_id":path}, ["skill_id"], skills.read_skill)
        add("read_skill_resource", "Read a skill resource relative to its own directory.",
            {"skill_id":path,"path":path}, ["skill_id","path"], skills.read_skill_resource)
        add("write_skill_file", "Create or update SKILL.md or a skill resource. Existing files require SHA256 from read_skill/read_skill_resource. Registers valid skills immediately.",
            {"skill_id":string(80,1), "path":path, "content":string(1000000), "expected_sha256":sha},
            ["skill_id","path","content"], skills.write_skill_file, "write")
        args = {"type":"array","items":string(10000),"maxItems":100}
        if self.hub_status["target"] != "local":
            if self.hub_status["connected"]:
                remote = HubTools(hubs, session_id, self.files)
                source = {"path": path, "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}
                add("run_jupyter_cell", "Execute this code on the conversation's persistent JupyterHub kernel. Files opened by code are on the remote server.",
                    {"code": string(100000, 1)}, ["code"], remote.run_jupyter_cell, "execute")
                add("run_jupyter_script", "Send one local Python script's source to a child of the selected Python kernel. Paths and imported modules resolve on the remote server; transfer dependencies manually in JupyterLab.",
                    {**source, "args": args}, list(source), remote.run_jupyter_script, "execute")
                add("run_jupyter_notebook_cell", "Execute one local notebook code cell on the selected JupyterHub kernel and save outputs to the same local notebook. Cell index starts at zero and counts every cell.",
                    {**source, "cell_index": {"type": "integer", "minimum": 0}},
                    [*source, "cell_index"], remote.run_jupyter_notebook_cell, "execute")
            return
        self.processes = ProcessTools(settings.workspace, settings.execution, skills)
        add("run_python_script", "Execute Python in the workspace, optionally from an installed skill's scripts/.",
            {"path":path,"args":args,"skill_id":string(200)}, ["path"],
            self.processes.run_python_script, "execute")
        add("run_command", "Run argv without a shell. cwd is workspace-relative. Commands access your user account.",
            {"argv":dict(args,minItems=1),"cwd":path}, ["argv"], self.processes.run_command, "execute")
        add("run_notebook", "Execute a notebook and save a separate output copy. Requires optional notebook dependencies.",
            {"path":path,"skill_id":string(200)}, ["path"], self.processes.run_notebook, "execute")

    def schemas(self):
        return [{"type":"function","function":{"name":name,"description":e["description"],
                  "parameters":e["schema"]}} for name,e in self.entries.items()]

    def validate(self, name, args):
        if name not in self.entries:
            raise HarnessError("unknown_tool", "Unknown tool name.", "tool")
        errors = list(Draft202012Validator(self.entries[name]["schema"]).iter_errors(args))
        if errors:
            raise HarnessError("invalid_tool_arguments", "Arguments do not match the tool schema.", "tool",
                               fields=[".".join(map(str,e.path)) or "(root)" for e in errors[:5]])
        return self.entries[name]

    async def call(self, name, args):
        entry = self.validate(name, args)
        result = entry["fn"](**args)
        return await result if inspect.isawaitable(result) else result
