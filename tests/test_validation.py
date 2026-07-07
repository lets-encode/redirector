import pytest

from app import validation


@pytest.mark.parametrize("name", [
    "abc",
    "my-campaign",
    "a1b",
    "x" * 40,
    "a-1-b-2",
    "renaissance-lute-tablature",
])
def test_valid_names(name):
    assert validation.registration_error(name) is None


@pytest.mark.parametrize("name", [
    "",            # empty
    "a",           # too short (regex alone would allow this)
    "ab",          # too short
    "x" * 41,      # too long
    "My-Campaign", # uppercase
    "MYCAMPAIGN",  # uppercase
    "-abc",        # leading hyphen
    "abc-",        # trailing hyphen
    "ab--cd",      # double hyphen (regex alone would allow this)
    "my%20name",   # percent-encoding
    "my%2Dname",   # percent-encoding, even of an allowed char
    "my name",     # space
    "my_name",     # underscore
    "café-fund",   # non-ascii
    "my.name",     # dot
    "a/b/c",       # slash
])
def test_syntax_rejections(name):
    assert validation.registration_error(name) == validation.ERR_SYNTAX


@pytest.mark.parametrize("name", ["api", "admin", "assets", "static", "register", "healthz"])
def test_reserved_names_refused(name):
    assert validation.registration_error(name) == validation.ERR_RESERVED
