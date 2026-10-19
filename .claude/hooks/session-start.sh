#!/bin/bash
# Prepares a Claude Code cloud session so lint, type checks, tests and the
# diagram/docs checks run without setup steps. Mirrors .github/workflows/ci.yml.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

# Every dependency group, as CI's lint and test jobs install them. The `ml`
# extra (torch, easyocr, transformers) is left out: only `slow` tests need it.
uv sync --frozen --all-groups

# CI runs keyring-touching tests with the fail backend so no OS keychain is used.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo 'export PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring' >> "$CLAUDE_ENV_FILE"
fi

# mermaid-cli at CI's pin, driving the preinstalled Chromium instead of
# downloading one. The container runs as root, so Chrome needs --no-sandbox.
MMDC_DIR="$HOME/.cache/sortify-mmdc"
CHROME="$(ls -d /opt/pw-browsers/chromium-*/chrome-linux/chrome 2>/dev/null | head -n1 || true)"
if [ -n "$CHROME" ]; then
  if [ ! -x "$MMDC_DIR/node_modules/.bin/mmdc" ]; then
    mkdir -p "$MMDC_DIR"
    PUPPETEER_SKIP_DOWNLOAD=1 npm install --prefix "$MMDC_DIR" --no-audit --no-fund \
      @mermaid-js/mermaid-cli@12.0.0
  fi
  printf '{"executablePath":"%s","args":["--no-sandbox"]}\n' "$CHROME" > "$MMDC_DIR/puppeteer.json"
  mkdir -p "$HOME/.local/bin"
  cat > "$HOME/.local/bin/mmdc" <<WRAPPER
#!/bin/bash
exec "$MMDC_DIR/node_modules/.bin/mmdc" -p "$MMDC_DIR/puppeteer.json" "\$@"
WRAPPER
  chmod +x "$HOME/.local/bin/mmdc"
else
  echo "session-start: no preinstalled Chromium found; skipping mermaid-cli" >&2
fi
