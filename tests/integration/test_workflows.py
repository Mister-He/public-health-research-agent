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
    wf = load("daily_digest.yml")
    for step in wf["jobs"]["analyze"]["steps"]:
        assert "env" not in step or not any("secrets." in str(v) for v in step["env"].values())


def test_tests_workflow_is_offline():
    wf = load("test.yml")
    assert wf["permissions"] == {"contents": "read"}
    run = " ".join(s.get("run", "") for s in wf["jobs"]["test"]["steps"])
    assert "pytest" in run and "-m live" not in run
