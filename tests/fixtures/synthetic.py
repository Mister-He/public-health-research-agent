"""SYNTHETIC test records (not real publications).

DOIs use Crossref's test prefix 10.5555; PMIDs are 9-digit values above the current PubMed
range. Every abstract starts with a synthetic marker so no record can be mistaken for real.
"""

from __future__ import annotations

from datetime import date, timedelta

from research_agent.metadata.schemas import SourceRecord

REPORT_DATE = date(2026, 10, 21)
MARK = "SYNTHETIC TEST RECORD, not a real publication."


def _d(days_ago: int) -> date:
    return REPORT_DATE - timedelta(days=days_ago)


CORE = [
    ("Bayesian calibration of a lung cancer screening microsimulation model using emulators",
     "We propose a novel Bayesian calibration approach for a lung cancer screening microsimulation model, "
     "using Gaussian process emulators and probabilistic sensitivity analysis to quantify uncertainty in "
     "risk-stratified screening strategies.", ["microsimulation", "Bayesian calibration"], 2),
    ("Dynamic transmission model of hepatitis C elimination among people who inject drugs in prisons",
     "A dynamic transmission model of hepatitis C among people who inject drugs and incarcerated populations "
     "evaluates elimination strategies, with model calibration and cost-effectiveness analysis of test-and-treat.",
     ["hepatitis C", "transmission model", "incarceration"], 3),
    ("Distributional cost-effectiveness analysis of national tobacco control policies",
     "We conduct a distributional cost-effectiveness analysis of tobacco control policies using a population "
     "simulation model, reporting equity impacts and health technology assessment implications.",
     ["distributional cost-effectiveness", "tobacco control"], 4),
    ("Agent-based model of prostate cancer screening uptake and overdiagnosis",
     "An agent-based model simulates prostate cancer screening behaviour, overdiagnosis and resource allocation "
     "under alternative PSA screening policies with model validation against registry data.",
     ["agent-based model", "prostate cancer screening"], 5),
    ("Optimal risk-stratified liver cancer screening via decision-analytic Markov models",
     "A decision-analytic Markov model identifies optimal risk-stratified liver cancer screening intervals for "
     "hepatocellular carcinoma surveillance, with probabilistic sensitivity analysis and value of information.",
     ["Markov model", "hepatocellular carcinoma"], 6),
    ("Uncertainty quantification for compartmental epidemic models with approximate Bayesian computation",
     "We develop uncertainty quantification methods for compartmental models using approximate Bayesian "
     "computation and evaluate parameter estimation and model validation on simulated outbreaks.",
     ["compartmental model", "uncertainty quantification"], 7),
    ("Health economic evaluation of screening optimization for colorectal cancer using microsimulation",
     "A health economic evaluation combines microsimulation and cost-utility analysis to optimize screening "
     "start ages, reporting QALY gains and incremental cost-effectiveness ratios.",
     ["cost-utility", "screening optimization"], 9),
    ("Target trial emulation integrated with simulation for evaluating smoking cessation policy",
     "We integrate causal inference through target trial emulation with a simulation model of smoking "
     "cessation to evaluate policy optimization under uncertainty.", ["causal inference", "smoking cessation"], 10),
    ("Resource allocation for tuberculosis transmission control using compartmental modelling",
     "Compartmental modelling of tuberculosis transmission informs resource allocation and intervention "
     "prioritization across districts in a national-scale health policy evaluation.",
     ["resource allocation", "tuberculosis transmission"], 12),
    ("Discrete-event simulation of cancer screening capacity and adaptive intervention design",
     "A discrete-event simulation of cancer screening capacity supports adaptive intervention design and "
     "healthcare intervention planning with external validation.", ["discrete-event simulation"], 14),
]
EXPLORATORY = [
    ("Reinforcement learning for adaptive cancer screening policies in a simulated population",
     "We apply reinforcement learning to a Markov decision process representing adaptive cancer screening "
     "policies in a simulated population, comparing learned policies with guideline strategies.",
     ["reinforcement learning", "Markov decision process"], 3),
    ("Retrieval-augmented generation with a knowledge graph for evidence synthesis in health economics",
     "A retrieval-augmented generation pipeline using a knowledge graph and a large language model supports "
     "evidence synthesis for health economic model parameterization.",
     ["retrieval-augmented generation", "knowledge graph", "large language model"], 4),
    ("Large language model agents for automating systematic review screening",
     "We evaluate large language model agents in an agentic workflow for systematic review screening and data "
     "extraction for evidence synthesis.", ["large language model", "systematic review"], 6),
]
IRRELEVANT = [
    ("Crystal structure of a bacterial membrane transporter",
     "We report the crystal structure of a membrane transporter and characterize its binding pocket.", [], 2),
    ("Surgical outcomes after knee arthroplasty in a single centre",
     "A single-centre retrospective series describes surgical outcomes after knee arthroplasty.", [], 3),
]


def _rec(i: int, title: str, abstract: str, kws: list[str], days_ago: int, *, query: str = "topic:fixture",
         provider: str = "fixture", **kw) -> SourceRecord:
    return SourceRecord(
        provider=provider, source_record_id=f"fx-{i:04d}", query_id=query, strategy="fixture",
        retrieved_at=f"{REPORT_DATE.isoformat()}T00:00:{i % 60:02d}Z",
        doi=kw.pop("doi", f"10.5555/fixture.{i:04d}"), title=title, authors=[f"Testauthor{i}, A.", "Example, B."],
        journal=kw.pop("journal", "Journal of Synthetic Public Health Fixtures"), journal_issns=["0000-0000"],
        publication_type="journal-article", online_date=kw.pop("online_date", _d(days_ago)),
        indexed_date=_d(days_ago), abstract=f"{MARK} {abstract}" if abstract else None,
        author_keywords=kws, source_url=f"https://example.org/fixture/{i}", **kw)


def records() -> list[SourceRecord]:
    out: list[SourceRecord] = []
    i = 1
    for group in (CORE, EXPLORATORY, IRRELEVANT):
        for title, abstract, kws, days in group:
            out.append(_rec(i, title, abstract, kws, days))
            i += 1
    # Duplicate discovery of record 1 through a second query (must merge, not duplicate).
    out.append(_rec(1, CORE[0][0], CORE[0][1], CORE[0][2], CORE[0][3], query="journal:fixture"))
    # Missing abstract (must render the explicit unavailable marker).
    out.append(_rec(40, "Microsimulation of cervical cancer screening programmes without abstract", "", [], 5))
    # Out of the 30-day horizon.
    out.append(_rec(41, "Microsimulation model calibration for colorectal cancer screening (older)",
                    "An older microsimulation calibration study of screening strategies.", ["microsimulation"], 60))
    # Retracted.
    out.append(_rec(42, "Cost-effectiveness analysis of hepatitis C treatment retracted", "A cost-effectiveness "
                    "analysis of hepatitis C treatment with a transmission model.", ["hepatitis C"], 4, is_retracted=True))
    return out
