#!/usr/bin/env bash
set -euo pipefail
outdir=".scan-results"
mkdir -p "$outdir"

echo "1) Git info"
git rev-parse --abbrev-ref HEAD > "$outdir/branch.txt" 2>/dev/null || echo "no-git" > "$outdir/branch.txt"
git log -n 5 --pretty=format:'%h %an %s' > "$outdir/recent-commits.txt"

echo "2) Quick grep for common patterns"
# grep common token patterns (adjust or extend as needed)
git grep -I --line-number -E "OPENAI_API_KEY|OPENAI|SECRET|PASSWORD|TOKEN|AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID|-----BEGIN|PRIVATE KEY|ghp_[A-Za-z0-9_\\-]{36,}|sk_live_[0-9a-zA-Z]{20,}|xox[abprs]-[0-9A-Za-z-]{10,}" \
  > "$outdir/greps.txt" || true

echo "3) detect-secrets (scan current files)"
if ! command -v detect-secrets >/dev/null 2>&1; then
  echo "installing detect-secrets (pip)"
  python -m pip install --user detect-secrets
  PATH="$PATH:$HOME/.local/bin"
fi
# JSON output for CI-friendly processing
detect-secrets scan --all-files --no-verify --json > "$outdir/detect-secrets.json" || true

echo "4) truffleHog (git history high-entropy + regex)"
if ! command -v trufflehog >/dev/null 2>&1; then
  echo "installing truffleHog (pip)"
  python -m pip install --user truffleHog
  PATH="$PATH:$HOME/.local/bin"
fi
# scan local history (filesystem mode)
trufflehog filesystem . --entropy=True --regex=True --json > "$outdir/trufflehog-fs.json" || true

echo "5) git-secrets quick scan"
if ! command -v git-secrets >/dev/null 2>&1; then
  echo "git-secrets not found; attempting install instructions"
  # Attempt to install git-secrets from source (may require sudo for make install)
  tmpdir="$(mktemp -d)"
  git clone https://github.com/awslabs/git-secrets.git "$tmpdir/git-secrets" >/dev/null 2>&1 || true
  if [ -d "$tmpdir/git-secrets" ]; then
    (cd "$tmpdir/git-secrets" && sudo make install) || true
    rm -rf "$tmpdir"
  fi
fi
# Register AWS patterns and run a scan (no changes to repo)
git secrets --register-aws || true
git secrets --scan > "$outdir/git-secrets.txt" || true

echo "Results written to $outdir"
ls -lah "$outdir" || true
