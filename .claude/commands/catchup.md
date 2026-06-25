Rebuild context for a new session. Do the following in order:

1. Read `docs/progress.md` to understand what's been completed and what's pending
2. Read the relevant sections of `docs/tech-plan.md` based on what's next in the progress log
3. Read all files modified on the current branch compared to main (`git diff --name-only main`)
4. Run **`make check-parity`** when operator surfaces changed (`mcp_rmm_server.py`, `rmm_tools.py`, `rmm_cli.py`, `server_rmm.py`, `web/app.js`); run **`make check`** before committing larger changes
5. Check for any failing tests
6. Summarize:
   - What has been implemented so far
   - What work remains
   - Any failing tests or known issues
   - Recommended next step
