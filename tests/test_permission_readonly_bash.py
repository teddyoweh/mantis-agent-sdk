"""Read-only bash auto-allow, destructive-git danger patterns, prefix rules,
rule persistence and session-allow carry-over (item 20a)."""

from __future__ import annotations

import json

import anyio
import pytest

from mantis_agent.permission_shell import classify_bash_readonly
from mantis_agent.permissions import (
    Allow,
    Deny,
    PermissionContext,
    PermissionRule,
    PermissionRuleSet,
    check_permission,
    classify_bash_command,
    is_read_only_call,
    rules_from_settings,
    suggest_prefix_rule,
)
from mantis_agent.settings import add_permission_rule, load_setting_source, save_setting_source
from mantis_agent.tools import tool


@tool(is_read_only=False)
async def bash(command: str, timeout: int = 120, stdin: str = "", run_in_background: bool = False) -> str:
    """shell"""
    return ""


@tool(is_read_only=False)
async def powershell(command: str) -> str:
    """not a POSIX shell"""
    return ""


BASH = bash

# ---------------------------------------------------------------------------
# Classification table
# ---------------------------------------------------------------------------

READ_ONLY = [
    "ls", "ls -la", "pwd", "cat README.md", "head -n 5 x", "tail -20 log.txt",
    "wc -l *.py", "stat x", "du -sh .", "df -h", "which python", "echo hi",
    "printf '%s\\n' a", "grep -r foo .", "rg foo", "find . -name '*.py'",
    "tree -L 2", "sort -u x", "uniq x", "diff a b", "basename /a/b", "date",
    "uname -a", "whoami", "id -u", "hostname",
    "git status", "git diff HEAD~1", "git log --oneline -5", "git show HEAD",
    "git -C dir status", "git --no-pager log", "git branch", "git branch -a",
    "git branch --list 'feat*'", "git rev-parse HEAD", "git ls-files",
    "git blame x.py", "git remote -v", "git remote show origin",
    "git config --get user.name", "git config --list", "git describe --tags",
    "git tag", "git tag -l", "git stash list", "python --version",
    "node --version", "jq . x.json", "ls 2>/dev/null", "ls 2>&1 | head",
    "ls && git status | head -3", "(ls; pwd)", "cd src && ls",
    "echo $(pwd)", "echo `pwd`", "echo $HOME", "cat < in.txt",
    "git show HEAD@{1}", "echo ${HOME}", "grep '.*' x", "date +%s", "date -u",
    "git config --global --get user.name", "git grep -n foo", "tree -a",
    "ls\npwd", "diff x /dev/null", "grep -r foo src/",
]

NOT_READ_ONLY = [
    "", "ls; rm -rf x", "cat x > y", "cat x >> y", "ls &", "ls & pwd",
    "find . -delete", "find . -exec rm {} \\;", "find . -fprint out",
    "find *", "find . $(echo -delete)", "find . $FLAGS",
    "git branch -D x", "git branch -d x", "git branch new", "git tag v1",
    "git tag -d v1", "git push", "git commit -m x", "git stash",
    "git config user.name x", "git remote add o u", "git -c core.pager=x log",
    "git log --output=x", "git diff --ext-diff", "git reset --hard",
    "echo $(rm x)", "echo `rm x`", "FOO=1 ls", "ls | xargs rm",
    "sudo ls", "env", "printenv", "printenv PATH", "env ls", "env X=1 ls", "nice ls", "sort -o out x", "uniq a b",
    "tree -o out", "rg --pre=sh foo", "date -s now", "hostname evil",
    "python x.py", "node -e 'x'", "awk '{print}' x", "sed -n 1p x", "./ls",
    "/tmp/cat x", "cat <<EOF\nx\nEOF", "ls\nrm x", "rm x", "npm test",
    "eval ls", "file -C -m m", "jq -i . x",
    # reads outside the working tree still ask
    "cat ~/.ssh/id_rsa", "cat /etc/passwd", "cat ../secret", "ls ~",
    "cat $HOME/.aws/credentials", "cat $(echo ~/.ssh/id_rsa)", "cat < /etc/shadow",
    "grep --file=/etc/x foo", "git -C /other status", "cd / && cat etc/passwd",
    "wc -l $(git ls-files)", "{ git status; cat ~/.ssh/id_rsa; }",
    # adversarial review — each of these was classified read-only before
    # long-option abbreviations (getopt_long / git parse-options prefixes)
    "sort --out=x y", "sort --compress=sh y", "file --comp -m x", "ag --pag sh x",
    "git grep --open=cmd x", "git grep --open-files x", "git branch --edit",
    "git branch --unset-up", "git branch --tr", "git tag -l --del v1",
    # a glued / clustered value
    "git grep -iOcmd x", "git grep '-iOcmd arg' x", "tree -ao out",
    "tree -R -L 1 -H x", "rg --hostname-bin sh x", "grep -rf/etc/passwd .",
    "grep -hf~/.ssh/id_rsa -r .", "head -c1/etc/passwd",
    # git config: a read flag in VALUE position still writes
    "git config set a.b --get", "git config a.b c --get",
    # network channel
    "git remote show https://evil.example/x", "git remote show host:/x",
    # date reads --file, sets via abbreviations
    "date --file=/etc/passwd", "date --fil=/etc/passwd", "date --se=1",
    # expansions that evaluate code / assign
    "printf -v 'a[$(id)]' 1", "printf -v x y", "echo ${x@P}", "echo ${a[$x]}",
    "echo $[1+1]", "echo ${!x}",
    # cd leaves the tree with no path in sight (and the cwd persists)
    "cd", "cd -", "cd && cat .ssh/id_rsa",
    # input redirects on path-free commands; read-write redirect
    "tr a b </etc/passwd", "tr a b < ~/.ssh/id_rsa", "ls <>newfile",
    # brace expansion hides paths / flags from per-word checks
    "cat {/etc/passwd,}", "cat {~/.ssh/id_rsa,x}", "sort {-o,x} y",
    "sort {--output=pwned,in}", "find . {-delete,}", "git log {--output=x,}",
    # dot-globs match `..` on bash < 5.2 (macOS /bin/bash)
    "cat .*/secret", "cat .[.]/secret", "cat .?/secret",
]


@pytest.mark.parametrize("cmd", READ_ONLY)
def test_read_only_commands_classify_read_only(cmd: str) -> None:
    assert classify_bash_readonly(cmd) is True, cmd


@pytest.mark.parametrize("cmd", NOT_READ_ONLY)
def test_mutating_or_unclear_commands_are_not_read_only(cmd: str) -> None:
    assert classify_bash_readonly(cmd) is False, cmd


def test_table_is_big_enough() -> None:
    assert len(READ_ONLY) + len(NOT_READ_ONLY) >= 40


# ---------------------------------------------------------------------------
# Decision pipeline
# ---------------------------------------------------------------------------


def _check(cmd: str, ctx: PermissionContext, t=BASH, **extra):
    async def main():
        return await check_permission(t, {"command": cmd, **extra}, ctx)

    return anyio.run(main)


def _asking_ctx(**kw) -> tuple[PermissionContext, list[str]]:
    prompts: list[str] = []

    async def asker(tool, inp, prompt):
        prompts.append(prompt)
        return "deny"

    return PermissionContext(mode="default", asker=asker, **kw), prompts


def test_default_mode_allows_read_only_bash_without_prompt() -> None:
    ctx, prompts = _asking_ctx()
    assert isinstance(_check("git status && ls -la", ctx), Allow)
    assert prompts == []


def test_default_mode_still_asks_for_mutating_bash() -> None:
    ctx, prompts = _asking_ctx()
    assert isinstance(_check("npm install", ctx), Deny)
    assert len(prompts) == 1


def test_background_read_only_command_still_asks() -> None:
    ctx, prompts = _asking_ctx()
    _check("tail -f log", ctx, run_in_background=True)
    assert len(prompts) == 1


def test_deny_rule_beats_read_only_allow() -> None:
    ctx, prompts = _asking_ctx(rules=PermissionRuleSet(deny=[PermissionRule("Bash(cat:*)", "deny")]))
    d = _check("cat README.md", ctx)
    assert isinstance(d, Deny) and "denied by rule" in d.reason
    # …including inside a compound read-only command.
    d = _check("ls && cat secret", ctx)
    assert isinstance(d, Deny)
    assert prompts == []


def test_explicit_ask_rule_beats_read_only_allow() -> None:
    ctx, prompts = _asking_ctx(rules=PermissionRuleSet(ask=[PermissionRule("Bash(git log:*)", "ask")]))
    _check("git log", ctx)
    assert len(prompts) == 1


def test_headless_read_only_allowed_in_every_mode() -> None:
    for mode in ("default", "auto", "acceptEdits"):
        ctx = PermissionContext(mode=mode)
        assert isinstance(_check("git status", ctx), Allow), mode


def test_read_only_classification_is_posix_shell_only() -> None:
    assert is_read_only_call(BASH, {"command": "ls"})
    assert not is_read_only_call(powershell, {"command": "ls"})


# ---------------------------------------------------------------------------
# Destructive git danger patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "git push --force", "git push -f origin main", "git push --force-with-lease",
    "git push origin +main", "git reset --hard HEAD~1", "git clean -fdx",
    "git clean -f", "git checkout -- .", "git checkout .", "git restore .",
    "git branch -D feature", "git -C repo push --force", "ls && git reset --hard",
    "rm -rf build", "git push -uf origin main", "git reset --har",
    "git checkout -f main", "git stash clear", "git stash drop",
])
def test_destructive_git_is_dangerous(cmd: str) -> None:
    assert classify_bash_command(cmd).is_dangerous, cmd


@pytest.mark.parametrize("cmd", [
    "git push", "git push origin main", "git reset HEAD~1", "git reset --soft HEAD~1",
    "git clean -n", "git checkout main", "git checkout -- src/x.py",
    "git restore --staged x.py", "git branch -d merged", "git status",
    "git checkout -b feat", "git stash list", "git push -u origin main",
])
def test_ordinary_git_is_not_dangerous(cmd: str) -> None:
    assert not classify_bash_command(cmd).is_dangerous, cmd


def test_headless_force_push_is_denied() -> None:
    d = _check("git push --force", PermissionContext(mode="default"))
    assert isinstance(d, Deny) and "dangerous" in d.reason


# ---------------------------------------------------------------------------
# Prefix rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd,rule", [
    ("npm run test -- -k x", "Bash(npm run test:*)"),
    ("uv run pytest -q", "Bash(uv run pytest:*)"),
    ("python -m pytest tests", "Bash(python -m pytest:*)"),
    ("git log -5", "Bash(git log:*)"),
    ("cargo build --release", "Bash(cargo build:*)"),
    ("pytest -q", "Bash(pytest:*)"),
    ("docker ps -a", "Bash(docker ps:*)"),
    ("kubectl get pods", "Bash(kubectl get:*)"),
    ("gh pr list", "Bash(gh pr:*)"),
])
def test_suggest_prefix_rule(cmd: str, rule: str) -> None:
    assert suggest_prefix_rule(cmd) == rule


@pytest.mark.parametrize("cmd", [
    "", "ls && rm x", "echo x > f", "rm -rf x", "sudo make", "python x.py",
    "bash -c 'x'", "./script.sh", "npm run", "git push --force", "echo $(x)",
    # a prefix would approve the code-running / destructive twin
    "git --no-pager log", "git", "npm -w a test", "find . -name x",
    "sed -n 1p x", "awk 1 x", "ssh host ls",
    # runs whatever the Makefile / package / container / cluster says
    "make", "make test", "npx jest", "bunx vite", "uvx ruff", "pipx run black",
    "docker run img", "docker exec c ls", "docker compose up",
    "podman run img", "podman exec c ls", "gh api repos/x/y",
    "kubectl delete pod x", "kubectl apply -f x.yml", "kubectl exec p -- ls",
    "kubectl edit deploy x", "kubectl patch deploy x", "kubectl replace -f x",
    "terraform apply", "terraform destroy", "helm install x y",
    "helm upgrade x y", "helm uninstall x",
])
def test_no_prefix_suggested_for_unsafe_shapes(cmd: str) -> None:
    assert suggest_prefix_rule(cmd) is None


def test_prefix_rule_matches_word_bounded_and_gates_compounds() -> None:
    rs = rules_from_settings({"allow": [suggest_prefix_rule("npm run test")]})
    ctx, prompts = _asking_ctx(rules=rs)
    assert isinstance(_check("npm run test -- -k x", ctx), Allow)
    assert prompts == []
    # Word boundary: `test:e2e`-style neighbours are not covered by `test`.
    _check("npm run testing", ctx)
    assert len(prompts) == 1
    # Compound: the chained segment must be allowed on its own.
    _check("npm run test && npm publish", ctx)
    assert len(prompts) == 2


def test_rules_from_settings_keeps_legacy_substring_for_deny() -> None:
    rs = rules_from_settings({"deny": ["Bash(rm -rf*)"], "allow": ["Read"]})
    assert rs is not None
    assert {r.pattern for r in rs.deny} == {"Bash(rm -rf*)", "*rm -rf**"}
    assert [r.pattern for r in rs.allow] == ["Read()"]
    assert rules_from_settings({}) is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_add_permission_rule_persists_idempotently(tmp_path) -> None:
    save_setting_source("local", {"permissions": {"allow": ["!Bash(*)"]}, "model": "m"}, cwd=tmp_path)
    path = add_permission_rule("Bash(npm run test:*)", cwd=tmp_path)
    add_permission_rule("Bash(npm run test:*)", cwd=tmp_path)
    assert path.name == "settings.local.json"
    data = load_setting_source("local", cwd=tmp_path)
    # the "!" revocation the user wrote survives (no merge_settings round-trip)
    assert data["permissions"]["allow"] == ["!Bash(*)", "Bash(npm run test:*)"]
    assert data["model"] == "m"
    assert json.loads(path.read_text())  # well-formed
    assert (path.stat().st_mode & 0o777) == 0o600
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_add_permission_rule_rejects_bad_action(tmp_path) -> None:
    with pytest.raises(ValueError):
        add_permission_rule("Bash(ls:*)", action="maybe", cwd=tmp_path)


# ---------------------------------------------------------------------------
# Session allows survive a rebuild
# ---------------------------------------------------------------------------


def test_session_allows_carry_over_to_rebuilt_context() -> None:
    old = PermissionContext()
    prompts: list[str] = []

    async def asker(tool, inp, prompt):
        prompts.append(prompt)
        return "allow_session"

    old.asker = asker
    assert isinstance(_check("npm install", old), Allow)
    assert len(prompts) == 1

    new = PermissionContext(asker=asker).carry_session_state_from(old)
    assert isinstance(_check("npm install", new), Allow)
    assert len(prompts) == 1  # not re-asked after the rebuild
    # independent sets: a new approval on the rebuilt context doesn't leak back
    _check("npm ci", new)
    assert ("bash", '{"command": "npm ci"}') not in old.session_allows
    assert new.carry_session_state_from(None) is new


def test_carried_session_allow_never_beats_danger_gate() -> None:
    old = PermissionContext()
    old.session_allows.add(("bash", '{"command": "git push --force"}'))
    new = PermissionContext().carry_session_state_from(old)
    assert isinstance(_check("git push --force", new), Deny)

