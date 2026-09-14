"""Form validation.

The HTML form posts strings. This module turns those strings into a clean,
normalised object or raises a per-field error map the template can render.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

CNIC_RE = re.compile(r"^\d{5}-\d{7}-\d$")
PHONE_RE = re.compile(r"^03\d{9}$")
NAME_RE = re.compile(r"^[A-Za-z\u00C0-\u024F.'\- ]+$")


class ApplicationForm(BaseModel):
    """A validated CV submission."""

    # An unchecked box is simply absent from the POST body, so the default must
    # be validated too — otherwise "no consent given" silently passes.
    model_config = ConfigDict(validate_default=True)

    full_name: str = Field(min_length=3, max_length=80)
    email: EmailStr
    cnic: str
    phone: str
    portfolio_url: str = ""
    note: str = Field(default="", max_length=1000)
    consent: bool = False

    # --- normalisers ---------------------------------------------------------

    @field_validator("full_name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not NAME_RE.match(v):
            raise ValueError("Use letters, spaces, hyphens and apostrophes only.")
        return v

    @field_validator("cnic")
    @classmethod
    def _clean_cnic(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if len(digits) != 13:
            raise ValueError("A CNIC has 13 digits, like 42101-1234567-8.")
        formatted = f"{digits[:5]}-{digits[5:12]}-{digits[12]}"
        if not CNIC_RE.match(formatted):
            raise ValueError("That doesn't look like a valid CNIC number.")
        return formatted

    @field_validator("phone")
    @classmethod
    def _clean_phone(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if digits.startswith("92"):
            digits = "0" + digits[2:]
        if not PHONE_RE.match(digits):
            raise ValueError("Use a Pakistani mobile number, like 0301 2345678.")
        return f"{digits[:4]} {digits[4:]}"

    @field_validator("portfolio_url")
    @classmethod
    def _clean_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            return ""
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        if len(v) > 300 or " " in v:
            raise ValueError("Paste a single link, starting with https://")
        return v

    @field_validator("note")
    @classmethod
    def _clean_note(cls, v: str) -> str:
        return v.strip()

    @field_validator("consent")
    @classmethod
    def _must_consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("We need your permission to store these details.")
        return v


# Messages from our own validators above are already written for a human. These
# cover pydantic's built-in constraint failures, which are not.
FRIENDLY: dict[tuple[str, str], str] = {
    ("full_name", "string_too_short"): "Enter your full name.",
    ("full_name", "string_too_long"): "Shorten this to 80 characters or fewer.",
    ("full_name", "missing"): "Enter your full name.",
    ("email", "value_error"): "Check this address — it needs an @ and a domain.",
    ("email", "missing"): "Enter the email address we should reply to.",
    ("note", "string_too_long"): "Trim this to 1000 characters or fewer.",
    ("cnic", "missing"): "Enter your CNIC number.",
    ("phone", "missing"): "Enter a mobile number.",
    ("consent", "missing"): "We need your permission to store these details.",
}

BY_TYPE: dict[str, str] = {
    "missing": "This one is required.",
    "string_too_short": "This is too short.",
    "string_too_long": "This is too long.",
}


def errors_by_field(exc) -> dict[str, str]:
    """Flatten a pydantic ValidationError into {field: one readable message}."""
    out: dict[str, str] = {}
    for err in exc.errors():
        field = str(err["loc"][0]) if err["loc"] else "form"
        if field in out:
            continue

        err_type = err["type"]
        # Pydantic labels every custom `raise ValueError` as "value_error".
        kind = "value_error" if err_type.startswith("value_error") else err_type

        if (field, kind) in FRIENDLY:
            out[field] = FRIENDLY[(field, kind)]
        elif kind == "value_error":
            # Our validators' own wording, minus pydantic's prefix.
            out[field] = err["msg"].replace("Value error, ", "")
        else:
            out[field] = BY_TYPE.get(kind, "Check this field.")
    return out
