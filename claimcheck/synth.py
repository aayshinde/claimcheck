"""Synthetic claimant files for the fictional Cedar Creek litigation.

No real medical records are used anywhere in this project. Each claimant gets 4-8 PDFs (intake questionnaire, exposure
record, diagnosis record, visit notes, itemized bill) written in several phrasings and date formats, plus a ground-truth
file (gold.json) used by the evaluation. Scenarios are mixed on purpose: qualified claimants, exposure outside the window,
too-short exposure, non-qualifying diagnoses, a diagnosis only self-reported, a missing exposure record, a diagnosis that
predates exposure, and one scanned (image-only) record that needs OCR.

    python -m claimcheck.synth --n 24
"""
from __future__ import annotations

import argparse
import io
import json
import random
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from faker import Faker
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from claimcheck import config

QUALIFYING = [
    ("Clear cell renal cell carcinoma, left kidney", "C64.2", "pathology_report"),
    ("Urothelial carcinoma of the bladder", "C67.9", "pathology_report"),
    ("Hepatocellular carcinoma", "C22.0", "pathology_report"),
    ("Diffuse large B-cell lymphoma", "C83.30", "pathology_report"),
    ("Idiopathic Parkinson's disease", "G20", "specialist_note"),
]
NON_QUALIFYING = [
    ("Type 2 diabetes mellitus without complications", "E11.9", "specialist_note"),
    ("Essential (primary) hypertension", "I10", "specialist_note"),
    ("Malignant melanoma of skin, unspecified", "C43.9", "pathology_report"),
]
JOBS = ["process operator", "maintenance technician", "lab assistant", "shift supervisor", "warehouse clerk"]
FACILITIES = ["Cedar Valley Medical Center", "Lakeshore Regional Hospital", "Northfield Family Clinic",
              "St. Brendan Oncology Associates", "Riverside Neurology Group"]
VISIT_REASONS = [
    ("flank pain and fatigue", "Ordered CT abdomen/pelvis; labs drawn."),
    ("follow-up of imaging results", "Discussed findings; referred to specialist."),
    ("post-operative check", "Incision healing well; continue monitoring."),
    ("tremor in right hand", "Started carbidopa/levodopa trial."),
    ("routine follow-up", "Stable. Return in 3 months."),
    ("medication review", "Adjusted dosage; labs in 6 weeks."),
]
CPT = [("99214", "Office visit, established patient", 185.00), ("74177", "CT abdomen and pelvis with contrast", 1420.00),
       ("99285", "Emergency department visit, high severity", 2310.00), ("88307", "Surgical pathology, gross and micro", 640.00),
       ("99204", "Office visit, new patient", 310.00)]

# scenario -> expected status, distributed over the claimants in this order (cycled)
SCENARIOS = ["qualified", "qualified", "outside_window", "qualified", "short_exposure", "non_qualifying_dx",
             "qualified", "self_reported_dx", "missing_exposure_doc", "qualified", "dx_before_exposure", "qualified_scanned",
             "qualified", "short_exposure", "qualified", "non_qualifying_dx", "outside_window", "qualified",
             "self_reported_dx", "missing_exposure_doc", "qualified", "qualified", "dx_before_exposure", "qualified"]
EXPECTED = {"qualified": "qualified", "qualified_scanned": "qualified", "outside_window": "not_qualified",
            "short_exposure": "not_qualified", "non_qualifying_dx": "not_qualified", "dx_before_exposure": "not_qualified",
            "self_reported_dx": "needs_review", "missing_exposure_doc": "needs_review"}


MESSY_RATE = 0.3    # share of records written in layouts the pattern extractor was not built for
SCAN_RATE = 0.08    # extra records turned into image-only scans (OCR needed)


@dataclass
class GoldClaimant:
    external_id: str
    name: str
    dob: str
    scenario: str
    expected_status: str
    facts: list[dict] = field(default_factory=list)     # {fact_type, file, page, ...typed fields}


# ------------------------------------------------------------------------------------------------ date phrasing
def fmt(d: date, rng: random.Random) -> str:
    style = rng.choice(["us", "long", "iso", "long2"])
    if style == "us":
        return d.strftime("%m/%d/%Y")
    if style == "iso":
        return d.isoformat()
    if style == "long2":
        return f"{d.day} {d.strftime('%B %Y')}"
    return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")


def rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, max(0, (end - start).days)))


# ------------------------------------------------------------------------------------------------ pdf writing
_styles = getSampleStyleSheet()


def write_pdf(path: Path, title: str, subtitle: str, blocks: list) -> None:
    doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.8 * inch, bottomMargin=0.8 * inch, title=title)
    story = [Paragraph(title, _styles["Title"]), Paragraph(subtitle, _styles["Italic"]), Spacer(1, 14)]
    for b in blocks:
        if isinstance(b, list):          # table rows
            t = Table(b, hAlign="LEFT")
            t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("GRID", (0, 0), (-1, -1), 0.4, "#888888"),
                                   ("BACKGROUND", (0, 0), (-1, 0), "#e8e8e8")]))
            story += [t, Spacer(1, 10)]
        else:
            story += [Paragraph(b, _styles["BodyText"]), Spacer(1, 6)]
    doc.build(story)


def make_scanned(path: Path, rng: random.Random) -> None:
    """Re-render a PDF as a slightly rotated, noisy image-only PDF (no text layer), like a fax or scan."""
    import pymupdf
    from PIL import Image, ImageFilter

    src = pymupdf.open(path)
    out = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
        img = img.rotate(rng.uniform(-1.2, 1.2), fillcolor=255, expand=False).filter(ImageFilter.GaussianBlur(0.5))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        p = out.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=buf.getvalue())
    src.close()
    out.save(path)
    out.close()


# ------------------------------------------------------------------------------------------------ claimant builder
def build_claimant(i: int, scenario: str, fake: Faker, rng: random.Random, out_dir: Path) -> GoldClaimant:
    ext = f"CC-{1000 + i}"
    name = fake.name()
    dob = rand_date(rng, date(1940, 1, 1), date(1975, 12, 31))
    folder = out_dir / ext
    folder.mkdir(parents=True, exist_ok=True)
    g = GoldClaimant(ext, name, dob.isoformat(), scenario, EXPECTED[scenario])
    street = f"{rng.randint(10, 990)} {rng.choice(['Elm', 'Oak', 'Birch', 'Mill', 'River', 'Church'])} " \
             f"{rng.choice(['Street', 'Road', 'Lane', 'Avenue'])}"

    # --- exposure period
    if scenario == "outside_window":
        exp_start = rand_date(rng, date(2007, 1, 1), date(2012, 1, 1))
        exp_end = exp_start + timedelta(days=rng.randint(400, 2000))
    elif scenario == "short_exposure":
        exp_start = rand_date(rng, date(1988, 1, 1), date(2003, 1, 1))
        exp_end = exp_start + timedelta(days=rng.randint(6, 24))
    else:
        exp_start = rand_date(rng, date(1985, 6, 1), date(2001, 1, 1))
        exp_end = exp_start + timedelta(days=rng.randint(200, 3000))
    kind = rng.choice(["residence", "employment"])

    # --- diagnosis
    if scenario == "non_qualifying_dx":
        dx = rng.choice(NON_QUALIFYING)
    else:
        dx = rng.choice(QUALIFYING)
    if scenario == "dx_before_exposure":
        dx_date = exp_start - timedelta(days=rng.randint(200, 1500))
    else:
        dx_date = rand_date(rng, max(exp_end, date(2008, 1, 1)), date(2024, 6, 1))

    # 1) intake questionnaire (self-report; never proof)
    write_pdf(folder / "01_intake_questionnaire.pdf", "Claimant Intake Questionnaire",
              "Cedar Creek Litigation Intake Team — completed by claimant", [
                  f"Full name: {name}", f"Date of birth: {fmt(dob, rng)}",
                  f"Cedar Creek address(es): {street}, Cedar Creek Township",
                  f"Approximate years in Cedar Creek: {exp_start.year}–{exp_end.year}",
                  f"Condition claimed: {dx[0]}", f"Approximate year of diagnosis: {dx_date.year}",
                  "I certify the above is true to the best of my knowledge.",
              ])

    # 2) exposure record
    if scenario != "missing_exposure_doc":
        fn = "02_exposure_record.pdf"
        narrative, messy = rng.random() < 0.4, rng.random() < MESSY_RATE
        if kind == "residence":
            title, sub = "Cedar Creek Municipal Water Utility", "Residential Service History — certified copy"
            if messy:
                line = (f"Per our archive, the subscriber took water at {street} commencing {fmt(exp_start, rng)}; "
                        f"service was terminated on {fmt(exp_end, rng)} at customer request.")
            elif narrative:
                line = (f"Our records show that water service for {name} at {street}, Cedar Creek Township was active "
                        f"from {fmt(exp_start, rng)} through {fmt(exp_end, rng)}.")
            else:
                line = f"Service start date: {fmt(exp_start, rng)}. Service end date: {fmt(exp_end, rng)}."
            blocks = [f"Account holder: {name}", f"Service address: {street}, Cedar Creek Township", line,
                      "Water for this service address was supplied by the Cedar Creek Wellfield."]
        else:
            title, sub = "Cedar Creek Chemical Works", "Human Resources — Employment Verification"
            job = rng.choice(JOBS)
            if messy:
                line = f"Employment dates: {fmt(exp_start, rng)} – {fmt(exp_end, rng)} ({job}, full time)."
            elif narrative:
                line = (f"This letter confirms that {name} was employed at our Cedar Creek plant as a {job} beginning "
                        f"{fmt(exp_start, rng)} and ending {fmt(exp_end, rng)}.")
            else:
                line = f"Date of hire: {fmt(exp_start, rng)}. Date of separation: {fmt(exp_end, rng)}. Position: {job}."
            blocks = [f"Employee: {name}", "Work location: Cedar Creek Plant, Cedar Creek Township", line,
                      "Please contact HR with any questions regarding this verification."]
        write_pdf(folder / fn, title, sub, blocks)
        g.facts.append({"fact_type": "exposure", "file": fn, "page": 1, "start": exp_start.isoformat(),
                        "end": exp_end.isoformat(), "kind": kind})
        if scenario == "qualified_scanned" or rng.random() < SCAN_RATE:
            make_scanned(folder / fn, rng)

    # 3) diagnosis record
    if scenario != "self_reported_dx":
        fn = "03_diagnosis_record.pdf"
        doctor = f"Dr. {fake.last_name()}"
        facility = rng.choice(FACILITIES)
        if rng.random() < MESSY_RATE:
            blocks = [f"Re: {name}", f"Referring: {doctor}",
                      f"Impression: {dx[0]}. Dx code {dx[1]}. Signed out {fmt(dx_date, rng)}.",
                      "Will forward to treating team."]
            title = "Surgical Pathology Report" if dx[2] == "pathology_report" else "Specialist Consultation Note"
            write_pdf(folder / fn, title, facility, blocks)
        elif dx[2] == "pathology_report":
            blocks = [f"Patient: {name}    DOB: {fmt(dob, rng)}", f"Pathologist: {doctor}, {facility}",
                      f"Date of report: {fmt(dx_date, rng)}",
                      "Specimen: core needle biopsy" if rng.random() < 0.5 else "Specimen: surgical resection",
                      f"FINAL DIAGNOSIS: {dx[0]}.", f"ICD-10: {dx[1]}",
                      "Comment: Findings discussed with the treating physician."]
            write_pdf(folder / fn, "Surgical Pathology Report", facility, blocks)
        else:
            blocks = [f"Patient: {name}", f"Seen by: {doctor} at {facility}",
                      f"Date of visit: {fmt(dx_date, rng)}",
                      "History: Patient presents for evaluation of symptoms reported by primary care.",
                      f"Assessment: {dx[0]} (ICD-10 {dx[1]}), diagnosed at this visit.",
                      "Plan: Treatment options reviewed with patient."]
            write_pdf(folder / fn, "Specialist Consultation Note", facility, blocks)
        g.facts.append({"fact_type": "diagnosis", "file": fn, "page": 1, "condition": dx[0], "icd10": dx[1],
                        "date": dx_date.isoformat()})
        if rng.random() < SCAN_RATE:
            make_scanned(folder / fn, rng)

    # 4) visit notes, with an occasional long gap in treatment
    visit_dates, d = [], dx_date + timedelta(days=rng.randint(10, 40))
    for k in range(rng.randint(2, 4)):
        visit_dates.append(d)
        d = d + timedelta(days=rng.randint(150, 260) if rng.random() < 0.3 else rng.randint(20, 70))
    charges = []
    for k, vd in enumerate(visit_dates):
        fn = f"{4 + k:02d}_visit_note.pdf"
        doctor, facility = f"Dr. {fake.last_name()}", rng.choice(FACILITIES)
        reason, plan = rng.choice(VISIT_REASONS)
        r = rng.random()
        if r < MESSY_RATE:
            blocks = [f"Pt: {name}", f"DOS {fmt(vd, rng)} | Attending {doctor} ({facility})", f"CC: {reason}.",
                      f"A/P: {plan}"]
        elif r < 0.65:
            blocks = [f"Patient: {name}", f"Visit date: {fmt(vd, rng)}", f"Provider: {doctor}", f"Facility: {facility}",
                      f"Chief complaint: {reason}.", f"Plan: {plan}"]
        else:
            blocks = [f"Patient: {name}",
                      f"{name.split()[0]} was seen in clinic on {fmt(vd, rng)} by {doctor} at {facility} for {reason}.",
                      f"Plan: {plan}"]
        write_pdf(folder / fn, "Clinic Visit Note", facility, blocks)
        g.facts.append({"fact_type": "visit", "file": fn, "page": 1, "date": vd.isoformat(), "provider": doctor,
                        "facility": facility})
        code, desc, amt = rng.choice(CPT)
        charges.append((vd, code, desc, round(amt * rng.uniform(0.9, 1.2), 2)))

    # 5) itemized bill
    fn = "09_itemized_bill.pdf"
    rows = [["Date of service", "CPT", "Description", "Charge"]]
    rows += [[c[0].strftime("%m/%d/%Y"), c[1], c[2], f"${c[3]:,.2f}"] for c in charges]
    rows.append(["", "", "Total charges", f"${sum(c[3] for c in charges):,.2f}"])
    write_pdf(folder / fn, "Itemized Statement of Charges", "Patient Financial Services", [
        f"Patient: {name}", f"Statement date: {fmt(visit_dates[-1] + timedelta(days=30), rng)}", rows])
    for c in charges:
        g.facts.append({"fact_type": "charge", "file": fn, "page": 1, "date": c[0].isoformat(), "cpt": c[1],
                        "amount": c[3]})
    return g


def generate(n: int = 24, seed: int = 7, out_dir: Path | None = None) -> list[GoldClaimant]:
    out_dir = out_dir or config.DOCS_DIR
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    fake, rng = Faker("en_US"), random.Random(seed)
    Faker.seed(seed)
    golds = [build_claimant(i, SCENARIOS[i % len(SCENARIOS)], fake, rng, out_dir) for i in range(n)]
    (out_dir / "gold.json").write_text(json.dumps([asdict(g) for g in golds], indent=1))
    return golds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    golds = generate(a.n, a.seed)
    print(f"wrote {len(golds)} claimants to {config.DOCS_DIR}")


if __name__ == "__main__":
    main()
