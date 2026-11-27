#!/usr/bin/env python3
"""The fork's daily rebase: main and every branch onto upstream main.

main is upstream main plus fork-only commits; it is replayed and pushed without checks. Every
other branch is a feature branch, possibly stacked on another one. A branch is stacked on the
branch whose old tip is its closest strict ancestor; it is replayed onto that branch's new tip.
A root branch is replayed onto upstream main, dropping main's fork commits if it started from
main. A branch whose rebase conflicts or whose checks fail keeps its old tip, and the branches
stacked on it wait. Anything that needs a person goes to report.md, and the exit code is 1.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

UPSTREAM = "https://github.com/tangency-labs/solidcraft.git"
LOG_TAIL = 80


def git(*args, cwd=None, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
    return result


def out(*args, cwd=None):
    return git(*args, cwd=cwd).stdout.strip()


def is_ancestor(a, b):
    return git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def rebase(worktree, onto, base, tip):
    """The new tip, or the files that conflict."""
    git("reset", "--hard", "--quiet", cwd=worktree)
    git("clean", "-fdxq", cwd=worktree)
    if git("rebase", "--onto", onto, base, tip, cwd=worktree, check=False).returncode == 0:
        return out("rev-parse", "HEAD", cwd=worktree), None
    conflicts = out("diff", "--name-only", "--diff-filter=U", cwd=worktree).splitlines()
    git("rebase", "--abort", cwd=worktree)
    return None, conflicts or ["(no conflicting file reported)"]


def run(command, worktree):
    print("  $", " ".join(command), flush=True)
    return subprocess.run(command, cwd=worktree, capture_output=True, text=True)


def failed(command, result):
    return " ".join(command), "\n".join((result.stdout + result.stderr).splitlines()[-LOG_TAIL:])


def checks(worktree, upstream, metadata):
    """The commands for the packages the branch changes, in the order they run."""
    changed = out("diff", "--name-only", f"{upstream}...HEAD", cwd=worktree).splitlines()
    root = Path(worktree).resolve()
    dirs = sorted(
        ((Path(p["manifest_path"]).parent.relative_to(root).as_posix(), p["name"]) for p in metadata["packages"]),
        key=lambda d: -len(d[0]),
    )
    libs, apps, app_tests = set(), set(), []
    for file in changed:
        package = next((d for d in dirs if file.startswith(d[0] + "/")), None)
        if package is None:
            continue
        directory, name = package
        if directory.startswith("apps/"):
            apps.add(name)
            test = Path(file)
            if test.parent.as_posix() == f"{directory}/tests" and test.suffix == ".rs" and (root / file).exists():
                app_tests.append(["cargo", "test", "-p", name, "--test", test.stem])
        else:
            libs.add(name)

    def each(packages):
        return [arg for p in sorted(packages) for arg in ("-p", p)]

    commands = [["cargo", "xtask", "layers"], ["cargo", "xtask", "lists"]]
    if libs | apps:
        commands.append(["cargo", "fmt", "--check", *each(libs | apps)])
    if libs:
        commands.append(["cargo", "clippy", *each(libs), "--all-targets", "--", "-D", "warnings"])
        commands.append(["cargo", "test", *each(libs)])
    if apps:
        commands.append(["cargo", "clippy", *each(apps), "--", "-D", "warnings"])
    return commands + app_tests


def run_checks(worktree, upstream):
    """None when every check passes, otherwise the failing command and its log tail."""
    command = ["cargo", "metadata", "--no-deps", "--format-version", "1"]
    result = run(command, worktree)
    if result.returncode != 0:
        return failed(command, result)
    for command in checks(worktree, upstream, json.loads(result.stdout)):
        result = run(command, worktree)
        if result.returncode != 0:
            return failed(command, result)
    return None


def push(branch, old, new):
    git("push", f"--force-with-lease=refs/heads/{branch}:{old}", "origin", f"{new}:refs/heads/{branch}")


def conflicts_entry(branch, files):
    return [f"- `{branch}` conflicts in:", *(f"  - `{f}`" for f in files)]


def main():
    worktree = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "fork-rebase"
    if git("remote", "get-url", "upstream", check=False).returncode != 0:
        git("remote", "add", "upstream", UPSTREAM)
    git("fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*")
    git("fetch", "upstream", "+refs/heads/main:refs/remotes/upstream/main")
    upstream = out("rev-parse", "refs/remotes/upstream/main")
    old = {}
    for line in out("for-each-ref", "--format=%(refname:lstrip=3) %(objectname)", "refs/remotes/origin/").splitlines():
        name, sha = line.split()
        if name != "HEAD":
            old[name] = sha
    main_old = old.pop("main")

    git("worktree", "add", "--force", "--detach", str(worktree), upstream)
    problems = []

    main_new, conflicts = rebase(worktree, upstream, out("merge-base", main_old, upstream), main_old)
    if conflicts:
        problems += conflicts_entry("main", conflicts)
    elif main_new != main_old:
        push("main", main_old, main_new)
    print(f"main: {main_old[:9]} -> {(main_new or main_old)[:9]}", flush=True)

    parent = {}
    for branch, tip in old.items():
        candidates = [
            (int(out("rev-list", "--count", f"{other_tip}..{tip}")), other)
            for other, other_tip in old.items()
            if other_tip != tip and is_ancestor(other_tip, tip)
        ]
        parent[branch] = min(candidates)[1] if candidates else None

    new = {}
    pending = sorted(old)
    while pending:
        branch = next(b for b in pending if parent[b] is None or parent[b] in new)
        pending.remove(branch)
        tip, up = old[branch], parent[branch]
        if up is not None and new[up] is None:
            new[branch] = None
            problems.append(f"- `{branch}` waits on `{up}`.")
            continue
        if up is None:
            from_main, from_upstream = out("merge-base", tip, main_old), out("merge-base", tip, upstream)
            base = from_upstream if is_ancestor(from_main, from_upstream) else from_main
            onto = upstream
        else:
            base, onto = old[up], new[up]
        rebased, conflicts = rebase(worktree, onto, base, tip)
        print(f"{branch}: {tip[:9]} -> {rebased[:9] if rebased else 'conflicts'}" + (f" on {up}" if up else ""), flush=True)
        if conflicts:
            new[branch] = None
            problems += conflicts_entry(branch, conflicts)
            continue
        if rebased != tip:
            failure = run_checks(worktree, upstream)
            if failure:
                new[branch] = None
                command, log = failure
                problems += [f"- `{branch}` fails `{command}`:", "", "  ```", *(f"  {l}" for l in log.splitlines()), "  ```"]
                continue
            push(branch, tip, rebased)
        new[branch] = rebased

    git("worktree", "remove", "--force", str(worktree))
    if not problems:
        return 0
    header = f"Rebasing onto upstream main ({upstream}) needs a person. These branches keep their old tips:"
    Path("report.md").write_text("\n".join([header, "", *problems]) + "\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
