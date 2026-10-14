# Source repository workflow

Run Git commands from the `OzarkPay` source directory. Its Git repository is
independent of the enclosing local audit workspace. The enclosing `.git`, local
Docker volumes, credentials and audit evidence are not part of this repository.

The current upstream is `https://github.com/Veyra-Technologies/ozark-pay`.
Visibility is public by the owner's explicit instruction. Treat every committed
file and every commit as publicly readable; Git ignore is not a secret scanner.

Start changes from an up-to-date `main` and use a topic branch:

```bash
git switch main
git pull --ff-only origin main
git switch -c codex/short-description
```

Before committing, inspect `git status`, `git diff`, and
`git diff --cached --name-only`. Scan the staged changes with Gitleaks or an
equivalent scanner. Findings must be reviewed without printing secret values.
The owner requested exclusion of tests and demo datasets from the current source
publication. Maintain acceptance tests separately in an isolated QA environment;
do not reintroduce synthetic accounts, test credentials or seed data into this
repository or a deployment image.

```bash
git add <reviewed-source-files>
git diff --cached --check
gitleaks git --staged --redact --ignore-gitleaks-allow
git commit -m "describe the actual change"
git push -u origin codex/short-description
```

Open a pull request for review. Do not force push `main`, mirror the enclosing
workspace, or commit `.env` files, local configuration, uploads, payment data,
database dumps, ACCESS files, 2FA QR codes, virtual environments or QA reports.
Adding `.gitignore` does not remove files from existing commits.

When a real secret has been exposed, rotate it through the responsible service
and coordinate history remediation separately. Rewriting published history or
changing repository visibility requires an explicit instruction.

Recovery requires three separate items: this source repository, privately stored
configuration and encryption keys, and a verified database/uploads backup. Git
alone does not restore users, balances, holds or encrypted bank requisites.
