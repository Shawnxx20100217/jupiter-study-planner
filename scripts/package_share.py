#!/usr/bin/env python3
"""Package only distributable plugin files; never include instance data or symlinks."""
import argparse
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
PRIVATE_NAMES = {"state", "work", "reports", "browser-profile", "__pycache__", ".venv", "venv",
                 "state.json", "local-instance.json", "collector-session.json", "run-status.json",
                 "notifications.json", "notification-state.json", "pending-review.json", "config.json",
                 "raw-latest.json", "snapshot-latest.json", "import-metadata.json", "Cookies", "Login Data"}


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


def package(root, output):
    root = Path(root).resolve()
    manifest = root / ".codex-plugin" / "plugin.json"
    if not manifest.is_file():
        raise ValueError("Not a plugin directory: .codex-plugin/plugin.json is missing")
    files = list(distributable_files(root))
    if not any(relative.as_posix() == ".codex-plugin/plugin.json" for _, relative in files):
        raise ValueError("Plugin manifest is not a regular distributable file")
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, relative in files:
            archive.write(path, "jupiter-study-planner/" + relative.as_posix())
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = package(args.root, args.output)
    print(f"分享包已生成，包含 {count} 个代码/文档/测试/素材文件：{args.output}")


if __name__ == "__main__":
    main()
