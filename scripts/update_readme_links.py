#!/usr/bin/env python3
"""Update README.md to point to submodule READMEs at their exact commits.

Usage: run this from the repository root. Exits 0 on success.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys


REPO_ROOT = os.path.abspath(os.path.dirname(__file__) + '/../')
README_PATH = os.path.join(REPO_ROOT, 'README.md')

# Map of submodule relative path -> upstream GitHub "owner/repo"
SUBMODULES = {
    os.path.join('open_road_editor', 'external', 'ORBIT'): 'RI-SE/ORBIT',
    os.path.join('open_road_editor', 'external', 'osm-to-xodr'): 'das-rise/osm-to-xodr',
}


def get_submodule_commit(path: str) -> str | None:
    try:
        out = subprocess.check_output(['git', '-C', path, 'rev-parse', 'HEAD'], text=True)
        return out.strip()
    except Exception:
        return None


def update_readme(submodule_commits: dict[str, str]) -> bool:
    """Update README.md links for multiple submodules.

    `submodule_commits` maps relative submodule path -> commit hash.
    """
    if not os.path.isfile(README_PATH):
        print('README.md not found', file=sys.stderr)
        return False
    with open(README_PATH, 'r', encoding='utf-8') as f:
        content = f.read()

    new_content = content

    for rel_path, commit in submodule_commits.items():
        upstream = SUBMODULES.get(rel_path)
        if not upstream or not commit:
            continue
        repo_name = upstream.split('/')[-1]
        display = repo_name  # markdown link text expected in README
        replacement = f'[{display}](https://github.com/{upstream}/blob/{commit}/README.md)'

        # Replace links that reference either the submodule path or the upstream repo
        pattern = re.compile(
            rf'\[{re.escape(display)}\]\([^)]*(?:{re.escape(rel_path)}|github.com/.+/{re.escape(display)})[^)]*\)'
        )
        if pattern.search(new_content):
            new_content = pattern.sub(replacement, new_content)
        else:
            # Fallback: replace first occurrence of [display](...)
            new_content = re.sub(
                rf'\[{re.escape(display)}\]\([^)]*\)', replacement, new_content, count=1
            )

    if new_content != content:
        with open(README_PATH, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print('README.md updated')
        return True
    print('No change necessary')
    return False


def main() -> int:
    submodule_commits: dict[str, str] = {}
    for rel_path in SUBMODULES.keys():
        abs_path = os.path.join(REPO_ROOT, rel_path)
        commit = get_submodule_commit(abs_path)
        if commit:
            submodule_commits[rel_path] = commit
        else:
            print(f'Warning: could not get commit for {rel_path}', file=sys.stderr)

    if not submodule_commits:
        print('No submodule commits found', file=sys.stderr)
        return 2
    update_readme(submodule_commits)
    return 0


if __name__ == '__main__':
    sys.exit(main())
