# Preparing a public repository

Publish only this source tree. Do not copy an older `.git` directory, local
settings, private artwork, device captures, vendor binaries or research folders.

```bash
python scripts/check-release.py
python -m pip install build
python -m build
python scripts/check-release.py dist/*.whl dist/*.tar.gz
```

Inspect the source and both archives, run the test suite, and test installation
from the wheel in a separate virtual environment. The checker detects common
private-data patterns and forbidden file types; it is not a legal clearance
or a guarantee that arbitrary artwork can be redistributed.

To start a fresh history, run `./scripts/init-public-repo.sh` in this directory.
It refuses an existing `.git`, checks the tree and initializes an empty `main`
branch. It does not stage, commit or push anything. Then review locally:

```bash
git add .
git diff --cached --stat
git diff --cached
git commit -m "Initial Verona release"
```

Create an empty GitHub repository and add its URL as `origin` when ready.
Review the destination before pushing. Do not fork or push old branches/tags
into the new repository. Keep author names/emails appropriate for public commits.
Cleaning files does not remove them from any separately retained old history.
