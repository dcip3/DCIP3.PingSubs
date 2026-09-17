# Contributing

Use Python 3.13 (see `.python-version`) and install `requirements.txt` in a virtual environment.
Install [Gitleaks](https://github.com/gitleaks/gitleaks#installing) and enable hooks:

```bash
git config --local core.hooksPath .githooks
```

If Gitleaks is not on PATH, set its executable with
`git config --local gitleaks.path /absolute/path/to/gitleaks`.
The pre-commit hook scans staged changes for secrets. The commit-msg hook checks
the message format. CI also scans the complete Git history.

## Commit messages

Use English [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
with ASCII punctuation, a lowercase description, and a subject of at most 72
characters. Separate an optional body from the subject with a blank line.

```text
feat: add subscription reminders
fix: preserve monthly billing dates
docs: explain local setup
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`,
`chore`, `revert`. An optional scope and breaking-change `!` are supported.
Credit actual human contributions accurately; generated tool signatures are not
required.

## Local checks

```bash
python -m unittest discover -s tests -v
python -m compileall -q app main.py scripts tests
python scripts/check_commit_messages.py --rev-range HEAD
gitleaks git --log-opts="--all --full-history" --redact
python -m pip install pip-audit
python -m pip_audit -r requirements.txt
```

Keep `.env`, bot tokens, databases, backups, and real member/payment data out of
commits and issue reports. Tests use temporary databases and never send Telegram
messages. No license has been selected yet; public visibility alone does not
grant permission to reuse the project.
