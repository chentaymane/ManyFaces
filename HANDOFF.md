# HANDOFF: ManyFaces reliability and dashboard improvements

**Updated:** 2026-11-10 | **By:** Codex | **Status:** Done

## Goal
Fix verified project issues, add useful improvements, optimize behavior, and commit all changes as requested by the user.

## Where we stopped
Completed the reliability and dashboard improvement pass for version 0.2.0. All 28 tests passed, including the optional real Chromium dashboard test. Python compilation, JavaScript syntax, and diff whitespace checks passed. Changes are ready for the user-requested commit.

## Next steps
No remaining implementation steps for this pass. Verify the latest commit with `git log -1` and `git status`. Future release validation should exercise real Android boots, full Camoufox launches, and portable builds; those were outside local automated verification.

## Done so far
- SQLite connections close on all paths; partial updates read/write in one transaction.
- Session registration starts threads under the manager lock; registry snapshots are locked.
- API launch serialization, rotation only after accepted launch, live profile edit guards.
- Validation of names, OS, engine, null patches, counts, ports, import version and cookie shape.
- Cookie writes are atomic; malformed stored jars and invalid cookie fields are normalized.
- Proxy IPv6, encoded credentials, colon-containing passwords, HTTPS transport; SOCKS dependency declared.
- Range downloader rejects ignored/mismatched ranges, truncated and oversized responses, and invalid offsets.
- Dashboard sorting, stopped/failed filters, persisted view choices, indeterminate selection, improved validation errors, reduced polling/rendering.
- FastAPI lifespan cleanup for both managers; desktop server receives graceful exit.
- Regression suite and real Chromium dashboard test added under tests/; 28 tests pass.
- Responsive header/table layout, retry recovery, and proxy editor credential round trips verified in Chromium.
- README test instructions and Windows/Linux Python 3.12/3.14 CI workflow added.

## Decisions and constraints
- Existing working tree was clean at start.
- No browser engine downloads, external proxy requests, dependency upgrades, or real Android boots were performed.
- Tests use temporary app data; screenshots are ignored under `.testdata/`.
- User explicitly requested committing all changes; no push requested.

## Gotchas
- `python` resolves to an inaccessible WindowsApps alias. Working interpreter: `C:\Users\chent\AppData\Local\Python\pythoncore-3.14-64\python.exe`.
- Installed Python 3.14 includes FastAPI, Camoufox, Playwright, and Chromium. Tests use unittest (pytest unavailable).
- Current Starlette emits an upstream TestClient/httpx deprecation warning; tests still pass.
- Git reports an inaccessible global ignore file; repository commands otherwise work.

## How to verify
```powershell
$env:MANYFACES_BROWSER_TESTS='1'
& 'C:\Users\chent\AppData\Local\Python\pythoncore-3.14-64\python.exe' -m unittest discover -s tests -v
node --check web/app.js
git diff --check
```

## Open questions
None.

<!-- handoff:auto:start -->
## Auto handoff

_Last update: 2026-11-10 16:57 +0000, from a **Codex** session (gpt-6.1-sol). Written by [handoff](https://github.com/chentaymane/handOff) from the session's log and git: this section is rewritten automatically, anything outside it is kept._

### Goal
_No request from the user found in the log._

### Where we stopped
_The agent had not replied yet._

### Next steps
1. Read "Where we stopped" and finish anything it left open.
2. Run `git status` to find uncommitted or half-finished edits before starting new work.

### Session
- **Tool:** Codex (gpt-6.1-sol), 26 tool calls
- **Active:** 2026-11-10 16:43 to 2026-11-10 16:57
- **Context:** ~95K of 475K tokens (20%)
- **Usage limit:** 6% used of the 5-hour window, resets 2026-11-10 21:43
- **Full log:** `C:\Users\chent\.codex\sessions\2026\11\10\rollout-2026-11-10T16-42-58-01a1c657-626c-70b1-bfcb-39e44c7e879e.jsonl`

### Repo
- **Branch:** `main` @ `e67fa89` - Fix Camoufox 0.5.x launches (#1), engine download, and UI bugs
- **Upstream:** `origin/main` - ahead 0, behind 0
- **Uncommitted:** 13 changed, 1 untracked

Recent commits:

```
e67fa89 2026-10-08 Fix Camoufox 0.5.x launches (#1), engine download, and UI bugs
7715fc5 2026-07-24 Non-blocking launch, live status, and Android perf tuning
8207a81 2026-07-20 ui/ux
4274959 2026-07-17 26
3246d58 2026-07-17 62
```

Working tree:

```
 M antidetect/android.py
 M antidetect/api.py
 M antidetect/browser.py
 M antidetect/config.py
 M antidetect/cookies.py
 M antidetect/db.py
 M antidetect/models.py
 M antidetect/proxy.py
 M desktop.py
 M requirements.txt
 M web/app.js
 M web/index.html
 M web/style.css
?? tests/
```
<!-- handoff:auto:end -->
