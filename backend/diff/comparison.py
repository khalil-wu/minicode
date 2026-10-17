"""Source L4/I4 base resolution and branch-commit metadata on the UI host."""
from __future__ import annotations

import re
import errno

from backend.diff.git_integration import GitCommandError, _read_git, _run_git_result, _run_ui_git


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class ComparisonUnavailable(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


async def _result(root, args, service, *, timeout=15, output_limit=20 * 1024 * 1024):
    result = await (_run_ui_git(service, args, timeout=timeout, output_limit=output_limit) if service is not None
        else _run_git_result(root, *args, timeout=timeout))
    if len(result.stdout) + len(result.stderr) > output_limit:
        raise OSError(errno.EFBIG, f"Git comparison metadata exceeds Source's {output_limit}-byte output limit")
    return result


def _check(result, args, allowed=(0,)):
    if result.returncode not in allowed:
        raise GitCommandError(tuple(args), result.returncode, result.stderr.decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8", errors="replace").strip()


async def _ref(root, name, service):
    return (await _read_git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{name}^{{commit}}",
        service=service, allowed_exit_codes=(0, 1))).strip() or None


async def ordered_remotes(root, service):
    remotes = (await _read_git(root, "remote", service=service)).splitlines()
    return ["origin", *(remote for remote in remotes if remote != "origin")] if "origin" in remotes else remotes


async def default_base_branch(root, service):
    # RTe -> Ewe/p0/Dwe -> zTe/BTe: known remote HEAD, remote's actual
    # advertised HEAD, then only existing main/master remote-tracking refs.
    for remote in await ordered_remotes(root, service):
        symbolic = (await _read_git(root, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD",
            service=service, allowed_exit_codes=(0, 1))).strip()
        prefix = f"refs/remotes/{remote}/"
        if symbolic.startswith(prefix):
            branch = symbolic[len(prefix):]
            return branch if "/" in branch else f"{remote}/{branch}"
        try:
            shown = await _result(root, ["remote", "show", remote], service, timeout=10)
        except TimeoutError:
            # Source Dwe receives success:false for this exact timed-out
            # lookup, then zTe still checks real remote-tracking refs.
            shown = None
        if shown is not None and shown.returncode == 0:
            match = re.search(r"HEAD branch:\s*(.+)", shown.stdout.decode("utf-8", errors="replace"))
            if match and match[1].strip() != "(unknown)":
                branch = match[1].strip()
                return branch if "/" in branch else f"{remote}/{branch}"
        for branch in ("main", "master"):
            args = ["show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"]
            found = await _result(root, args, service)
            _check(found, args, (0, 1))
            if found.returncode == 0:
                return f"{remote}/{branch}"
    return None


async def merge_base(root, base, service):
    # Source dEe first trusts merge-base's real result. Only an unresolved ref
    # (128) enters its explicit remote/upstream candidate lookup.
    args = ["merge-base", "HEAD", base]
    result = await _result(root, args, service)
    value = _check(result, args, (0, 1, 128))
    if result.returncode != 128:
        return value or None
    if await _ref(root, "HEAD", service) is None:
        return None
    if await _ref(root, base, service) is not None:
        return _check(await _result(root, args, service), args, (0, 1)) or None
    upstream = (await _read_git(root, "for-each-ref", "--format=%(upstream:short)", f"refs/heads/{base}",
        service=service)).strip()
    remotes = await ordered_remotes(root, service)
    parts = base.split("/", 1)
    has_remote = len(parts) == 2 and parts[0] in remotes
    candidates = []
    for remote in remotes:
        if has_remote and remote == parts[0]:
            candidates.append(f"refs/remotes/{remote}/{parts[1]}")
        else:
            candidates.extend((f"{remote}/{base}", f"refs/remotes/{remote}/{base}"))
    if upstream:
        candidates.append(upstream)
    for candidate in dict.fromkeys(candidates):
        if await _ref(root, candidate, service) is not None:
            args = ["merge-base", "HEAD", candidate]
            return _check(await _result(root, args, service), args, (0, 1)) or None
    return None


async def branch_comparison(root, service, *, base_branch=None, merge_base_sha=None):
    base = base_branch or await default_base_branch(root, service)
    if not base:
        raise ComparisonUnavailable("default_branch_unavailable")
    if merge_base_sha:
        baseline = await _ref(root, merge_base_sha, service)
    else:
        baseline = await merge_base(root, base, service)
    if not baseline:
        raise ComparisonUnavailable("merge_base_unavailable")
    return {"base_branch": base, "merge_base_sha": baseline}


async def commit_comparison(root, service, commit_sha):
    commit = await _ref(root, commit_sha, service)
    if commit is None:
        raise ValueError("The selected commit is unavailable")
    parent = (await _read_git(root, "rev-parse", "--verify", "--quiet", f"{commit}^",
        service=service, allowed_exit_codes=(0, 1, 128))).strip() or EMPTY_TREE
    return {"commit_sha": commit, "parent_sha": parent}


async def review_options(root, service, *, base_branch=None):
    current = (await _read_git(root, "symbolic-ref", "--quiet", "--short", "HEAD",
        service=service, allowed_exit_codes=(0, 1))).strip() or None
    branches = (await _read_git(root, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)",
        "refs/heads", "refs/remotes", service=service)).splitlines()
    default = await default_base_branch(root, service)
    data = {"current_branch": current, "default_base_branch": default,
        "branches": [branch for branch in branches if not branch.endswith("/HEAD")],
        "merge_base_sha": None, "commits": [], "comparison_status": "success", "reason": None}
    if base_branch is None and default is None:
        return {**data, "comparison_status": "expected", "reason": "default_branch_unavailable"}
    try:
        comparison = await branch_comparison(root, service, base_branch=base_branch or default)
    except ComparisonUnavailable as unavailable:
        return {**data, "comparison_status": "expected", "reason": unavailable.reason}
    data.update(comparison)
    # Source f3/POe: at most 100 commits reachable from HEAD after merge-base,
    # preserving the exact committed-at, subject and full body fields.
    args = ["log", "--max-count=100", "--format=%H%x00%cI%x00%s%x00%B%x1e", f"{comparison['merge_base_sha']}..HEAD"]
    result = await _result(root, args, service, output_limit=2 * 1024 * 1024)
    output = _check(result, args)
    for record in output.split("\x1e"):
        if record.strip():
            sha, committed_at, subject, message = record.strip().split("\0", 3)
            data["commits"].append({"sha": sha, "committedAt": committed_at, "subject": subject, "message": message})
    return data
