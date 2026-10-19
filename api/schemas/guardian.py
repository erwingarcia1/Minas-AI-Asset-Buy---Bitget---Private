"""Request and response shapes for /api/guardian (v0.9.2).

Only `GuardianAccountOut` leaves the API, and `account_out` builds it field by
field from the engine's row dict: the three encrypted columns and the key
fingerprint are not fields of the model, so they cannot be serialised by
accident, whatever the row carries. Request bodies are bounded to the column
widths of migration 030 and refuse fields they do not know.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from pydantic_core import PydanticCustomError

__all__ = [
    "PASSPHRASE_EXCHANGES", "PASSPHRASE_REQUIRED", "SECRET_COLUMNS", "GuardianAccountCreate", "GuardianAccountOut",
    "GuardianAccountUpdate", "GuardianExchange", "GuardianKeyReplace", "GuardianKeyReplaceResult", "GuardianTopology",
    "account_out", "needs_passphrase",
]

# The engine's SUPPORTED_EXCHANGES and migration 030's check constraints, spelled
# out so this module does not import the exchange adapters (ccxt) to validate a
# request body; a test holds the two lists together.
GuardianExchange = Literal["bitget", "bybit", "okx", "binance"]
GuardianTopology = Literal["independent", "sub_account"]

# Columns of guardian_accounts that must never reach a response.
SECRET_COLUMNS = frozenset({"encrypted_api_key", "encrypted_secret", "encrypted_passphrase", "key_fingerprint"})

_OPTIONAL_TEXT = ("alert_phone", "alert_email_secondary", "alert_phone_secondary", "telegram_handle")

# Exchanges whose API keys come with a passphrase that every signed request
# needs. Without it the exchange refuses the key, which the engine can only
# report as "not read-only"; the body says what is missing instead.
PASSPHRASE_EXCHANGES = frozenset({"bitget", "okx"})
PASSPHRASE_REQUIRED = ("This exchange needs the key's passphrase as well. Copy it from the exchange's key page "
                       "and try again.")


def needs_passphrase(exchange: str) -> bool:
    return exchange in PASSPHRASE_EXCHANGES


def _stripped(value):
    return value.strip() if isinstance(value, str) else value


def _blank_to_none(value):
    """An empty input is "nothing", not a value: stored as NULL, never as ''."""
    if isinstance(value, str):
        return value.strip() or None
    return value


class GuardianAccountCreate(BaseModel):
    """POST /api/guardian/accounts. The key must be a separate read-only key,
    never an AutoBuy purchase key; a Bitget or OKX key also needs its
    passphrase, and the body is refused with `PASSPHRASE_REQUIRED` without
    it, before the exchange is asked."""

    model_config = ConfigDict(extra="forbid")

    exchange: GuardianExchange
    api_key: str = Field(min_length=1, max_length=256)
    api_secret: str = Field(min_length=1, max_length=256)
    # validate_default: the check below must run for an omitted passphrase too
    passphrase: Optional[str] = Field(default=None, max_length=128, validate_default=True)
    name: str = Field(min_length=1, max_length=80, description="How the customer calls this account; the label is derived from it")
    topology: GuardianTopology = "independent"
    alert_phone: Optional[str] = Field(default=None, max_length=32)
    alert_email_secondary: Optional[EmailStr] = None
    alert_phone_secondary: Optional[str] = Field(default=None, max_length=32)
    telegram_handle: Optional[str] = Field(default=None, max_length=64)
    sms_alerts_enabled: bool = False

    _strip = field_validator("api_key", "api_secret", "name", mode="before")(_stripped)
    _blank = field_validator("passphrase", *_OPTIONAL_TEXT, mode="before")(_blank_to_none)

    @field_validator("passphrase")
    @classmethod
    def _passphrase_the_exchange_needs(cls, value: Optional[str], info) -> Optional[str]:
        """`exchange` is declared first, so it is validated by the time this
        runs; when it failed it is absent from `info.data` and its own error
        speaks alone. The message is the customer's, so it is raised as a
        custom error and not prefixed with "Value error"."""
        if needs_passphrase(info.data.get("exchange", "")) and not value:
            raise PydanticCustomError("passphrase_required", PASSPHRASE_REQUIRED)
        return value


class GuardianKeyReplace(BaseModel):
    """PUT /api/guardian/accounts/{id}/key: a new read-only key for the SAME
    exchange account."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1, max_length=256)
    api_secret: str = Field(min_length=1, max_length=256)
    passphrase: Optional[str] = Field(default=None, max_length=128)

    _strip = field_validator("api_key", "api_secret", mode="before")(_stripped)
    _blank = field_validator("passphrase", mode="before")(_blank_to_none)

    def missing_passphrase_for(self, exchange: str) -> bool:
        """Does this body lack a passphrase the account's exchange needs? The
        exchange is the account's, not the body's, so the route asks with the
        row's value and refuses with `PASSPHRASE_REQUIRED`."""
        return needs_passphrase(exchange) and not self.passphrase


class GuardianKeyReplaceResult(BaseModel):
    """What a successful replacement answers. A refusal is a 400 whose
    `detail` is the plain-language message; the technical reason stays in
    the log."""

    ok: bool
    message: str


class GuardianAccountUpdate(BaseModel):
    """PATCH /api/guardian/accounts/{id}: where alerts go and the SMS switch.
    A field sent as null clears it; one left out is not touched."""

    model_config = ConfigDict(extra="forbid")

    alert_phone: Optional[str] = Field(default=None, max_length=32)
    alert_email_secondary: Optional[EmailStr] = None
    alert_phone_secondary: Optional[str] = Field(default=None, max_length=32)
    telegram_handle: Optional[str] = Field(default=None, max_length=64)
    sms_alerts_enabled: Optional[bool] = None

    _blank = field_validator(*_OPTIONAL_TEXT, mode="before")(_blank_to_none)

    def changes(self) -> dict:
        """Only what the client sent. `sms_alerts_enabled` sent as null is not
        an answer for a switch and is ignored."""
        sent = self.model_dump(exclude_unset=True)
        if "sms_alerts_enabled" in sent and sent["sms_alerts_enabled"] is None:
            del sent["sms_alerts_enabled"]
        return sent


class GuardianAccountOut(BaseModel):
    """One monitored account as its owner sees it. There is no field for key
    material or for the key fingerprint. Times are UTC."""

    id: int
    label: str
    name: Optional[str] = None
    exchange: str
    topology: str
    account_mode: Optional[str] = None
    status: str
    baseline_status: str
    exchange_uid: Optional[str] = None
    sms_alerts_enabled: bool
    alert_phone: Optional[str] = None
    alert_email_secondary: Optional[str] = None
    alert_phone_secondary: Optional[str] = None
    telegram_handle: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_success_at: Optional[datetime] = None

    @field_validator("created_at", "updated_at", "last_success_at")
    @classmethod
    def _as_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def account_out(row: Mapping) -> GuardianAccountOut:
    """The engine's row (guardian_accounts joined with the poll state's
    `last_success_at`) as a response. Explicit field by field: nothing the
    row carries is copied by name."""
    return GuardianAccountOut(
        id=row["id"],
        label=row["label"],
        name=row.get("customer_name"),
        exchange=row["exchange"],
        topology=row["topology"],
        account_mode=row.get("account_mode"),
        status=row["status"],
        baseline_status=row["baseline_status"],
        exchange_uid=row.get("exchange_uid"),
        sms_alerts_enabled=bool(row["sms_alerts_enabled"]),
        alert_phone=row.get("alert_phone"),
        alert_email_secondary=row.get("alert_email_secondary"),
        alert_phone_secondary=row.get("alert_phone_secondary"),
        telegram_handle=row.get("telegram_handle"),
        created_at=row["onboarded_at"],
        updated_at=row["updated_at"],
        last_success_at=row.get("last_success_at"),
    )
