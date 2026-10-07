"""A minimal, explicit git wrapper for the harness.

One place shells out to git, so the flags that carry security weight are
chosen once and reviewed once — notably `--no-renames` (see `candidate.py`)
and `-z` (filename-safe parsing).

No vendor SDK; `git` is invoked as a subprocess. This module is Zone B.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_S = 60


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitRepo:
    root: Path
    timeout_s: int = DEFAULT_TIMEOUT_S

    def run(self, *args: str) -> str:
        return self._run(*args).stdout.decode("utf-8", errors="replace")

    def run_bytes(self, *args: str) -> bytes:
        return self._run(*args).stdout

    def _run(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:  # pragma: no cover - git is a hard dep
            raise GitError("git executable not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {' '.join(args)} timed out after {self.timeout_s}s") from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"git {' '.join(args)} failed ({completed.returncode}): {stderr}")
        return completed

    def rev_parse(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def merge_base(self, base: str, head: str) -> str:
        return self.run("merge-base", base, head).strip()

    def show(self, ref: str, path: str) -> str:
        """Contents of ``path`` as of ``ref``.

        Explicit base-ref reads use this primitive. The provenance of the
        executing harness is established by its launcher, not by this method;
        see ``trust.py`` and ADR 0047.
        """
        return self.run("show", f"{ref}:{path}")

    def ref_exists(self, ref: str) -> bool:
        """Does `ref` resolve to a commit in this repository?

        Exists because `path_exists_at` below CANNOT answer it. `git cat-file
        -e <ref>:<path>` fails identically for a missing file and a missing
        ref, so a caller that only has `path_exists_at` reports "no agent
        source at <persona> in main" for a repository that has no `main` at
        all — blaming a file that is present, on the exit code that means
        nothing was wrong (reproduced on a real repo whose default branch is
        `azure-agent/uptime-monitoring`; ADR 0187's F-M8-1, fixed in 0189).

        Two questions, two methods, and the caller has to say which it is
        asking.
        """
        try:
            self.rev_parse(ref)
        except GitError:
            return False
        return True

    def is_repo(self) -> bool:
        """Is `root` inside a git repository at all?

        Asked so that "this ref does not exist" and "there are no refs here
        because this is a plain directory" stay two different answers. The
        second is not a base-ref mistake: nothing named a ref wrongly, and the
        diagnostics (`aef loop doctor`) must still run and report what IS
        missing rather than crash on the first call out to git.
        """
        try:
            self.run("rev-parse", "--git-dir")
        except GitError:
            return False
        return True

    def branch_names(self) -> tuple[str, ...]:
        """Every local branch, in git's own order.

        For error messages only: a refusal that names a ref the repository
        does not have is only actionable next to the refs it does.
        """
        try:
            out = self.run("branch", "--format=%(refname:short)")
        except GitError:  # pragma: no cover - a repo with no refs at all
            return ()
        return tuple(line.strip() for line in out.splitlines() if line.strip())

    def symbolic_ref(self, name: str) -> str | None:
        """`git symbolic-ref --short <name>`, or `None` when it is not one."""
        try:
            return self.run("symbolic-ref", "--quiet", "--short", name).strip() or None
        except GitError:
            return None

    def path_exists_at(self, ref: str, path: str) -> bool:
        """Does `path` exist at `ref`? **Assumes `ref` exists** — ask
        `ref_exists` first if that is not already established, or a False here
        means one of two very different things (see above)."""
        try:
            self.run("cat-file", "-e", f"{ref}:{path}")
        except GitError:
            return False
        return True

    def list_tree(self, ref: str, prefix: str) -> tuple[str, ...]:
        """Every file path under `prefix` as of `ref`.

        Exists because G5's drift metric compares two dicts of path -> bytes
        and is only meaningful when both describe the **same tree**. Feeding
        it a whole-tree baseline and a changed-files-only candidate made every
        untouched file read as deleted (ADR 0074).
        """
        try:
            out = self.run("ls-tree", "-r", "--name-only", "-z", ref, "--", prefix)
        except GitError:
            return ()
        return tuple(p for p in out.split("\0") if p)

    def raw_diff(self, base: str, head: str) -> bytes:
        """`git diff --raw -z --no-renames base...head`.

        `...` is the symmetric form: changes on `head` since its merge-base
        with `base`, which is exactly "what this candidate proposes" and not
        "everything that has happened on base meanwhile".

        `--no-renames` is a **security** flag, not a cosmetic one. With
        rename detection on, moving `aef/kernel/executor.py` to
        `agents/executor.py` reports only the destination path, so a zone
        check would see one Zone A file and wave through the deletion of a
        core one. `--no-renames` reports the delete and the add separately.

        `-z` makes parsing independent of filename contents; without it git
        quotes paths containing spaces, tabs, or newlines.
        """
        return self.run_bytes("diff", "--raw", "-z", "--no-renames", f"{base}...{head}")

    def numstat_diff(self, base: str, head: str) -> bytes:
        """`added\\tremoved\\0path\\0` per entry — the diff-size input the
        G0 budget (Q-A1) applies a threshold to. Same flags, same reasons."""
        return self.run_bytes("diff", "--numstat", "-z", "--no-renames", f"{base}...{head}")
