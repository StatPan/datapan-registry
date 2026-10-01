#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <repository-checkout> <health-state-branch> <worktree-path>" >&2
  exit 2
fi

repository_checkout="$1"
state_branch="$2"
state_worktree="$3"
state_ref="refs/remotes/origin/${state_branch}"
remote_result="$(mktemp)"
trap 'rm -f "${remote_result}"' EXIT

git -C "${repository_checkout}" worktree remove --force "${state_worktree}" >/dev/null 2>&1 || true
if git -C "${repository_checkout}" ls-remote --exit-code origin "refs/heads/${state_branch}" > "${remote_result}"; then
  git -C "${repository_checkout}" fetch --no-tags origin "refs/heads/${state_branch}:${state_ref}"
  GIT_LFS_SKIP_SMUDGE=1 git -C "${repository_checkout}" worktree add --detach "${state_worktree}" "${state_ref}"
  echo "branch_exists=true"
else
  rc=$?
  if [ "${rc}" -ne 2 ]; then
    echo "Unable to inspect health state branch ${state_branch} (git ls-remote exit ${rc})" >&2
    exit "${rc}"
  fi
  GIT_LFS_SKIP_SMUDGE=1 git -C "${repository_checkout}" worktree add --detach "${state_worktree}" HEAD
  git -C "${state_worktree}" checkout --orphan "${state_branch}"
  git -C "${state_worktree}" rm -rf . >/dev/null 2>&1 || true
  git -C "${state_worktree}" clean -fdx >/dev/null
  echo "branch_exists=false"
fi
