import pytest
from pydantic import ValidationError

from api.routes.auth import RegisterRequest


def test_password_policy():
    ok = RegisterRequest(email="a@b.co", password="Str0ng!pass")
    assert ok.password == "Str0ng!pass"
    cases = {
        "alllowercase1!": "uppercase",
        "ALLUPPERCASE1!": "lowercase",
        "NoDigitsHere!": "digit",
        "NoSpecials123": "special",
        "Sh0r!t": "at least 8",
    }
    for pwd, hint in cases.items():
        with pytest.raises(ValidationError):
            RegisterRequest(email="a@b.co", password=pwd)
    # blacklist matches through case and decoration
    with pytest.raises(ValidationError, match="too common"):
        RegisterRequest(email="a@b.co", password="Password123!")
    with pytest.raises(ValidationError, match="too common"):
        RegisterRequest(email="a@b.co", password="Qwerty123!")
