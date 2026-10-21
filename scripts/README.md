# Scripts

- [tampermonkey/bdfr-export.user.js](tampermonkey/bdfr-export.user.js): the userscript that sends posts from old.reddit.com to `bdfr serve` and exports them for `--post-json`; see the [README](../README.md#downloading-from-the-browser). Its Playwright tests are run with `npm install && npx playwright install chromium && npm test` in `tampermonkey/`.
- [arctic_shift_guard.py](arctic_shift_guard.py) and [check_files.py](check_files.py): Claude Code hooks for working on this repository (see `.claude/settings.json`). The first asks before a command would contact the live Arctic Shift API; the second checks a file after it is edited (YAML parses, line endings, offline tests for Python files).

To read the logs, see [Reading the logs](../README.md#reading-the-logs) in the README.
