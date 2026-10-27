#!/usr/bin/env bash
# Portfolio Pre-Push Quality Gate
# Usage: ./scripts/pre_push_check.sh
# Install as Git hook: make install-hooks
set -euo pipefail
trap 'echo -e "${RED}✗ Unexpected script failure at line $LINENO${NC}"; exit 1' ERR

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}✓ PASS${NC}: $1"; }
fail() { echo -e "${RED}✗ FAIL${NC}: $1"; exit 1; }
warn() { echo -e "${YELLOW}⚠ WARN${NC}: $1"; }

# Ensure virtualenv binaries are accessible if present
if [ -d ".venv/bin" ]; then
    export PATH="$(pwd)/.venv/bin:$PATH"
fi

echo "═══════════════════════════════════════════"
echo "  Portfolio Pre-Push Quality Gate "
echo "═══════════════════════════════════════════"

# ── Auto-Detect Source Directory ──
SRC_DIR="${SRC_DIR:-}"
if [ -z "$SRC_DIR" ]; then
    if [ -d "src" ]; then
        SRC_DIR="src"
    elif [ -d "app" ]; then
        SRC_DIR="app"
    else
        SRC_DIR="."
    fi
fi

# ── 1. Ruff Lint ──
echo ""
echo "── Step 1/11: Ruff Lint ──"
ruff check . || fail "ruff check failed"
pass "ruff check"

# ── 2. Ruff Format ──
echo ""
echo "── Step 2/11: Ruff Format ──"
ruff format --check . || fail "ruff format --check failed"
pass "ruff format"

# ── 3. Type Checking (mypy strict) ──
echo ""
echo "── Step 3/11: Type Checking ──"
mypy --strict . || fail "mypy --strict failed"
pass "mypy --strict"

# ── 4. Tests with Branch Coverage ──
echo ""
echo "── Step 4/11: Tests + Branch Coverage ──"
pytest tests/ -v --tb=short --cov="$SRC_DIR" --cov-branch --cov-fail-under=80 || fail "pytest failed or branch coverage < 80%"
pass "pytest (branch coverage ≥ 80%)"

# ── 5. Dependency Vulnerability Scan ──
echo ""
echo "── Step 5/11: Dependency Audit ──"
if command -v pip-audit &> /dev/null; then
    pip-audit || fail "pip-audit found vulnerabilities"
    pass "pip-audit"
else
    fail "pip-audit not installed — required by security policy (install via: uv tool install pip-audit or uv sync --dev)"
fi

# ── 6. Secret Scanning ──
echo ""
echo "── Step 6/11: Secret Scanning ──"
if command -v gitleaks &> /dev/null; then
    gitleaks detect --source . --verbose --no-banner || fail "gitleaks detected secrets in repository history"
    pass "gitleaks"
else
    warn "gitleaks not installed — falling back to pattern scan"
    # Fallback: grep for common secret patterns in tracked files (excluding only pre-push script and lock files)
    SECRETS_FOUND=$(git grep -lIiE \
        '(sk-[a-zA-Z0-9]{20,}|sk-ant-[a-zA-Z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|github_pat_[a-zA-Z0-9_]{82}|(ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{36}|xox[bpas]-[a-zA-Z0-9-]+|AIza[0-9A-Za-z_\-]{35}|(sk|rk)_live_[0-9a-zA-Z]{24,}|sbp_[a-zA-Z0-9]{40}|-----BEGIN (RSA|EC|DSA|OPENSSH|PGP) PRIVATE KEY-----)' \
        -- ':!*.lock' ':!scripts/pre_push_check.sh' 2>/dev/null || true)
    if [ -n "$SECRETS_FOUND" ]; then
        echo "$SECRETS_FOUND"
        fail "Potential secrets found in tracked files (install gitleaks for full history scan)"
    fi
    pass "Pattern-based secret scan (install gitleaks for comprehensive scanning)"
fi

# ── 7. .env Exclusion Verification ──
echo ""
echo "── Step 7/11: .env Protection ──"
if git ls-files --error-unmatch .env 2>/dev/null; then
    fail ".env is tracked by git — add to .gitignore and remove: git rm --cached .env"
fi
if ! grep -q "^\.env$" .gitignore 2>/dev/null; then
    fail ".env is not in .gitignore"
fi
pass ".env excluded from tracking"

# ── 8. Required Files Check ──
echo ""
echo "── Step 8/11: Required Files ──"
REQUIRED_FILES=("README.md" "LICENSE" ".gitignore")
for f in "${REQUIRED_FILES[@]}"; do
    [ -f "$f" ] || fail "Required file missing: $f"
done
# Check README minimum length (proxy for completeness)
README_CHARS=$(wc -c < README.md)
if [ "$README_CHARS" -lt 500 ]; then
    warn "README.md is under 500 characters — likely incomplete"
fi
pass "All required files present"

# ── 9. Mocking Integrity (AST Verification) ──
echo ""
echo "── Step 9/11: Mocking Integrity ──"
if [ -d "tests/" ]; then
    python3 -c '
import ast, sys
from pathlib import Path

test_dir = Path("tests")
violations = []
if test_dir.is_dir():
    for py_file in test_dir.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                is_patch = False
                name = ""
                if isinstance(node.func, ast.Name) and node.func.id in ("patch",):
                    is_patch = True
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    val = node.func.value
                    attr = node.func.attr
                    if attr in ("patch", "object", "multiple"):
                        if isinstance(val, ast.Name) and val.id in ("patch", "mocker", "mock"):
                            is_patch = True
                            name = f"{val.id}.{attr}"
                        elif isinstance(val, ast.Attribute) and val.attr in ("mock", "patch"):
                            is_patch = True
                            name = f"...{val.attr}.{attr}"
                if is_patch:
                    has_autospec = any(
                        kw.arg == "autospec" and getattr(kw.value, "value", None) is True
                        for kw in node.keywords
                    )
                    if not has_autospec:
                        violations.append(f"{py_file}:{node.lineno}: {name}() missing autospec=True")

if violations:
    print("\n".join(violations))
    sys.exit(1)
' || fail "Found patch() calls without autospec=True (code-standards.md §8.7)"
    pass "All patch() calls enforce autospec=True (AST-verified)"
else
    warn "No tests/ directory found — skipping mocking integrity check"
fi

# ── 10. Outermost I/O Seam & Attribute Sniffing ──
echo ""
echo "── Step 10/11: Internal Mock Ban & Attribute Sniffing ──"
if [ -d "tests/" ]; then
    python3 -c '
import ast, sys
from pathlib import Path

src_dir = sys.argv[1] if len(sys.argv) > 1 else "app"
test_dir = Path("tests")
internal_targets = []
if test_dir.is_dir():
    for py_file in test_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        lines = text.splitlines()
        try:
            tree = ast.parse(text, filename=str(py_file))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target_str = None
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    target_str = node.args[0].value
                for kw in node.keywords:
                    if kw.arg == "target" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        target_str = kw.value.value
                if target_str and (target_str.startswith(f"{src_dir}.") or target_str.startswith("app.")):
                    line_idx = node.lineno - 1
                    line_content = lines[line_idx] if line_idx < len(lines) else ""
                    prev_content = lines[line_idx - 1] if line_idx > 0 else ""
                    if "# MOCK-SEAM-WAIVER:" not in line_content and "# MOCK-SEAM-WAIVER:" not in prev_content:
                        internal_targets.append(f"{py_file}:{node.lineno}: Mock targets internal \"{target_str}\" without # MOCK-SEAM-WAIVER:")

if internal_targets:
    print("\n".join(internal_targets))
    sys.exit(1)
' "$SRC_DIR" || fail "Found mock patches targeting internal modules without # MOCK-SEAM-WAIVER: (code-standards.md §8.6)"
    pass "Zero un-waived internal mocks (outermost I/O seam verified)"
else
    warn "No tests/ directory found — skipping internal mock ban check"
fi

if [ -d "$SRC_DIR" ] && [ "$SRC_DIR" != "." ]; then
    HASATTR_CALLS=$(grep -rn "hasattr(" "$SRC_DIR/" --include='*.py' | grep -v '# ATTR-SNIFF-WAIVER:' || true)
    if [ -n "$HASATTR_CALLS" ]; then
        echo "$HASATTR_CALLS"
        fail "Found hasattr() calls in $SRC_DIR/ without # ATTR-SNIFF-WAIVER: (code-standards.md §1)"
    fi
    pass "Attribute sniffing check passed (no un-waived hasattr calls in $SRC_DIR/)"
fi

# ── 11. Conventional Commits ──
echo ""
echo "── Step 11/11: Conventional Commits ──"
if git rev-parse --verify HEAD &>/dev/null; then
    RANGE="HEAD~10..HEAD"
    UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)
    if [ -n "$UPSTREAM" ]; then
        RANGE="$UPSTREAM..HEAD"
    elif [ "$(git rev-list --count HEAD)" -lt 10 ]; then
        RANGE="HEAD"
    fi
    COMMITS=$(git rev-list "$RANGE" 2>/dev/null || true)
    if [ -n "$COMMITS" ]; then
        NON_CONVENTIONAL=$(git log "$RANGE" --format="%h %s" | grep -vE '^[a-f0-9]+ (feat|fix|test|docs|chore|refactor|ci|style|perf|build|revert)(\([a-zA-Z0-9_.-]+\))?!?: .+' || true)
        if [ -n "$NON_CONVENTIONAL" ]; then
            echo "$NON_CONVENTIONAL"
            fail "Found commits not following Conventional Commits format (code-standards.md §9)"
        fi
        pass "Recent commits follow Conventional Commits format"
    else
        pass "No commits in range to verify"
    fi
else
    warn "No commits yet in repository — skipping conventional commits check"
fi

echo ""
echo "═══════════════════════════════════════════"
echo -e "  ${GREEN}ALL 11 GATES PASSED — Safe to push${NC}"
echo "═══════════════════════════════════════════"
