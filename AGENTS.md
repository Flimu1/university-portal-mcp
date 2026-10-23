# University Portal MCP: install from this link

When the user asks to install this repository for their own account, complete the local setup. A GitHub URL identifies the source, not a hosted MCP endpoint.

1. Read README.md. Use an existing clean checkout, or clone this public repo into a permanent user-owned folder. Never copy another person's `.local`, auth, course cache or submissions. Do not delete or reset a dirty checkout.
2. Find Python 3.11+ (`python3`, `python`, or `py -3` on Windows). If needed, use an already available `uv run --python 3.13 --no-project install.py`, an official Python installation, or the Codex bundled Python runtime. Do not install unverified binaries.
3. From the repository, run `python install.py` with the chosen Python. It creates `.venv`, installs the package, registers `university_portal` in the actual Codex configuration, and opens a private loopback login page. `--no-browser` prints a link for opening manually and keeps the setup process running. Keep that process alive while the person logs in. For terminal tools, use a persistent session; do not kill a running setup just because it yielded.
4. The person enters their own credentials in the browser. Never ask for passwords in chat, inspect saved passwords, read auth file contents, or enter a person's password on their behalf. Optional IT login is separate. Keep the token/session files local. No credentials go to GitHub or a shared server.
5. Wait for the person to finish setup. Run `.venv/bin/python -m university_mcp.onboarding --doctor --live` (Windows: `.venv/Scripts/python.exe`). Claim connected only when Moodle reports `connected`. A successful installation/configuration or a saved token alone does not prove authentication. If login is pending, give the local link and leave the process running.
6. Ask the person to restart the MCP connection or open a new chat if the current tool list has not refreshed. Verify `portal_status` and `list_courses` through the new configured MCP when available. Do not start tests, submit coursework, send messages or create recurring automation as part of installing.

The installer preserves other Codex settings and honors `CODEX_HOME`. Use `--replace` only when the user intends to replace a conflicting university_portal entry. `--data-dir` and `--codex-home` isolate test installs. Do not point tests at real user settings.

For reconnecting, run `python -m university_mcp.onboarding` from the installed environment. Existing valid Moodle auth is verified, so only the optional expired IT session needs a new login. Browser/SSO-only accounts may need the advanced Moodle auth helper described in README; never invent a successful login or bypass a login challenge.

For development, run the synthetic test suite and preserve fixed-origin, own-account scope. First-time installation must remain account-independent; no hardcoded student identity or personal absolute paths. User instructions override these project defaults.
