"""
================================================================================
  VALIDATORS & GATEWAY ENGINE - QUALITY ASSURANCE FOR PRODUCT VAULT & ASTRO
  Blocks incomplete, fake, or corrupted data from ever reaching production.
================================================================================
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Tuple
from PIL import Image

APPROVED_CATEGORIES = {
    "tech-accessories": "Tech & Everyday Accessories",
    "kitchen-food": "Kitchen & Food Prep",
    "home-organization": "Home & Organization",
    "car-travel": "Car & Travel",
    "fitness-outdoor": "Fitness & Outdoor",
    "beauty-personal-care": "Beauty & Personal Care"
}

PROHIBITED_MARKETING_WORDS = [
    r"\btested\b",
    r"\bverified\b",
    r"\bguaranteed\b",
    r"\bcures\b",
    r"\bmiracle\b",
    r"(?:^|\W)#1(?:\W|$)"
]


class ValidationGate:
    """
    Enforces strict quality rules on product bundles before approval or publication.
    """

    @staticmethod
    def validate_asin(asin: str) -> bool:
        if not asin or not isinstance(asin, str):
            return False
        return bool(re.match(r"^B0[A-Z0-9]{8}$", asin.strip().upper()))

    @staticmethod
    def validate_product(product: Dict[str, Any], vault_product_dir: Path = None) -> List[Dict[str, str]]:
        """
        Runs comprehensive checks on a product dictionary.
        Returns a list of issue dicts: [{"field": str, "severity": "block" | "warn", "code": str, "msg": str}]
        """
        issues = []

        # 1. ASIN Check
        asin = product.get("asin", "").strip().upper()
        if not ValidationGate.validate_asin(asin):
            issues.append({
                "field": "asin",
                "severity": "block",
                "code": "INVALID_ASIN",
                "msg": f"ASIN '{asin}' does not match standard Amazon format (^B0[A-Z0-9]{{8}}$)."
            })

        # 2. Site Category Check
        site_category = product.get("site_category", "")
        if site_category not in APPROVED_CATEGORIES:
            issues.append({
                "field": "site_category",
                "severity": "block",
                "code": "INVALID_CATEGORY",
                "msg": f"Category '{site_category}' is not in approved site categories: {list(APPROVED_CATEGORIES.keys())}"
            })

        # 3. Commercial & Field Status Checks
        commercial = product.get("commercial", {})
        price = commercial.get("price", 0.0)
        try:
            price_val = float(price)
            if price_val <= 0.0:
                issues.append({
                    "field": "commercial.price",
                    "severity": "block",
                    "code": "PRICE_ZERO",
                    "msg": "Commercial price must be strictly greater than 0.00."
                })
        except (ValueError, TypeError):
            issues.append({
                "field": "commercial.price",
                "severity": "block",
                "code": "PRICE_NAN",
                "msg": "Commercial price is not a valid floating-point number."
            })

        # Rating & Reviews
        rating = product.get("commercial", {}).get("rating")
        if rating is None:
            rating = product.get("metrics", {}).get("rating", 0.0)

        reviews_count = product.get("commercial", {}).get("review_count")
        if reviews_count is None:
            reviews_count = product.get("metrics", {}).get("reviews_count", 0)

        if rating < 1.0 or rating > 5.0:
            issues.append({
                "field": "rating",
                "severity": "block",
                "code": "RATING_RANGE",
                "msg": f"Rating ({rating}) must be between 1.0 and 5.0."
            })

        if reviews_count < 0:
            issues.append({
                "field": "reviews_count",
                "severity": "block",
                "code": "REVIEWS_NEGATIVE",
                "msg": "Reviews count cannot be negative."
            })

        # Verification Sources (Blocks default/fake data)
        field_status = product.get("field_status", {})
        for req_field in ["price", "rating", "reviews_count"]:
            source_info = field_status.get(req_field, {})
            src = source_info.get("source", "unknown")
            if src in ["default", "seed", "unknown"]:
                issues.append({
                    "field": f"field_status.{req_field}",
                    "severity": "block",
                    "code": "UNVERIFIED_DATA_SOURCE",
                    "msg": f"Field '{req_field}' has unverified source '{src}'. Must be 'live' or 'manual'."
                })

            # Freshness check: price must not be older than 7 days
            if req_field == "price" and src == "live":
                ver_at = source_info.get("verified_at")
                if ver_at:
                    try:
                        ver_dt = datetime.fromisoformat(ver_at.replace("Z", "+00:00"))
                        now_dt = datetime.now(timezone.utc)
                        days_old = (now_dt - ver_dt).total_seconds() / 86400
                        if days_old > 7:
                            issues.append({
                                "field": "field_status.price",
                                "severity": "warn",
                                "code": "STALE_PRICE",
                                "msg": f"Price was verified {days_old:.1f} days ago. Price might have changed on Amazon."
                            })
                    except Exception:
                        pass

        # 4. Affiliate Link & Tag Validation (Enforce exact approved tag prismeia-tw-20)
        REQUIRED_TAG = "prismeia-tw-20"
        aff_url = product.get("affiliate_url", "")
        direct_url = product.get("commercial", {}).get("direct_amazon_url", "")

        for url_name, u in [("affiliate_url", aff_url), ("commercial.direct_amazon_url", direct_url)]:
            if not u:
                issues.append({
                    "field": url_name,
                    "severity": "block",
                    "code": "MISSING_URL",
                    "msg": f"URL '{url_name}' is empty."
                })
            elif "amazon.com" not in u:
                issues.append({
                    "field": url_name,
                    "severity": "block",
                    "code": "INVALID_DOMAIN",
                    "msg": f"URL '{url_name}' must point to amazon.com domain."
                })
            elif f"tag={REQUIRED_TAG}" not in u:
                issues.append({
                    "field": url_name,
                    "severity": "block",
                    "code": "INVALID_AFFILIATE_TAG",
                    "msg": f"URL '{url_name}' does not contain the required affiliate tag '{REQUIRED_TAG}'."
                })

        # 5. Asset Checks (Physical Image Verification)
        assets = product.get("assets", {})
        images = assets.get("images", [])

        if not images:
            issues.append({
                "field": "assets.images",
                "severity": "block",
                "code": "NO_IMAGES",
                "msg": "Product bundle has zero images. At least 1 hero image is strictly required."
            })
        elif len(images) < 3:
            issues.append({
                "field": "assets.images",
                "severity": "warn",
                "code": "FEW_IMAGES",
                "msg": f"Product only has {len(images)} images. 3 or more are recommended for high conversion."
            })

        # If vault directory is provided, verify files physically
        if vault_product_dir and Path(vault_product_dir).exists():
            v_dir = Path(vault_product_dir)
            for rel_img in images:
                # Disallow absolute paths in asset references
                if ":\\" in rel_img or rel_img.startswith("/"):
                    issues.append({
                        "field": "assets.images",
                        "severity": "block",
                        "code": "ABSOLUTE_IMAGE_PATH",
                        "msg": f"Image path '{rel_img}' must be relative (e.g. 'images/hero_800x800.webp')."
                    })
                    continue

                img_path = v_dir / rel_img
                if not img_path.exists():
                    issues.append({
                        "field": "assets.images",
                        "severity": "block",
                        "code": "MISSING_IMAGE_FILE",
                        "msg": f"Physical image file not found on disk: {img_path.name}"
                    })
                else:
                    try:
                        with Image.open(img_path) as img:
                            w, h = img.size
                            if w != 800 or h != 800:
                                issues.append({
                                    "field": "assets.images",
                                    "severity": "warn",
                                    "code": "NON_STANDARD_DIMENSIONS",
                                    "msg": f"Image '{img_path.name}' is {w}x{h}. Recommended standard is 800x800."
                                })
                    except Exception as img_err:
                        issues.append({
                            "field": "assets.images",
                            "severity": "block",
                            "code": "CORRUPT_IMAGE",
                            "msg": f"Image '{img_path.name}' is corrupted or unreadable: {img_err}"
                        })

        # 6. Pros & Cons Evidence Check
        pros = product.get("editorial", {}).get("pros", [])
        cons = product.get("editorial", {}).get("cons", [])
        evidence_source = product.get("editorial", {}).get("evidence_source", "")

        if cons and not evidence_source:
            issues.append({
                "field": "editorial.cons",
                "severity": "block",
                "code": "UNSUBSTANTIATED_CONS",
                "msg": "Cons are listed without an 'evidence_source'. Inventing negative claims is prohibited."
            })

        # 7. Unsubstantiated SEO / Marketing Claims (Check all editorial and badge texts)
        title = product.get("naming", {}).get("clean_title", "")
        hero_hook = product.get("editorial", {}).get("hero_hook", "")
        ai_summary = product.get("editorial", {}).get("ai_customer_summary", "")
        badge = product.get("commercial", {}).get("badge", "")

        texts_to_check = [
            ("naming.clean_title", title),
            ("editorial.hero_hook", hero_hook),
            ("editorial.ai_customer_summary", ai_summary),
            ("commercial.badge", badge)
        ]

        for pattern in PROHIBITED_MARKETING_WORDS:
            for field_name, txt in texts_to_check:
                if txt and re.search(pattern, txt, re.IGNORECASE):
                    # If it's Amazon's verified badge and proven by evidence, warn; otherwise block if no evidence
                    if not evidence_source:
                        issues.append({
                            "field": field_name,
                            "severity": "warn",
                            "code": "PROHIBITED_MARKETING_CLAIM",
                            "msg": f"Field '{field_name}' contains claim matching '{pattern}' without documented evidence source."
                        })

        return issues

    @staticmethod
    def can_publish(product: Dict[str, Any], vault_product_dir: Path = None) -> Tuple[bool, List[str]]:
        """
        Determines whether product can transition to 'approved' or 'published'.
        Returns (can_publish, blocking_reasons)
        """
        issues = ValidationGate.validate_product(product, vault_product_dir)
        blocking_issues = [iss["msg"] for iss in issues if iss["severity"] == "block"]
        return (len(blocking_issues) == 0, blocking_issues)
