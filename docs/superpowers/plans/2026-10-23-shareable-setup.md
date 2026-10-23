# Shareable Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One repository link lets a local agent install and connect a new user's own university account.
**Architecture:** stdlib bootstrap + package onboarding/config service + loopback login wizard. Reuse scoped Moodle auth and IT report validation, keep secrets out of CLI/logs/Git.
**Tech Stack:** Python3.11+,HTTPX,FastMCP,tomlkit,pytest; existing GitHub Actions.
**Spec:** docs/superpowers/specs/2026-10-23-shareable-setup-design.md

## Global Constraints
- Fixed university origins; verified TLS; no credential-bearing redirects.
- Passwords only in person-operated local form, never persisted/logged; auth file private.
- Preserve unrelated config and existing installs; separate account caches.
- Repeat setup must be safe; failures do not claim a successful connection.
- No automatic coursework submissions or recurring schedule for a new installation.

## Review Focus
- Paths with spaces, Windows paths and nonstandard CODEX_HOME.
- Reinstall, foreign MCP name conflict, config symlink and malformed TOML.
- Host/Origin/CSRF, oversized/malformed requests, form errors and secret leakage.
- Expired or MFA-only university login; IT session identity matching and redirects.
- Package installed from clean GitHub checkout, no current-account cache/secrets required.

### Task 1: Local paths, config and bootstrap
**Files:** install.py,university_mcp/onboarding.py,tests/test_onboarding.py,pyproject.toml.
**Interfaces:** default_data_dir(), configure_codex(python,data_dir,codex_home,replace=False), doctor(data_dir,codex_home,live=False). Bootstrap installs package into isolated venv then invokes onboarding.
- [ ] Write failing behavior tests for generated absolute paths, unrelated config preservation, rerun/conflict, malformed/symlink failures and OS-specific paths.
- [ ] Implement atomic configuration and private directory creation; bootstrap supports no-browser and configurable directories for clean tests.
- [ ] Verify targeted and full tests; commit.

### Task 2: Login wizard and account isolation
**Files:** university_mcp/setup_web.py,university_mcp/setup_page.html,university_mcp/auth.py,university_mcp/it_auth.py,server.py,tests/test_setup_web.py,tests/test_it_auth.py.
**Interfaces:** create_setup_server(data_dir,...) -> loopback HTTPServer with random capability path; Moodle existing password_login and session exchange; IT normal form login -> validated own auth; save account-specific metadata for loader.
- [ ] Add failing real-HTTP tests for form login/token save, no password persistence, Host/Origin/CSRF guards, retry/error handling and account isolation.
- [ ] Add synthetic university transport tests for optional normal IT login, CSRF and exact bridge/own ID extraction.
- [ ] Build a readable local login page and CLI wizard, verify generated MCP config/discovery and UI without real passwords.
- [ ] Run full suite; commit.

### Task 3: Link-based installation and release
**Files:** README.md,AGENTS.md,CI,version.
- [ ] Explain agent clone/install/login/doctor workflow, prerequisites and session refresh; no user-specific paths.
- [ ] Exercise bootstrap from a fresh checkout with isolated data and Codex config; inspect browser page; verify real MCP initialization.
- [ ] Fresh independent review, fix important findings with RED→GREEN, run final tests.
- [ ] Publish to the existing public repo; verify exact commit and all OS CI jobs; return GitHub link.
