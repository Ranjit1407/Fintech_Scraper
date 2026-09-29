# Train a model: python -m app.ai.classification train labelled.csv model.joblib  (CSV columns: text, category)

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.validation.schemas import ProductCategory, coerce_category

log = logging.getLogger(__name__)

C = ProductCategory
# keyword -> weight; a trailing * matches any word ending (stems)
CATEGORY_KEYWORDS: dict[ProductCategory, dict[str, float]] = {
    C.CREDIT_CARD: {
        "credit card": 3, "cashback card": 3, "rewards card": 3, "travel card": 2, "balance transfer": 2,
        "purchase apr": 2, "cash advance": 2, "card apr": 2, "credit limit": 1.5, "annual fee": 1,
        "visa": 0.5, "mastercard": 1, "amex": 1, "american express": 1, "reward points": 1,
    },
    C.LOAN: {
        "loan": 2, "mortgage*": 2, "borrow*": 1.5, "lend*": 1, "refinanc*": 1.5, "emi": 1.5,
        "line of credit": 2, "heloc": 2, "car finance": 2, "auto finance": 2, "repayment": 1, "loan term": 1.5,
        "personal loan": 3, "home loan": 3, "student loan": 3, "payday": 2,
    },
    C.INSURANCE: {
        "insur*": 3, "premium": 0.5, "policy": 0.5, "coverage": 1.5, "cover": 0.5, "deductible": 1.5,
        "excess": 0.5, "claim": 0.5, "underwrit*": 1, "life cover": 2, "term plan": 1.5, "sum assured": 2,
        "policyholder": 1.5,
    },
    C.INVESTMENT: {
        "invest*": 2, "fund": 1, "etf": 2, "mutual fund": 3, "stock": 1, "shares": 1, "portfolio": 1.5,
        "brokerage": 2, "bond": 1, "sip": 1, "pension": 1.5, "retirement": 1, "ira": 1, "401(k)": 2,
        "wealth management": 2, "robo-advis*": 2, "expense ratio": 2, "dividend": 1, "nav": 0.5,
    },
    C.BANKING: {
        "savings account": 3, "current account": 3, "checking account": 3, "bank account": 3,
        "deposit": 1, "fixed deposit": 3, "term deposit": 3, "certificate of deposit": 3, "apy": 1.5,
        "aer": 1.5, "debit card": 2, "overdraft": 1, "isa": 1.5, "savings": 1, "interest-bearing": 1,
    },
    C.PAYMENT: {
        "payment": 1, "pay later": 2.5, "buy now pay later": 3, "bnpl": 3, "wallet": 2, "money transfer": 2.5,
        "remittance": 2.5, "upi": 2, "merchant": 1.5, "point of sale": 1.5, "checkout": 1.5,
        "payment gateway": 3, "prepaid card": 2, "send money": 2, "international transfer": 2,
    },
}


def _compile(keyword: str) -> re.Pattern[str]:
    if keyword.endswith("*"):
        return re.compile(r"(?<![a-z])" + re.escape(keyword[:-1]) + r"[a-z]*", re.IGNORECASE)
    return re.compile(r"(?<![a-z])" + re.escape(keyword) + r"s?(?![a-z])", re.IGNORECASE)


_PATTERNS = {
    category: [(_compile(kw), weight) for kw, weight in keywords.items()]
    for category, keywords in CATEGORY_KEYWORDS.items()
}


@dataclass
class ClassificationResult:
    category: ProductCategory
    confidence: float
    method: str
    scores: dict[str, float] = field(default_factory=dict)


def product_text(product: dict[str, Any]) -> tuple[str, str]:
    """(name, everything else) — the name is weighted more heavily."""
    rest: list[str] = [str(product.get("description") or "")]
    for key in ("features", "eligibility", "conditions"):
        rest.extend(str(v) for v in product.get(key) or [])
    for key in ("rates", "fees"):
        rest.extend(str(v.get("label", "")) for v in product.get(key) or [] if isinstance(v, dict))
    attributes = product.get("attributes") or {}
    if isinstance(attributes, dict):
        rest.extend(f"{k} {v}" for k, v in attributes.items())
    return str(product.get("name") or ""), " ".join(rest)


def keyword_scores(name: str, text: str) -> dict[ProductCategory, float]:
    scores: dict[ProductCategory, float] = {}
    for category, patterns in _PATTERNS.items():
        score = 0.0
        for pattern, weight in patterns:
            score += 3 * weight * min(len(pattern.findall(name)), 1)
            score += weight * min(len(pattern.findall(text)), 3)
        if score:
            scores[category] = score
    return scores


def classify_by_keywords(name: str, text: str) -> ClassificationResult:
    scores = keyword_scores(name, text)
    if not scores:
        return ClassificationResult(ProductCategory.OTHER, 0.0, "keywords")
    best, top = max(scores.items(), key=lambda item: item[1])
    total = sum(scores.values())
    confidence = (top / total) * min(1.0, top / 4)
    return ClassificationResult(best, round(confidence, 3), "keywords", {c.value: s for c, s in scores.items()})


class ProductClassifier:
    def __init__(self, model_path: str | None = None):
        self._model = None
        if model_path and Path(model_path).is_file():
            try:
                import joblib

                self._model = joblib.load(model_path)
                log.info("Loaded product classifier model from %s", model_path)
            except Exception as exc:
                log.warning("Could not load classifier model %s: %s", model_path, exc)

    def _model_prediction(self, name: str, text: str) -> ClassificationResult | None:
        if self._model is None:
            return None
        probabilities = self._model.predict_proba([f"{name} {name} {text}"])[0]
        index = int(probabilities.argmax())
        category = coerce_category(self._model.classes_[index]) or ProductCategory.OTHER
        return ClassificationResult(category, round(float(probabilities[index]), 3), "sklearn")

    def classify(self, product: dict[str, Any]) -> ClassificationResult:
        name, text = product_text(product)
        candidate = classify_by_keywords(name, text)
        model_result = self._model_prediction(name, text)
        if model_result and (model_result.confidence >= candidate.confidence or candidate.category == C.OTHER):
            candidate = model_result

        prior = coerce_category(product.get("category"))
        if prior and prior != C.OTHER:
            source = product.get("extraction_method") or "extractor"
            try:
                prior_confidence = float(product.get("category_confidence") or 0.7)
            except (TypeError, ValueError):
                prior_confidence = 0.7
            prior_confidence = min(max(prior_confidence, 0.0), 1.0)
            if candidate.category == prior:
                return ClassificationResult(prior, round(max(prior_confidence, candidate.confidence), 3),
                                            f"{source}+{candidate.method}", candidate.scores)
            return ClassificationResult(prior, round(prior_confidence * 0.85, 3), source, candidate.scores)
        return candidate


@lru_cache(maxsize=4)
def get_classifier(model_path: str | None = None) -> ProductClassifier:
    return ProductClassifier(model_path)


def train_classifier(csv_path: str, model_path: str) -> dict[str, Any]:
    """Train a TF-IDF + logistic regression classifier from a labelled CSV (columns: text, category)."""
    import joblib
    import pandas as pd
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import cross_val_score
    from sklearn.pipeline import make_pipeline

    df = pd.read_csv(csv_path).dropna(subset=["text", "category"])
    df["category"] = df["category"].map(lambda c: (coerce_category(c) or C.OTHER).value)
    pipeline = make_pipeline(
        TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True),
        LogisticRegression(max_iter=1000, class_weight="balanced"),
    )
    smallest_class = int(df["category"].value_counts().min())
    cv_accuracy = None
    if df["category"].nunique() > 1 and smallest_class >= 2:
        cv_accuracy = float(cross_val_score(pipeline, df["text"], df["category"], cv=min(5, smallest_class)).mean())
    pipeline.fit(df["text"], df["category"])
    joblib.dump(pipeline, model_path)
    return {"samples": len(df), "classes": sorted(df["category"].unique()), "cv_accuracy": cv_accuracy,
            "model_path": model_path}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Product classifier utilities")
    sub = parser.add_subparsers(dest="command", required=True)
    train = sub.add_parser("train", help="train a scikit-learn model from a labelled CSV")
    train.add_argument("csv_path")
    train.add_argument("model_path")
    args = parser.parse_args()
    print(json.dumps(train_classifier(args.csv_path, args.model_path), indent=2))
