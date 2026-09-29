from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from app.config import Settings, get_settings
from app.validation.schemas import CheckStatus, ComplianceCheck

log = logging.getLogger(__name__)

# PERSON is counted for the compliance check but not redacted: NER regularly tags product names
# ("Sapphire", "Amex Gold") as people.
REDACT_ENTITIES = {"EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE", "US_SSN", "US_BANK_NUMBER"}
DETECT_ENTITIES = REDACT_ENTITIES | {"PERSON"}
HIGH_RISK_ENTITIES = {"CREDIT_CARD", "IBAN_CODE", "US_SSN", "US_BANK_NUMBER"}

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?<![\w.%$€£₹])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-])\d{3,4}[\s.-]\d{3,4}(?![\w%])"
)
CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


@dataclass
class PiiFinding:
    entity_type: str
    start: int
    end: int
    score: float


def luhn_valid(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


class PiiDetector:
    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self._analyzer = None
        self._entities: list[str] = []
        if settings.presidio_enabled:
            self._analyzer = self._load_presidio(settings.presidio_spacy_model)
        self.engine = "presidio" if self._analyzer else "regex"

    def _load_presidio(self, model: str):
        try:
            import spacy
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider
        except ImportError:
            log.info("Presidio not installed; using regex PII detection")
            return None
        if not spacy.util.is_package(model):
            log.warning("spaCy model %s not installed (python -m spacy download %s); using regex PII detection",
                        model, model)
            return None
        try:
            provider = NlpEngineProvider(nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": model}],
            })
            analyzer = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])
            self._entities = sorted(DETECT_ENTITIES & set(analyzer.get_supported_entities("en")))
            return analyzer
        except Exception as exc:
            log.warning("Presidio failed to initialise (%s); using regex PII detection", exc)
            return None

    def analyze(self, text: str) -> list[PiiFinding]:
        if not text:
            return []
        if self._analyzer is None:
            return self._regex_findings(text)
        findings: list[PiiFinding] = []
        chunk_size = 100_000
        for offset in range(0, len(text), chunk_size):
            chunk = text[offset : offset + chunk_size]
            for result in self._analyzer.analyze(text=chunk, language="en", entities=self._entities,
                                                 score_threshold=0.5):
                if result.entity_type == "PERSON" and result.score < 0.85:
                    continue
                findings.append(PiiFinding(result.entity_type, result.start + offset, result.end + offset,
                                           result.score))
        return findings

    @staticmethod
    def _regex_findings(text: str) -> list[PiiFinding]:
        findings = [PiiFinding("EMAIL_ADDRESS", m.start(), m.end(), 1.0) for m in EMAIL_RE.finditer(text)]
        findings += [PiiFinding("US_SSN", m.start(), m.end(), 0.8) for m in SSN_RE.finditer(text)]
        findings += [PiiFinding("IBAN_CODE", m.start(), m.end(), 0.8) for m in IBAN_RE.finditer(text)]
        findings += [PiiFinding("CREDIT_CARD", m.start(), m.end(), 0.9)
                     for m in CARD_RE.finditer(text) if luhn_valid(m.group())]
        findings += [PiiFinding("PHONE_NUMBER", m.start(), m.end(), 0.6) for m in PHONE_RE.finditer(text)]
        return findings

    def redact(self, text: str) -> tuple[str, int]:
        findings = sorted(
            (f for f in self.analyze(text) if f.entity_type in REDACT_ENTITIES),
            key=lambda f: (f.start, -f.end),
        )
        kept: list[PiiFinding] = []
        for finding in findings:
            if kept and finding.start < kept[-1].end:
                continue  # overlaps a previous finding
            kept.append(finding)
        for finding in reversed(kept):
            text = text[: finding.start] + f"<{finding.entity_type}>" + text[finding.end :]
        return text, len(kept)


@lru_cache(maxsize=1)
def get_pii_detector() -> PiiDetector:
    return PiiDetector()


def check_pii(text: str, detector: PiiDetector, block_threshold: int) -> ComplianceCheck:
    counts = Counter(f.entity_type for f in detector.analyze(text))
    total = sum(counts.values())
    high_risk = sum(counts[e] for e in HIGH_RISK_ENTITIES)
    details = {"engine": detector.engine, "counts": dict(counts), "block_threshold": block_threshold}

    if high_risk >= 3 or total >= block_threshold:
        return ComplianceCheck(
            name="personal_data", status=CheckStatus.FAIL,
            summary=f"Page appears to contain personal data at scale ({total} item(s), {high_risk} high-risk)",
            details=details,
        )
    if total:
        return ComplianceCheck(
            name="personal_data", status=CheckStatus.WARN,
            summary=f"Found {total} potential personal-data item(s); identifiers are redacted before processing",
            details=details,
        )
    return ComplianceCheck(name="personal_data", status=CheckStatus.PASS,
                           summary="No personal data detected", details=details)
