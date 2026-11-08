"""GSTIN helpers: format check, checksum, state code.

GSTIN layout (15 chars): 2-digit state code, 10-char PAN, entity number,
the letter Z, and a mod-36 checksum character.
"""
import re

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b")

STATES = {
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "27": "Maharashtra",
    "29": "Karnataka",
    "33": "Tamil Nadu",
    "36": "Telangana",
}


def checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        factor = 1 if i % 2 == 0 else 2
        product = CHARS.index(ch) * factor
        total += product // 36 + product % 36
    return CHARS[(36 - total % 36) % 36]


def make_gstin(state: str, pan: str, entity: str = "1") -> str:
    base = f"{state}{pan}{entity}Z"
    return base + checksum_char(base)


def is_valid(gstin: str) -> bool:
    if not gstin or not GSTIN_RE.fullmatch(gstin):
        return False
    return checksum_char(gstin[:14]) == gstin[14]


def state_code(gstin: str) -> str:
    return (gstin or "")[:2]
