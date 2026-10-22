"""Static checks of the GitHub Actions workflows (security and scheduling invariants)."""

from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def load(name):
    data = yaml.safe_load((WF / name).read_text())
    data["on"] = data.pop(True, data.get("on"))   # YAML 1.1 parses `on:` as True
    return data


def test_schedule_is_0600_shanghai():
    wf = load("daily_digest.yml")
    sched = wf["on"]["schedule"][0]
    assert sched == {"cron": "0 6 * * *", "timezone": "Asia/Shanghai"}
    assert "workflow_dispatch" in wf["on"]


def test_concurrency_and_minimal_permissions():
    wf = load("daily_digest.yml")
    assert wf["concurrency"] == {"group": "research-agent-state", "cancel-in-progress": False}
    assert wf["permissions"] == {}
    perms = {name: job["permissions"] for name, job in wf["jobs"].items()}
    assert perms == {"ingest": {"contents": "write"}, "analyze": {"contents": "read"}, "publish": {"contents": "write"}}


def test_claude_only_via_oauth_in_read_only_job():
    text = (WF / "daily_digest.yml").read_text()
    assert "anthropic_api_key" not in text            # no pay-as-you-go input anywhere
    wf = load("daily_digest.yml")
    for name, job in wf["jobs"].items():
        for step in job["steps"]:
            uses = step.get("uses", "")
            if uses.startswith("anthropics/claude-code-action"):
                assert name == "analyze"
                assert "@" in uses and len(uses.split("@")[1]) == 40       # pinned to a commit SHA
                assert step["with"]["claude_code_oauth_token"] == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
                assert step.get("continue-on-error") is True               # failures are classified, not fatal
    checkout = wf["jobs"]["analyze"]["steps"][0]
    assert checkout["with"]["persist-credentials"] is False


def test_secrets_never_reach_the_claude_steps_env():
    """Only the DeepSeek steps get a secret in env, and only DEEPSEEK_API_KEY."""
    wf = load("daily_digest.yml")
    for step in wf["jobs"]["analyze"]["steps"]:
        secret_env = {k: v for k, v in (step.get("env") or {}).items() if "secrets." in str(v)}
        if step.get("name", "").startswith("DeepSeek"):
            assert secret_env == {"DEEPSEEK_API_KEY": "${{ secrets.DEEPSEEK_API_KEY }}"}
            assert "deepseek" in step["if"]
        else:
            assert not secret_env, step.get("name")


def test_provider_selects_exactly_one_branch():
    wf = load("daily_digest.yml")
    for step in wf["jobs"]["analyze"]["steps"]:
        name, cond = step.get("name", ""), step.get("if", "")
        if name.startswith("Claude") or name.startswith("Classify"):
            assert "provider == 'claude_code_action'" in cond, name
        if name.startswith("DeepSeek"):
            assert "provider == 'deepseek'" in cond, name


def test_tests_workflow_is_offline():
    wf = load("test.yml")
    assert wf["permissions"] == {"contents": "read"}
    run = " ".join(s.get("run", "") for s in wf["jobs"]["test"]["steps"])
    assert "pytest" in run and "-m live" not in run


def test_auth_check_is_manual_minimal_and_oauth_only():
    wf = load("claude-auth-check.yml")
    assert set(wf["on"]) == {"workflow_dispatch"}                 # never scheduled / on push
    assert wf["permissions"] == {"contents": "read"}
    text = (WF / "claude-auth-check.yml").read_text()
    assert "anthropic_api_key" not in text and "ANTHROPIC_API_KEY" not in text
    steps = wf["jobs"]["check"]["steps"]
    assert steps[0]["with"]["persist-credentials"] is False
    claude = next(s for s in steps if s.get("uses", "").startswith("anthropics/claude-code-action@"))
    daily = next(s for s in load("daily_digest.yml")["jobs"]["analyze"]["steps"]
                 if s.get("uses", "").startswith("anthropics/claude-code-action@"))
    assert claude["uses"] == daily["uses"]                         # same pinned release
    assert "--max-turns 1" in claude["with"]["claude_args"] and "Bash" in claude["with"]["claude_args"]
