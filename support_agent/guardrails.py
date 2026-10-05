"""Guardrails pe intrare, pe acțiuni și pe ieșire.

Regulile de business stau AICI, în cod, nu în prompt: modelul poate propune o acțiune,
dar codul decide dacă e permisă. Promptul doar explică regulile.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import config
from .rag import normalize

# --- Intrare -------------------------------------------------------------------

INJECTION_PATTERNS = [
    r"ignor\w*\s+(toate\s+)?(instructiunil|regulil|indicatiil)",
    r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules)",
    r"(system|developer)\s*prompt",
    r"promptul\s+(de\s+)?sistem",
    r"(you are now|acum esti|de acum esti)\s",
    r"(developer|admin|god)\s*mode",
    r"(mod|modul)\s+(admin|dezvoltator)",
    r"(sunt|i am)\s+(un\s+)?(operator|admin|administrator)",
    r"aproba\w*\s+(singur|automat|direct|imediat)",
    r"(fara|without)\s+(aprobare|approval)",
]

HUMAN_REQUEST_PATTERNS = [
    r"\b(vreau|doresc|da-mi|dati-mi|cer)\b.{0,25}\b(om|operator|persoana|manager|agent uman|consultant)\b",
    r"\b(om real|operator uman|persoana reala)\b",
    r"\b(talk to|speak to)\s+(a\s+)?(human|person|agent)\b",
    r"\b(avocat|protectia consumatorului|reclamatie oficiala)\b",
]

CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"(?<!\w)(?:\+373|0)\s?\d{2}[\s-]?\d{3}[\s-]?\d{3}\b")


def detect_injection(text: str) -> str | None:
    t = normalize(text)
    for p in INJECTION_PATTERNS:
        if re.search(p, t):
            return p
    return None


def wants_human(text: str) -> bool:
    t = normalize(text)
    return any(re.search(p, t) for p in HUMAN_REQUEST_PATTERNS)


def contains_card_number(text: str) -> bool:
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def _luhn(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def redact_pii(text: str) -> str:
    text = CARD_RE.sub(lambda m: "[CARD]" if _luhn(re.sub(r"\D", "", m.group())) else m.group(), text)
    text = EMAIL_RE.sub("[EMAIL]", text)
    return PHONE_RE.sub("[TELEFON]", text)


# --- Acțiuni -------------------------------------------------------------------

@dataclass
class Decision:
    allowed: bool
    reason: str
    code: str = "ok"


def check_refund(order: dict | None, amount: float, pending_or_approved: float,
                 session_refund_requests: int, today: datetime) -> Decision:
    """Politica de rambursare, aplicată determinist înainte să ajungă la operator."""
    if order is None:
        return Decision(False, "Comanda nu există sau nu aparține acestui client.", "not_found")
    if session_refund_requests >= config.MAX_REFUND_REQUESTS_PER_SESSION:
        return Decision(False, "S-a atins limita de cereri de rambursare pentru această conversație.", "rate_limited")
    if order["status"] != "livrată":
        return Decision(False, f"Comanda are statusul „{order['status']}”; doar comenzile livrate pot fi rambursate"
                               " (comenzile în procesare se pot anula).", "wrong_status")
    delivered = datetime.fromisoformat(order["delivered_at"])
    if today - delivered > timedelta(days=config.REFUND_WINDOW_DAYS):
        return Decision(False, f"Au trecut peste {config.REFUND_WINDOW_DAYS} de zile de la livrare.", "window_expired")
    items = json.loads(order["items"]) if isinstance(order["items"], str) else order["items"]
    returnable_value = sum(i["price"] * i["qty"] for i in items if i.get("returnable", True))
    if returnable_value <= 0:
        return Decision(False, "Produsele din comandă nu pot fi returnate (ex. card cadou).", "not_returnable")
    if amount <= 0:
        return Decision(False, "Suma trebuie să fie pozitivă.", "invalid_amount")
    remaining = returnable_value - order["refunded"] - pending_or_approved
    if remaining <= 0:
        return Decision(False, "Există deja o rambursare în curs sau finalizată pentru această comandă.", "duplicate")
    if amount > remaining + 0.001:
        return Decision(False, f"Suma cerută depășește suma rambursabilă rămasă ({remaining:.2f} MDL).", "amount_too_high")
    return Decision(True, "Cererea respectă politica; necesită aprobarea unui operator.")


# --- Ieșire --------------------------------------------------------------------

CITATION_RE = re.compile(r"\[([a-z0-9-]+#[a-z0-9-]+)\]")
FALSE_PROMISE_RE = re.compile(
    r"(rambursarea|banii|refund\w*)(\s+\w+){0,3}\s+(a|au)\s+fost\s+(aprobat|procesat|virat|returnat|trimis)"
    r"|\b(am|s-a)\s+aprobat\s+(rambursarea|cererea|refund)"
)


def check_output(text: str, retrieved_ids: set[str], approved_refund_in_turn: bool) -> tuple[str, list[str], list[str]]:
    """Returnează (text curățat, citări valide, avertismente).

    - citările către surse care NU au fost recuperate în această tură sunt eliminate
      (previne citări inventate);
    - promisiunile că o rambursare „a fost aprobată” sunt blocate dacă nu există o aprobare reală.
    """
    warnings = []
    valid = []
    for sid in CITATION_RE.findall(text):
        if sid in retrieved_ids:
            if sid not in valid:
                valid.append(sid)
        else:
            warnings.append(f"citare_inventata:{sid}")
            text = text.replace(f"[{sid}]", "")
    if not approved_refund_in_turn and FALSE_PROMISE_RE.search(normalize(text)):
        warnings.append("promisiune_falsa_rambursare")
        text = ("Cererea de rambursare este în așteptarea aprobării unui operator uman. "
                "Veți primi un mesaj aici imediat ce este luată o decizie.")
    return text.strip(), valid, warnings
