# Engineering principles

We are four teammates with mixed experience, building over six days. Keep changes
focused on your task so reviewers and other coding agents can follow them.

## Branches and commits

- `main` must always be deployable. Never push directly to it; use a pull request.
- Branch from current `main`, using `feature/<short-description>`,
  `fix/<short-description>`, or `chore/<short-description>`.
- Use Conventional Commits: `<type>: <short imperative summary>`.
  Allowed types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.
  Example: `chore: add startup check`.
- Keep commits small and atomic: one coherent change per commit.
- Do not implement a teammate's TODO files. Read [GUIDE.md](GUIDE.md) first.

## Pull requests

Use the feature/fix/task name as the PR title. Describe:

- What was built or changed.
- Why the change is needed.
- How it was tested, including any checks not run.
- Any environment, dependency, database, or setup changes (say “none” if none).

Run `ruff check .`, verify the app starts, and check `/health`. Add task-specific
verification when you implement a feature. Never include API keys or local `.env`
files in commits or PR descriptions. Flag proposed schema changes before editing.

Every PR requires at least **one human approval** plus passing `lint` and `smoke`
checks. CodeRabbit and coding agents cannot replace the human reviewer. Address
review feedback before merging; keep the change within its assigned task.

## Administrator checklist: enforce the policy on GitHub

These are required repository settings, not protections activated by local files.
Their activation has not been verified in this workspace.

1. Push the workflow on the initial repository setup and let it run so GitHub
   discovers the `lint` and `smoke` checks.
2. In repository Settings → Branches, create a branch protection rule for `main`.
3. Enable **Require a pull request before merging**, require **1 approving
   review**, and dismiss stale approvals when new commits are pushed.
4. Enable **Require status checks to pass before merging**, selecting `lint`
   and `smoke` from GitHub Actions. Require the branch to be up to date.
5. Enable **Do not allow bypassing the above settings** (including admins),
   leave bypass actors empty, and keep force pushes and deletions disabled.
6. Verify enforcement on a test PR: merging is blocked without both checks and
   a human approval. Confirm `main` cannot receive direct pushes under the rule.

See [GitHub branch protection documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
Protection availability depends on repository visibility and GitHub plan; the
owner must use a supported configuration before claiming the policy is enforced.

Enable CodeRabbit and Render separately using the README setup instructions.
