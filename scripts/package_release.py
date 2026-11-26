"""Build a source archive from the release manifest, or refresh it from staged files."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tomllib
import zipfile

if __package__:
    from . import deploy
else:
    import deploy

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "SOURCE-MANIFEST.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def refresh_manifest(root, additions):
    """Keep the committed release list. New entries require an explicit path."""
    previous = json.loads(git(root, "show", "HEAD:" + MANIFEST))
    tree = git(root, "write-tree").decode("ascii").strip()
    staged = {}
    for entry in git(root, "ls-tree", "-r", "-z", tree).split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            mode, _, oid = metadata.decode("ascii").split()
            staged[name.decode("utf-8")] = (mode, oid)
    for name in additions:
        if name not in staged:
            raise ValueError("Stage the new release file first: " + name)
    manifest = {}
    for name in sorted((previous.keys() & staged.keys()) | set(additions)):
        path = PurePosixPath(name)
        mode, oid = staged[name]
        if (not name or name == MANIFEST or str(path) != name or path.is_absolute()
                or ".." in path.parts or "\\" in name or ":" in name
                or not deploy.permitted(name) or mode not in ("100644", "100755")):
            raise ValueError("Invalid release source: " + name)
        manifest[name] = digest(git(root, "cat-file", "blob", oid))
    if not {"agent_harness/__main__.py", "pyproject.toml", "scripts/check_environment.py"} <= manifest.keys():
        raise ValueError("The staged release lacks required source files.")
    (root / MANIFEST).write_bytes((json.dumps(manifest, indent=2) + "\n").encode())
    return manifest


def package(root):
    manifest_bytes = (root / MANIFEST).read_bytes()
    manifest = deploy.verify_source(root)
    if manifest != json.loads(manifest_bytes):
        raise ValueError("Source manifest changed during verification.")
    payloads = {}
    for name, expected in manifest.items():
        data = deploy.contained(root, name).read_bytes()
        if digest(data) != expected:
            raise ValueError("Source changed during packaging: " + name)
        payloads[name] = data
    version = tomllib.loads(payloads["pyproject.toml"].decode())["project"]["version"]
    archive = root.parent / ("bat-agent-v" + version + ".zip")
    payloads[MANIFEST] = manifest_bytes
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
        for name, data in payloads.items():
            output.writestr("BAT-Agent/" + name, data)
    with zipfile.ZipFile(archive) as check:
        if check.testzip() or len(check.namelist()) != len(payloads):
            raise ValueError("Release archive verification failed.")
    checksum = digest(archive.read_bytes())
    archive.with_suffix(".zip.sha256").write_text(checksum + "  " + archive.name + "\n", encoding="ascii")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-manifest", action="store_true", help="Hash staged release sources and exit")
    parser.add_argument("--include", metavar="PATH", action="append", default=[],
                        help="Add this staged file to the release when refreshing the manifest")
    args = parser.parse_args()
    if args.refresh_manifest:
        print(f"Recorded {len(refresh_manifest(ROOT, args.include))} staged source files.")
    elif args.include:
        parser.error("--include requires --refresh-manifest")
    else:
        print("Verified release archive:", package(ROOT).name)


if __name__ == "__main__":
    main()
