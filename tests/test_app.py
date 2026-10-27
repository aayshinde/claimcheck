import pytest
from streamlit.testing.v1 import AppTest

from claimcheck import config

SIMPLE = ["Overview", "Claimants"]
TECHNICAL = ["Claimant details (technical)", "Accuracy & cost", "Behind the scenes"]
PAGES = SIMPLE + TECHNICAL


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(demo, page):
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=60).run()
    if page in TECHNICAL:
        at.sidebar.toggle[0].set_value(True).run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception


def test_technical_pages_hidden_by_default(demo):
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=60).run()
    assert list(at.sidebar.radio[0].options) == SIMPLE


def test_attorney_can_sign_off_from_claimants_page(demo):
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=60).run()
    at.sidebar.radio[0].set_value("Claimants").run()
    at.radio(key="grp").set_value("qualified").run()
    agree = [b for b in at.button if b.label.startswith("✔ Agree")]
    assert agree, "sign-off button missing"
    agree[0].click().run()
    assert not at.exception
    assert any(c["decision"] for c in __import__("claimcheck.service", fromlist=["x"]).claimant_summary())
