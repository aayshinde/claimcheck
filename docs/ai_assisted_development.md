# How I used AI to build this

I built ClaimCheck with an AI coding assistant (Claude Code) acting as a pair programmer. That made it possible to
cover ingestion, extraction, rules, an agent, an API, integrations, a dashboard, tests, CI and docs in a short time.
The useful parts are less about typing speed and more about how the work was steered and checked.

## What worked

- **Decide the design myself, delegate the typing.** Architecture choices (rules decide, LLM only extracts; quote
  verification; drafts-only agent; one database with pgvector) were made up front and written into `CLAUDE.md`, so
  generated code followed them.
- **Evaluation first.** The synthetic data generator writes a ground-truth file, and `evaluate.py` scores every run.
  "Looks right" was never the bar; the numbers were.
- **Let the evaluation catch mistakes.** Two real bugs were caught this way rather than by reading code:
  - The first version scored 100% everywhere. The pattern extractor had been written against the same templates that
    generated the data. I added layouts it had never seen and more scanned pages; recall dropped to an honest ~78%.
    The same change made it clear where an LLM adds value.
  - A CMS download cache compared file sizes instead of checksums, so regenerated documents of the same size were
    not refreshed. Evaluation showed two claimants whose extracted dates didn't match the ground truth. Fixed with
    SHA-256.
- **Error analysis on the LLM, one failure type at a time.** Running the local model against the ground truth showed:
  - The 3B model often returned correct values **without a quote**, which verification rightly rejected. Fix: a
    deterministic evidence locator finds the page's own sentence containing those dates/codes; if they aren't on the
    page, the fact stays unverified.
  - It sometimes **invented facts** (e.g. billing charges on a water-utility record). Verification rejected them all,
    and a token cap stopped runaway generations that were stalling batch runs.
  - It **didn't recognise pathology shorthand** ("Impression", "Dx code", "signed out") and returned nothing. Fix: a
    few lines of medical vocabulary in the prompt, the kind of thing a paralegal teaches a new hire.
  - **OCR read the ICD code "I10" as "110".** The fact matched the OCR text, so it passed. Fix: codes must be valid
    ICD-10 format to count, so garbled codes go to review.
  - The 7B model was too slow on an 8 GB laptop (~80 s per page under memory pressure); the 3B model runs at 3–8 s.
    Choosing the model was a measured trade-off, not a default.
- **Test the guardrails, not just the happy path.** Tests check that paralegals can't decide claimants, that overriding
  needs a note, that the agent can't call unknown tools or change a status, that webhook signatures are enforced, and
  that document downloads block path traversal.
- **Watch the model's behaviour.** The local 7B model sometimes *recommended* drafting a records request instead of
  calling the tool. The fix was a stricter prompt plus a deterministic safety net, and both are visible in the agent's
  step log.

## What I'd tell a team adopting AI-assisted development

Keep humans on the design, the review and the evaluation; let the assistant do volume. Require tests for anything
security- or money-related. Make the assistant's context explicit (a `CLAUDE.md` with the project's rules). Measure
outcomes instead of trusting confident-sounding output, which is the same rule ClaimCheck applies to its own LLM.
