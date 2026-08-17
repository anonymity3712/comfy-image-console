# Codex for Open Source Application Notes

Official program page: https://developers.openai.com/codex/codex-for-oss

## Eligibility Assessment

Codex for Open Source supports active maintainers of public repositories with
substantial usage, broad adoption, or clear ecosystem importance. This project
is a useful local ComfyUI console, but it is not yet ready for a competitive
application because:

| Requirement | Current status | Action |
|---|---|---|
| Public GitHub repository | Missing; only a private LAN Gitea remote exists | Create a public GitHub repository |
| Public GitHub profile | Not verified | Set profile visibility to public |
| Active maintainer role | Yes for this project | State that you are the primary maintainer |
| Substantial usage / adoption | Not yet demonstrated | Publish first, collect stars/issues/contributors/releases |
| Open-source license | Missing | Add a license after choosing one, preferably MIT or Apache-2.0 |
| Reproducible installation | Hard-coded Windows paths | Add configuration and dependency files |
| Sensitive runtime data | Ignored by Git, but working tree is dirty | Create a clean release commit |

An honest application can be submitted once the repository is public, but the
chance of selection is low until it has visible adoption. A stronger target is
at least one tagged release, external issue reports or contributors, and
evidence such as stars, clones, forks, or downstream usage.

## Public Release Blockers

1. Add `LICENSE`; MIT is simple, while Apache-2.0 adds an explicit patent grant.
2. Add `requirements.txt` and document the minimum Python version.
3. Replace hard-coded paths in `app.py` and `start.bat` with environment
   variables or a `config.example.json` file.
4. Document required ComfyUI custom nodes, model families, and model-license
   restrictions. Do not distribute model or LoRA files.
5. Keep `outputs/`, user prompts, logs, caches, and generated images out of Git.
6. Add a smoke test for startup, `/api/status`, `/api/models`, and workflow
   validation without requiring a GPU.
7. Add screenshots or a short demo to the README after the UI is stable.
8. Add contribution, security-reporting, and support policies.

## Application Copy

These drafts are intentionally honest about the project's early stage. Replace
the bracketed placeholders before submitting.

### Role

```text
I am the primary maintainer and author of [repository]. I designed the
architecture, implement features, review changes, manage releases, and handle
issues and security updates.
```

### Why this repository qualifies

```text
[Repository] is an open-source, local-first web console for ComfyUI. It turns
curated text-to-image workflows into a reproducible interface with model
selection, LoRA discovery, queued generation, PNG metadata, history, parameter
reuse, and A/B comparison. It helps creators and local operators use ComfyUI
without editing node graphs for every run.
```

If public adoption metrics become available, prepend them:

```text
[N] GitHub stars, [N] forks/clones in the last month, and [N] downstream
users/issues.
```

### How API credits will be used

```text
I will use Codex and API credits for daily maintenance: feature development,
code and security review, workflow-regression tests, release preparation,
changelogs, documentation, issue triage, and contributor-patch review. Priority
areas are path configuration, cross-platform startup, ComfyUI compatibility,
and automated smoke tests.
```

### Additional notes

```text
The project is newly public, so adoption metrics are still early. I am the
primary maintainer and plan regular tagged releases. The software does not
distribute model weights; users bring their own ComfyUI installation and models
and must follow the applicable model licenses.
```

## Recommended Sequence

1. Finish and commit the current functional cleanup.
2. Add license, dependencies, configuration, and cross-platform installation docs.
3. Tag a `v0.1.0` release and publish the public GitHub repository.
4. Share it in ComfyUI or local AI-image communities and collect real feedback.
5. Apply only after the public repository contains evidence of usage or clear
   ecosystem value; update the drafts above with actual metrics.
