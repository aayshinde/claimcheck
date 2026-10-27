# Build vs. buy: claimant qualification for a mass tort practice

A one-page memo of the kind I would write for firm leadership before committing engineering time.

## The workflow

Intake → collect records → first-pass review against case criteria → paralegal/attorney review → follow-up records
requests → qualification decision → status in the case management system (CMS) → reporting to leadership and funders.

## Options

| Option | Good for | Watch out for |
|---|---|---|
| **A. Outsourced record review / medical chronology service** | No engineering; human reviewers; quick to start | Per-file cost grows with claimant volume; turnaround time; criteria logic lives outside the firm; data-sharing agreements |
| **B. Legal AI SaaS (PI / mass tort platforms)** | Polished UI, vendor handles models and upkeep | Fit with *this* firm's criteria and CMS; per-seat or per-document pricing; where PHI is processed; lock-in of the firm's structured data |
| **C. AI features inside the firm's CMS** | Already in the workflow, no new login | Usually generic (summaries, Q&A), not case-criteria aware; limited control over accuracy checks |
| **D. Build on general models (this prototype)** | Exact fit to the firm's criteria and review process; firm owns the data and rules; cost close to zero with local models | Needs an owner to maintain it; must prove accuracy; responsibility for security sits in-house |

## How I would decide

Score each option on the same 20–50 real, already-decided claimant files:

1. **Accuracy:** false qualifications (must be ~0) and recall of the facts that matter.
2. **Reviewer time saved:** minutes per file, measured with the paralegals who do the work today.
3. **Cost per claimant at the firm's real volume**, including review time, not just license or token fees.
4. **Data control:** where PHI goes, BAA availability, whether the firm can export its structured data.
5. **Fit:** case criteria as configuration, the CMS integration, and audit trail requirements.
6. **Time to value and maintenance burden.**

## My recommendation pattern

- **Buy** commodity pieces where the market is mature and the firm gains nothing from owning them: OCR, e-signature,
  general document storage, and possibly medical chronology summaries if a vendor meets the accuracy bar.
- **Build** the thin layer that is specific to this firm: case criteria, the review workflow, CMS integration and
  reporting. That's where the firm's judgment lives, and it changes with every new tort.
- **Keep the decision logic out of any black box.** Whatever extracts the facts, qualification rules and the audit
  trail should stay inspectable by the firm.

ClaimCheck is the "build" option at prototype size. It exists to put real numbers (accuracy, review minutes,
cost per claimant) next to vendor quotes, not to assume building is the answer.
