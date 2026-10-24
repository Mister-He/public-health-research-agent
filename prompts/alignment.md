<!-- prompt_version: 1.0 | dimension: research_alignment | rubric_version: 1.1 -->
# Rubric: Research alignment (0–100)

Judge how useful this paper is to the research profile in the manifest (core topics weigh
more than exploratory ones). Reason about substance, not keyword counts.

Highest priority: (1) microsimulation and other individual-level, agent-based or dynamic
transmission simulation models; (2) AI-empowered modelling methods (machine learning, deep
learning, RL or LLMs used to build, calibrate, emulate, accelerate or optimize such models).
Papers combining both rank highest. Other core areas still count, but a standard
application without either focus should rarely score above 75.

Also consider: methodological relevance (modelling, biostatistics, simulation, calibration,
economic evaluation, decision analysis); public health / epidemiological application;
model development or validation; health economic analysis; policy and intervention
optimization; transferability to the profile's application domains (cancer screening,
tobacco, HCV/infectious disease, incarceration-related dynamics, national policy).

Anchors:
- 90–100: core methods contribution squarely in the focus (e.g. new calibration approach
  for a cancer screening microsimulation; neural-network emulator or ML calibration of a
  microsimulation; RL optimizing policies inside a simulation model).
- 70–89: strong methodological or applied fit with one core area.
- 50–69: partial fit — relevant application with standard methods, or relevant method in a
  distant application.
- 25–49: tangential; useful mostly as background.
- 0–24: not relevant (e.g. purely clinical/biological findings with no modelling/decision content).

Also report `profile_category`: `core` if the best-matching topics are core, `exploratory`
if the paper is relevant mainly through exploratory topics (LLM/RAG/RL/agents NOT applied
to modelling). AI applied to simulation or decision models is `core`.
List the matched profile topic ids in `research_domains`.
