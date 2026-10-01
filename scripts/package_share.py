#!/usr/bin/env python3
"""Build source and plugin archives without private instance data or symlinks."""
import argparse
import json
from pathlib import Path
import zipfile


ROOT_FILES = {"README.md", "GETTING_STARTED.zh-CN.md", "requirements-collector.txt", "LICENSE", "LICENSE.md"}
EXTENSIONS = {
    "app": {".swift", ".sh", ".plist", ".icns"},
    "scripts": {".py"},
    "tests": {".py", ".html", ".json"},
    "skills": {".md"},
    "references": {".md"},
    "examples": {".json", ".md"},
    "assets": {".svg", ".png", ".jpg", ".jpeg", ".webp", ".css", ".js"},
}
PLUGIN_NAMES = ("jupiter-reader", "ticktick-sync")
PLUGIN_ASSET_EXTENSIONS = EXTENSIONS["assets"]
PRIVATE_NAMES = {"state", "work", "reports", "browser-profile", "__pycache__", ".venv", "venv",
                 "state.json", "local-instance.json", "collector-session.json", "run-status.json",
                 "notifications.json", "notification-state.json", "pending-review.json", "config.json",
                 "raw-latest.json", "snapshot-latest.json", "import-metadata.json", "Cookies", "Login Data"}


def _regular_file(path):
    """Return true only for a real file whose path contains no symlink."""
    return path.is_file() and not any(part.is_symlink() for part in (path, *path.parents))


def distributable_files(root):
    root = Path(root).resolve()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
            continue
        if any(part in PRIVATE_NAMES or part.startswith(".") for part in relative.parts if part != ".codex-plugin"):
            continue
        if len(relative.parts) == 1 and relative.name in ROOT_FILES:
            yield path, relative
        elif relative.as_posix() == ".codex-plugin/plugin.json":
            yield path, relative
        elif relative.as_posix() == "assets/dashboard.html":
            yield path, relative
        elif relative.parts[0] in EXTENSIONS and path.suffix in EXTENSIONS[relative.parts[0]]:
            yield path, relative
        elif relative.parts[0] == "plugins" and len(relative.parts) >= 3:
            plugin_name = relative.parts[1]
            plugin_file = relative.parts[2:]
            if plugin_name in PLUGIN_NAMES and (
                    plugin_file == (".codex-plugin", "plugin.json") or
                    (plugin_file[0] == "skills" and path.suffix.lower() == ".md") or
                    (plugin_file[0] == "assets" and path.suffix.lower() in PLUGIN_ASSET_EXTENSIONS)):
                yield path, relative


def package(root, output):
    """Package the source tree for sharing.

    The repository is source-first and is no longer itself a Codex plugin.  A
    source package therefore does not require a root ``.codex-plugin``
    manifest; the two installable plugin packages are built explicitly with
    :func:`package_plugin`.  Keep this function as the backwards-compatible
    source-share entry point used by older setup instructions.
    """
    root = Path(root).resolve()
    if not (root / "scripts").is_dir():
        raise ValueError("Not a source tree: scripts directory is missing")
    files = list(distributable_files(root))
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, relative in files:
            archive.write(path, "jupiter-study-planner/" + relative.as_posix())
    return len(files)


def _plugin_files(root, plugin_name):
    """Return ``(source, archive-relative-path)`` for a plugin release.

    Plugin metadata and skills live under ``plugins/<name>`` while executable
    Python remains canonical under the repository's top-level ``scripts``.
    This keeps the repository from carrying three copies of the runtime while
    producing a completely self-contained plugin archive for installation.
    """
    if plugin_name not in PLUGIN_NAMES:
        raise ValueError(f"Unknown plugin {plugin_name!r}; choose one of {', '.join(PLUGIN_NAMES)}")
    plugin_root = root / "plugins" / plugin_name
    manifest = plugin_root / ".codex-plugin" / "plugin.json"
    scripts = root / "scripts"
    if not _regular_file(manifest):
        raise ValueError(f"Plugin manifest is missing: {manifest}")
    if not scripts.is_dir():
        raise ValueError("Not a source tree: scripts directory is missing")
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid plugin manifest: {manifest}") from exc
    if manifest_data.get("name") != plugin_name:
        raise ValueError(f"Plugin manifest name does not match directory: {plugin_name}")

    files = [(manifest, Path(".codex-plugin/plugin.json"))]
    for path in sorted(plugin_root.rglob("*")):
        relative = path.relative_to(plugin_root)
        if not _regular_file(path) or relative.parts[0] in {"scripts", ".codex-plugin"}:
            continue
        if relative.parts[0] == "assets" and path.suffix.lower() in PLUGIN_ASSET_EXTENSIONS:
            files.append((path, relative))
        elif relative.parts[0] == "skills" and path.suffix.lower() == ".md":
            files.append((path, relative))

    # Include the deterministic runtime and its two files used by optional
    # dashboard generation.  The runtime imports sibling modules, so all
    # Python files are intentionally copied into each release.
    for path in sorted(scripts.glob("*.py")):
        if _regular_file(path):
            files.append((path, Path("scripts") / path.name))
    requirements = root / "requirements-collector.txt"
    if _regular_file(requirements):
        files.append((requirements, Path("requirements-collector.txt")))
    dashboard = root / "assets" / "dashboard.html"
    if _regular_file(dashboard):
        files.append((dashboard, Path("assets/dashboard.html")))
    return files


def package_plugin(root, plugin_name, output):
    """Build one standalone installable plugin from canonical root scripts."""
    root = Path(root).resolve()
    files = _plugin_files(root, plugin_name)
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, relative in files:
            archive.write(path, f"{plugin_name}/{relative.as_posix()}")
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plugin", choices=PLUGIN_NAMES,
                        help="build a standalone plugin using the canonical root scripts")
    args = parser.parse_args()
    if args.plugin:
        count = package_plugin(args.root, args.plugin, args.output)
        print(f"插件包已生成（{args.plugin}），包含 {count} 个代码/技能/素材文件：{args.output}")
    else:
        count = package(args.root, args.output)
        print(f"源码分享包已生成，包含 {count} 个代码/文档/测试/素材文件：{args.output}")


if __name__ == "__main__":
    main()
