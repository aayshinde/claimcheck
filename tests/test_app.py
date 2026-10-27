import pytest
from streamlit.testing.v1 import AppTest

from claimcheck import config

PAGES = ["Overview", "All claimants", "Claimant file", "Needs a person", "Accuracy & cost", "Behind the scenes", "About"]


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(demo, page):
    at = AppTest.from_file(str(config.ROOT / "app.py"), default_timeout=60).run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
