import pytest
import yaml

from research_agent.settings import ConfigError, load_settings


def _edit(project, fname, fn):
    p = project / "config" / fname
    data = yaml.safe_load(p.read_text())
    fn(data)
    p.write_text(yaml.safe_dump(data))


def test_repository_configuration_is_valid(settings):
    assert abs(sum(d.weight for d in settings.scoring.enabled) - 1) < 1e-12
    assert settings.llm.provider == "claude_code_action" and settings.llm.auth == "oauth_subscription"
    assert settings.llm.allow_api_key_fallback is False
    ids = {j.id for j in settings.journals.journals}
    assert {"lancet_public_health", "lancet_rh_wpac", "plos_medicine", "bmc_medicine", "jama_network_open"} <= ids
    assert {t.kind for t in settings.profile.topics} == {"core", "exploratory"}


def test_weights_must_sum_to_one(project):
    _edit(project, "scoring.yaml", lambda d: d["scoring"]["dimensions"][0].__setitem__("weight", 0.5))
    with pytest.raises(ConfigError, match="sum to 1.0"):
        load_settings(project)


def test_disabled_dimension_excluded_from_weight_sum(project):
    def fn(d):
        dims = {x["id"]: x for x in d["scoring"]["dimensions"]}
        dims["cross_disciplinary_transferability"]["enabled"] = False      # 0.05
        dims["research_alignment"]["weight"] += 0.05                        # alignment absorbs it
    _edit(project, "scoring.yaml", fn)
    s = load_settings(project)
    assert "cross_disciplinary_transferability" not in [d.id for d in s.scoring.enabled]


def test_api_key_fallback_cannot_be_enabled(project):
    _edit(project, "llm.yaml", lambda d: d["llm"].__setitem__("allow_api_key_fallback", True))
    with pytest.raises(ConfigError):
        load_settings(project)


def test_invalid_issn_rejected(project):
    _edit(project, "journals.yaml", lambda d: d["journals"][0].__setitem__("issns", ["2468-266"]))
    with pytest.raises(ConfigError, match="ISSN"):
        load_settings(project)


def test_llm_dimension_needs_existing_prompt(project):
    (project / "prompts" / "innovation.md").unlink()
    with pytest.raises(ConfigError, match="prompt file not found"):
        load_settings(project)


def test_mandatory_sources(project):
    _edit(project, "sources.yaml", lambda d: d["sources"].pop("medrxiv"))
    with pytest.raises(ConfigError, match="mandatory"):
        load_settings(project)


def test_versions_change_with_rubric(project, settings):
    v1, r1 = settings.scoring.version(), settings.scoring.rubric_version()
    _edit(project, "scoring.yaml", lambda d: d["scoring"]["dimensions"][1].__setitem__("rubric_version", "1.1"))
    s2 = load_settings(project)
    assert s2.scoring.version() != v1 and s2.scoring.rubric_version() != r1
