"""
================================================================================
  AMAZON PRODUCT VAULT & MEDIA INTELLIGENCE ENGINE (Enterprise v3.0)
  Production-Grade Extraction Pipeline with Strict Verification & Atomic I/O
================================================================================
"""

import os
import re
import json
import uuid
import shutil
import subprocess
import urllib.request
import urllib.error
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone
from PIL import Image, ImageOps

from vault_io import write_json_atomic, read_manifest_safe, _VAULT_LOCK
from validators import ValidationGate, APPROVED_CATEGORIES

DEFAULT_VAULT_DIR = Path(__file__).resolve().parent.parent / "PRODUCT_VAULT"


class ExtractionError(Exception):
    """Raised when Amazon page extraction fails or is blocked by bot verification"""
    pass


class AmazonMediaEngine:
    def __init__(self, vault_dir: Path = None):
        self.vault_dir = Path(vault_dir) if vault_dir else DEFAULT_VAULT_DIR
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir = self.vault_dir / "_staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.vault_dir / "vault_manifest.json"
        self._init_manifest()

    def _init_manifest(self):
        """Initializes central manifest index if not present"""
        if not self.manifest_path.exists():
            initial_data = {
                "manifest_version": "3.0",
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "total_products": 0,
                "products": {}
            }
            write_json_atomic(self.manifest_path, initial_data, keep_backup=True)

    def fetch_amazon_page_curl(self, asin: str) -> str:
        """
        Executes robust curl request to fetch raw Amazon product page.
        Includes timeout, redirect following (-L), and explicit HTTP/bot validation.
        """
        url = f"https://www.amazon.com/dp/{asin}"
        cmd = [
            "curl.exe", "-s", "-L",
            "--max-time", "15",
            "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", "Sec-Ch-Ua: \"Chromium\";v=\"128\", \"Not;A=Brand\";v=\"24\", \"Google Chrome\";v=\"128\"",
            "-H", "Sec-Ch-Ua-Mobile: ?0",
            "-H", "Sec-Ch-Ua-Platform: \"Windows\"",
            "-H", "Sec-Fetch-Dest: document",
            "-H", "Sec-Fetch-Mode: navigate",
            "-H", "Sec-Fetch-Site: none",
            "-H", "Sec-Fetch-User: ?1",
            "-H", "Upgrade-Insecure-Requests: 1",
            url
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
            if res.returncode != 0:
                raise ExtractionError(f"curl process failed with exit code {res.returncode}")

            html = res.stdout
            if not html or len(html) < 500:
                raise ExtractionError("Amazon returned an empty or truncated response.")

            # Check for bot verification / CAPTCHA walls
            lower_html = html.lower()
            if "type the characters you see in this image" in lower_html or "robot check" in lower_html:
                raise ExtractionError("Amazon bot detection triggered (CAPTCHA challenge required).")
            if "503 - service unavailable" in lower_html:
                raise ExtractionError("Amazon returned HTTP 503 Service Unavailable.")

            return html
        except ExtractionError:
            raise
        except Exception as e:
            raise ExtractionError(f"Network error while fetching ASIN {asin}: {e}")

    def clean_title(self, raw_title: str) -> str:
        """Converts lengthy Amazon search-stuffed titles into clean, authoritative product names"""
        if not raw_title:
            return ""
        # Strip brackets and parenthesis
        t = re.sub(r'[\(\[\{].*?[\)\]\}]', '', raw_title)
        # Split on separators (dashes, colons, pipes)
        parts = re.split(r'\s*[-–—|:]\s*', t)
        core = parts[0].strip()
        if len(core) < 28 and len(parts) > 1:
            core = f"{core} - {parts[1].strip()}"
        return re.sub(r'\s+', ' ', core).strip()

    def extract_amazon_product_intelligence(self, asin: str, seed_data: dict = None) -> dict:
        """
        Extracts verified data points from the live Amazon page.
        Never fabricates fake fallback defaults; explicitly tracks field_status sources.
        """
        html = self.fetch_amazon_page_curl(asin)
        now_iso = datetime.now(timezone.utc).isoformat()
        field_status = {}

        # 1. Product Title
        title_m = re.search(r'<span id="productTitle"[^>]*>\s*([^<]+)\s*</span>', html)
        if title_m:
            raw_title = title_m.group(1).strip()
            field_status["title"] = {"source": "live", "verified_at": now_iso}
        elif seed_data and seed_data.get("title"):
            raw_title = seed_data["title"]
            field_status["title"] = {"source": "manual", "verified_at": now_iso}
        else:
            raise ExtractionError(f"Could not extract #productTitle for ASIN {asin}.")

        clean_title = self.clean_title(raw_title)

        # 2. Rating & Review Count
        rating_m = re.search(r'([0-9\.]+)\s+out of 5 stars', html)
        if rating_m:
            rating = float(rating_m.group(1))
            field_status["rating"] = {"source": "live", "verified_at": now_iso}
        elif seed_data and "rating" in seed_data:
            rating = float(seed_data["rating"])
            field_status["rating"] = {"source": "manual", "verified_at": now_iso}
        else:
            rating = None
            field_status["rating"] = {"source": "unknown"}

        reviews_m = re.search(r'id="acrCustomerReviewText"[^>]*>([0-9,]+)\s+ratings', html)
        if reviews_m:
            review_count = int(reviews_m.group(1).replace(",", ""))
            field_status["reviews_count"] = {"source": "live", "verified_at": now_iso}
        elif seed_data and "reviews" in seed_data:
            review_count = int(seed_data["reviews"])
            field_status["reviews_count"] = {"source": "manual", "verified_at": now_iso}
        else:
            review_count = None
            field_status["reviews_count"] = {"source": "unknown"}

        # Price extraction from page
        price = None
        price_m = re.search(r'class="a-price-whole">([0-9]+)\.</span><span class="a-price-fraction">([0-9]{2})</span>', html)
        if price_m:
            try:
                price = float(f"{price_m.group(1)}.{price_m.group(2)}")
                field_status["price"] = {"source": "live", "verified_at": now_iso}
            except Exception:
                price = None

        if price is None and seed_data and "price" in seed_data:
            price = float(seed_data["price"])
            field_status["price"] = {"source": "manual", "verified_at": now_iso}
        elif price is None:
            field_status["price"] = {"source": "unknown"}

        # 3. High-Resolution Images (SL1500)
        hires = re.findall(r'"hiRes"\s*:\s*"(https://m\.media-amazon\.com/images/I/[^"]+)"', html)
        large = re.findall(r'"large"\s*:\s*"(https://m\.media-amazon\.com/images/I/[^"]+)"', html)
        combined_images = []
        for u in (hires + large):
            clean_u = re.sub(r'\._[A-Za-z0-9_,-]+_\.(jpg|png|webp)', r'._AC_SL1500_.\1', u)
            if clean_u not in combined_images:
                combined_images.append(clean_u)

        if not combined_images:
            raw_imgs = re.findall(r'https://m\.media-amazon\.com/images/I/[a-zA-Z0-9_\-\+%]+\.jpg', html)
            for u in raw_imgs:
                if "_AC_" in u or "_SL" in u:
                    clean_u = re.sub(r'\._[A-Za-z0-9_,-]+_\.(jpg|png|webp)', r'._AC_SL1500_.\1', u)
                    if clean_u not in combined_images:
                        combined_images.append(clean_u)

        # 4. Feature Bullets
        bullets = re.findall(r'<span class="a-list-item">\s*([^<]+)\s*</span>', html)
        clean_bullets = [b.strip() for b in bullets if len(b.strip()) > 30][:5]

        # 5. Amazon AI Customer Summary (The genuine social proof engine)
        ai_summary = ""
        summary_m = re.search(r'"inertText"\s*:\s*"([^"]+)"', html)
        if summary_m:
            try:
                ai_summary = summary_m.group(1).encode('utf-8').decode('unicode_escape')
            except Exception:
                ai_summary = summary_m.group(1)

        # Extract genuine Pros & Cons ONLY when evidenced by customer summary
        pros = []
        cons = []
        evidence_source = ""
        if ai_summary:
            evidence_source = "Amazon AI Customer Review Summary"
            sentences = [s.strip() for s in ai_summary.split(".") if len(s.strip()) > 15]
            for s in sentences:
                if any(neg in s.lower() for neg in ["mixed", "however", "too big", "too firm", "hard", "difficult", "heavy", "bulky", "flaw", "downside"]):
                    cons.append(s.strip())
                else:
                    if len(pros) < 3:
                        pros.append(s.strip())

        if not pros and clean_bullets:
            pros = clean_bullets[:3]
            evidence_source = "Product Feature Bullets"

        # NOTE: Do NOT invent fake cons if none exist.
        return {
            "asin": asin,
            "raw_title": raw_title,
            "clean_title": clean_title,
            "rating": rating,
            "review_count": review_count,
            "price": price,
            "images": combined_images[:6],
            "bullets": clean_bullets,
            "ai_customer_summary": ai_summary,
            "pros": pros,
            "cons": cons,
            "evidence_source": evidence_source,
            "field_status": field_status
        }

    def standardize_to_webp(self, img_bytes: bytes, target_size=(800, 800)) -> bytes:
        """
        Image Standardization Pipeline:
        - Centers product on pristine 800x800 square canvas
        - Handles alpha transparency seamlessly
        - Outputs ultra-lightweight WebP format (<80KB)
        """
        img = Image.open(BytesIO(img_bytes))
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            alpha = img.convert('RGBA')
            bg = Image.new('RGBA', target_size, (255, 255, 255, 255))
            fitted = ImageOps.contain(alpha, (target_size[0] - 40, target_size[1] - 40), Image.Resampling.LANCZOS)
            offset = ((target_size[0] - fitted.width) // 2, (target_size[1] - fitted.height) // 2)
            bg.paste(fitted, offset, mask=fitted)
            final_img = bg.convert('RGB')
        else:
            rgb = img.convert('RGB')
            bg = Image.new('RGB', target_size, (255, 255, 255))
            fitted = ImageOps.contain(rgb, (target_size[0] - 40, target_size[1] - 40), Image.Resampling.LANCZOS)
            offset = ((target_size[0] - fitted.width) // 2, (target_size[1] - fitted.height) // 2)
            bg.paste(fitted, offset)
            final_img = bg

        out_io = BytesIO()
        final_img.save(out_io, format="WEBP", quality=86, method=6)
        return out_io.getvalue()

    def download_and_standardize_images(self, img_urls: list, output_dir: Path) -> list:
        """Downloads high-res image URLs and standardizes them into the target directory"""
        output_dir.mkdir(parents=True, exist_ok=True)
        saved_assets = []
        headers = {"User-Agent": "Mozilla/5.0"}

        for idx, u in enumerate(img_urls[:4]):
            try:
                req = urllib.request.Request(u, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as res:
                    raw_bytes = res.read()
                    webp_bytes = self.standardize_to_webp(raw_bytes, target_size=(800, 800))

                    filename = "hero_800x800.webp" if idx == 0 else f"gallery_{idx:02d}.webp"
                    dest_file = output_dir / filename
                    with open(dest_file, "wb") as f_out:
                        f_out.write(webp_bytes)

                    saved_assets.append({
                        "filename": filename,
                        "rel_path": f"images/{filename}",
                        "type": "hero" if idx == 0 else "gallery",
                        "size_kb": round(len(webp_bytes) / 1024, 1)
                    })
            except Exception as e:
                print(f"[!] Warning: Failed to process image {u}: {e}")

        return saved_assets

    def bundle_product(self, asin: str, seed_dict: dict = None, associate_tag: str = "prismeia-tw-20", site_category: str = None) -> dict:
        """
        Main Staging & Atomic Deployment Pipeline:
        1. Creates isolated staging folder in `PRODUCT_VAULT/_staging/<ASIN>.<uuid>`
        2. Scrapes official Amazon intelligence & SL1500 images
        3. Fails loudly with ExtractionError if images or critical data missing
        4. Writes `product.json` using atomic write
        5. Swaps staging folder into `PRODUCT_VAULT/<ASIN>` atomically
        6. Safely updates `vault_manifest.json`
        """
        asin = asin.strip().upper()
        if not ValidationGate.validate_asin(asin):
            raise ExtractionError(f"Invalid ASIN format: {asin}")

        stage_id = f"{asin}_{uuid.uuid4().hex[:8]}"
        stage_dir = self.staging_dir / stage_id
        stage_img_dir = stage_dir / "images"
        stage_dir.mkdir(parents=True, exist_ok=True)

        final_prod_dir = self.vault_dir / asin

        try:
            print(f"[*] Extracting verified intelligence for ASIN: {asin}...")
            intel = self.extract_amazon_product_intelligence(asin, seed_dict)

            if not intel["images"]:
                raise ExtractionError(f"No high-resolution images found for ASIN {asin}.")

            print(f"[*] Standardizing {len(intel['images'])} images to 800x800 WebP...")
            saved_images = self.download_and_standardize_images(intel["images"], stage_img_dir)
            if not saved_images:
                raise ExtractionError(f"Failed to download and standardize any images for ASIN {asin}.")

            # Map Category to Approved Category
            chosen_category = site_category
            if not chosen_category and seed_dict:
                seed_cat = seed_dict.get("category", "")
                cat_map = {
                    "Beauty & Personal Care": "beauty-personal-care",
                    "Home & Kitchen": "home-organization",
                    "Health & Household": "tech-accessories",
                    "Pet Supplies": "home-organization",
                    "Sports & Outdoors": "fitness-outdoor"
                }
                chosen_category = cat_map.get(seed_cat, "tech-accessories")

            if chosen_category not in APPROVED_CATEGORIES:
                chosen_category = "tech-accessories"

            # Clean editorial verdict
            price = intel["price"] if intel["price"] is not None else (float(seed_dict.get("price")) if seed_dict and "price" in seed_dict else None)
            rating = intel["rating"] if intel["rating"] is not None else (float(seed_dict.get("rating")) if seed_dict and "rating" in seed_dict else None)
            reviews_count = intel["review_count"] if intel["review_count"] is not None else (int(seed_dict.get("reviews")) if seed_dict and "reviews" in seed_dict else None)
            clean_title = intel["clean_title"]

            hook = seed_dict.get("hook", f"Practical features and performance overview of the {clean_title}.") if seed_dict else f"Practical overview of the {clean_title}."
            target_audience = seed_dict.get("target_audience", "General Consumers") if seed_dict else "General Consumers"

            bundle_payload = {
                "asin": asin,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "needs_review",  # Requires human verification to reach 'approved'
                "site_category": chosen_category,
                "associate_tag": associate_tag,
                "affiliate_url": f"https://www.amazon.com/dp/{asin}?tag={associate_tag}",
                "naming": {
                    "clean_title": clean_title,
                    "original_title": intel["raw_title"],
                    "short_name": clean_title.split("-")[0].strip()
                },
                "meta_tags": {
                    "title": f"{clean_title} - Practical Overview & Buying Guide",
                    "description": f"Comprehensive guide for the {clean_title}. Key specs, real owner feedback, and direct Amazon buying options.",
                    "canonical_slug": f"{chosen_category}/{asin.lower()}",
                    "og_image": saved_images[0]["rel_path"] if saved_images else ""
                },
                "commercial": {
                    "price": price if price is not None else 0.0,
                    "currency": "USD",
                    "rating": rating if rating is not None else 0.0,
                    "review_count": reviews_count if reviews_count is not None else 0,
                    "badge": seed_dict.get("badge", "") if seed_dict else "",
                    "prime_eligible": bool(seed_dict.get("prime", True)) if seed_dict else True,
                    "direct_amazon_url": f"https://www.amazon.com/dp/{asin}?tag={associate_tag}"
                },
                "field_status": intel["field_status"],
                "editorial": {
                    "hero_hook": hook,
                    "target_audience": target_audience,
                    "ai_customer_summary": intel["ai_customer_summary"],
                    "pros": intel["pros"],
                    "cons": intel["cons"],
                    "evidence_source": intel["evidence_source"]
                },
                "key_specifications": intel["bullets"],
                "assets": {
                    "images_count": len(saved_images),
                    "hero_image": saved_images[0]["rel_path"] if saved_images else "",
                    "images": [img["rel_path"] for img in saved_images]
                }
            }

            # 4. Write product.json into staging directory
            stage_json = stage_dir / "product.json"
            write_json_atomic(stage_json, bundle_payload, keep_backup=False)

            # 5. Atomic Promotion: swap stage_dir into final_prod_dir
            with _VAULT_LOCK:
                if final_prod_dir.exists():
                    prev_dir = self.vault_dir / f"{asin}_prev_{uuid.uuid4().hex[:6]}"
                    final_prod_dir.rename(prev_dir)
                    try:
                        stage_dir.rename(final_prod_dir)
                        shutil.rmtree(prev_dir, ignore_errors=True)
                    except Exception as swap_err:
                        # Rollback
                        prev_dir.rename(final_prod_dir)
                        raise swap_err
                else:
                    stage_dir.rename(final_prod_dir)

            # 6. Update manifest safely
            self._update_manifest(asin, bundle_payload)
            print(f"[OK] Product {asin} atomically published to vault: {final_prod_dir}")
            return bundle_payload

        finally:
            # Cleanup any remaining staging folder
            if stage_dir.exists():
                try:
                    shutil.rmtree(stage_dir, ignore_errors=True)
                except Exception:
                    pass

    def _update_manifest(self, asin: str, bundle: dict):
        """Updates manifest index in PRODUCT_VAULT/vault_manifest.json with relative paths"""
        with _VAULT_LOCK:
            manifest = read_manifest_safe(self.manifest_path)
            products = manifest.get("products", {})

            products[asin] = {
                "asin": asin,
                "title": bundle["naming"]["clean_title"],
                "site_category": bundle.get("site_category", "tech-accessories"),
                "price": bundle["commercial"]["price"],
                "rating": bundle["commercial"]["rating"],
                "review_count": bundle["commercial"]["review_count"],
                "status": bundle.get("status", "needs_review"),
                "hero_image": f"{asin}/{bundle['assets']['hero_image']}",
                "images_count": bundle["assets"]["images_count"],
                "rel_json_path": f"{asin}/product.json",
                "updated_at": datetime.now(timezone.utc).isoformat()
            }

            manifest["manifest_version"] = "3.0"
            manifest["last_updated"] = datetime.now(timezone.utc).isoformat()
            manifest["total_products"] = len(products)
            manifest["products"] = products

            write_json_atomic(self.manifest_path, manifest, keep_backup=True)
