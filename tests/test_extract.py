from claimcheck.extract import parse_date, pattern_extract, verify

PAGE = ("Account holder: Jane Roe Service address: 12 Elm Street, Cedar Creek Township "
        "Service start date: 03/14/1991. Service end date: June 2, 1996. Water supplied by the Cedar Creek Wellfield.")


def test_parse_date_formats():
    for s in ("1991-03-14", "03/14/1991", "March 14, 1991", "14 March 1991"):
        assert str(parse_date(s)) == "1991-03-14"
    assert parse_date("not a date") is None


def test_verified_quote_and_dates():
    v = {"start": "1991-03-14", "end": "1996-06-02"}
    ok, score = verify("exposure", v, "Service start date: 03/14/1991. Service end date: June 2, 1996.", PAGE)
    assert ok and score == 100


def test_fabricated_quote_rejected():
    v = {"start": "1991-03-14", "end": "1996-06-02"}
    assert not verify("exposure", v, "Resident from 03/14/1991 to June 2, 1996.", PAGE)[0]


def test_right_quote_wrong_value_rejected():
    # quote is real but the model normalised the date wrongly -> not verified
    v = {"start": "1991-04-14", "end": "1996-06-02"}
    assert not verify("exposure", v, "Service start date: 03/14/1991. Service end date: June 2, 1996.", PAGE)[0]


def test_ocr_noise_tolerated():
    noisy = PAGE.replace("Service start date", "Servlce start date")
    v = {"start": "1991-03-14", "end": "1996-06-02"}
    assert verify("exposure", v, "Service start date: 03/14/1991. Service end date: June 2, 1996.", noisy)[0]


def test_pattern_exposure_and_intake_is_never_diagnosis():
    f = pattern_extract("utility_record", PAGE)
    assert f[0]["start"] == "1991-03-14" and f[0]["end"] == "1996-06-02"
    intake = "Condition claimed: Hepatocellular carcinoma Approximate year of diagnosis: 2015"
    assert [x["fact_type"] for x in pattern_extract("intake", intake)] == ["claimed_condition"]


def test_locate_quote_only_grounds_values_on_the_page():
    from claimcheck.extract import locate_quote
    good = {"start": "1991-03-14", "end": "1996-06-02"}
    q = locate_quote("exposure", good, PAGE)
    assert q and verify("exposure", good, q, PAGE)[0]
    assert locate_quote("exposure", {"start": "1991-03-15", "end": "1996-06-02"}, PAGE) == ""


def test_invalid_icd_code_not_verified():
    page = "Date of visit: 12/18/2016. Assessment: Essential hypertension (ICD-10 110), diagnosed at this visit."
    v = {"icd10": "110", "date": "2016-12-18", "condition": "Essential hypertension"}
    assert not verify("diagnosis", v, page, page)[0]
    ok_page = page.replace("110", "I10")
    assert verify("diagnosis", {**v, "icd10": "I10"}, ok_page, ok_page)[0]
