"""Regression tests for the URL check that decides "are we signed in yet".

This is the function that carried the original production bug: a substring
test matched the Brightspace hostname where it appears inside the AAD SAML
RelayState parameter, so the code declared login successful while the browser
was still sitting on the Microsoft sign-in page.
"""

import pytest

from brightspace_mcp.auth import landed_on_home

BASE = "https://d2l.langara.bc.ca"

# A real AAD SAML request. The Brightspace host and the /d2l/home path both
# appear inside RelayState, which is exactly what fooled the substring check.
AAD_SAML = (
    "https://login.microsoftonline.com/eb1c9d1a-e6e8-4097-87fe-bb01690935b7/saml2"
    "?SAMLRequest=jZJNb9swDIb%2FiqG7bdluYltIAqQNhgXotqDJduil0AfdCLAkT5T38e%2Bn2C3a"
    "&RelayState=https%3A%2F%2Fd2l.langara.bc.ca%2Fd2l%2Fhome"
    "&SigAlg=http%3A%2F%2Fwww.w3.org%2F2001%2F04%2Fxmldsig-more%23rsa-sha256"
)


@pytest.mark.parametrize(
    "url,expected,reason",
    [
        (AAD_SAML, False, "AAD page carrying the LMS host in RelayState"),
        (f"{BASE}/d2l/home", True, "the signed-in destination"),
        (f"{BASE}/d2l/home/", True, "trailing slash"),
        (f"{BASE}/d2l/home/12345", True, "org-unit suffix under home"),
        (f"{BASE}/d2l/login", False, "the login page is not home"),
        (f"{BASE}/d2l/homepage/foo", False, "prefix over-match must not pass"),
        ("https://D2L.Langara.BC.ca/d2l/home", True, "hostname is case-insensitive"),
        (f"https://d2l.langara.bc.ca:443/d2l/home", True, "explicit default port"),
        ("https://evil.example.com/d2l/home", False, "right path, wrong host"),
        (
            "https://d2l.langara.bc.ca.evil.com/d2l/home",
            False,
            "suffix-extended host must not pass",
        ),
    ],
)
def test_landed_on_home(url: str, expected: bool, reason: str) -> None:
    assert landed_on_home(url, BASE) is expected, reason
