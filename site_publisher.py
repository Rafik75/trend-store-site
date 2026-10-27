"""
================================================================================
  SITE PUBLISHER & CONTENT SYNC PIPELINE (Vault -> Astro Site -> GitHub)
  Transfers ONLY validated & approved products into the Cloudflare Pages Astro site.
================================================================================
"""

import os
import json
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from vault_io import write_json_atomic, read_manifest_safe, _VAULT_LOCK
from validators import ValidationGate, APPROVED_CATEGORIES

VAULT_DIR = Path(__file__).resolve().parent.parent / "PRODUCT_VAULT"
DEFAULT_SITE_DIR = Path(__file__).resolve().parent.parent / "TREND_STORE_SITE"


class SitePublisher:
    def __init__(self, vault_dir: Path = None, site_dir: Path = None):
        self.vault_dir = Path(vault_dir) if vault_dir else VAULT_DIR
        self.site_dir = Path(site_dir) if site_dir else DEFAULT_SITE_DIR
        self.site_content_dir = self.site_dir / "src" / "content" / "products"
        self.site_public_dir = self.site_dir / "public" / "products"

    def ensure_site_directories(self):
        self.site_content_dir.mkdir(parents=True, exist_ok=True)
        self.site_public_dir.mkdir(parents=True, exist_ok=True)

    def publish_approved_product(self, asin: str) -> Tuple[bool, List[str]]:
        """
        Validates product in vault and publishes to Astro site directory:
        1. Checks ValidationGate.can_publish
        2. Copies product.json to site content layer
        3. Copies webp images to site public directory
        4. Updates status in vault_manifest to 'published'
        """
        asin = asin.strip().upper()
        prod_dir = self.vault_dir / asin
        prod_json = prod_dir / "product.json"

        if not prod_json.exists():
            return False, [f"Product bundle does not exist at: {prod_dir}"]

        try:
            product = json.loads(prod_json.read_text(encoding="utf-8"))
        except Exception as e:
            return False, [f"Corrupt product.json: {e}"]

        # Check approval status
        status = product.get("status", "draft")
        if status not in ["approved", "published"]:
            return False, [f"Product status is '{status}'. Must be 'approved' by reviewer before publishing."]

        # 1. Validation Gate
        can_pub, blockers = ValidationGate.can_publish(product, prod_dir)
        if not can_pub:
            return False, blockers

        self.ensure_site_directories()

        # 2. Deploy Content Layer (JSON)
        dest_prod_dir = self.site_content_dir / asin.lower()
        dest_prod_dir.mkdir(parents=True, exist_ok=True)
        dest_json = dest_prod_dir / "product.json"

        # Mark as published
        product["status"] = "published"
        product["published_at"] = datetime.now(timezone.utc).isoformat()

        write_json_atomic(dest_json, product, keep_backup=False)

        # 3. Deploy Static Assets (WebP Images with clean wipe of stale files)
        dest_img_dir = self.site_public_dir / asin.lower()
        if dest_img_dir.exists():
            for old_img in dest_img_dir.glob("*.webp"):
                try:
                    old_img.unlink()
                except Exception:
                    pass
        dest_img_dir.mkdir(parents=True, exist_ok=True)

        src_img_dir = prod_dir / "images"
        if src_img_dir.exists():
            for img_file in src_img_dir.glob("*.webp"):
                shutil.copy2(img_file, dest_img_dir / img_file.name)

        # 4. Update Vault Manifest
        with _VAULT_LOCK:
            manifest_path = self.vault_dir / "vault_manifest.json"
            manifest = read_manifest_safe(manifest_path)
            if asin in manifest.get("products", {}):
                manifest["products"][asin]["status"] = "published"
                manifest["products"][asin]["published_at"] = product["published_at"]
                write_json_atomic(manifest_path, manifest, keep_backup=True)

        return True, []

    def git_commit_and_push(self, commit_message: str = "Auto-publish products to Cloudflare Pages") -> Tuple[bool, str]:
        """
        Pushes changes to GitHub repository to trigger Cloudflare Pages automatic build.
        """
        if not (self.site_dir / ".git").exists():
            return False, "Site directory is not a Git repository."

        try:
            # 1. git pull --rebase to sync remote changes before pushing
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=self.site_dir, capture_output=True, text=True)

            # 2. git add only content and public assets
            subprocess.run(["git", "add", "src/content/products", "public/products"], cwd=self.site_dir, check=True, capture_output=True)

            # 3. git commit
            res_commit = subprocess.run(
                ["git", "commit", "-m", commit_message],
                cwd=self.site_dir,
                capture_output=True,
                text=True
            )
            if "nothing to commit" in res_commit.stdout:
                return True, "No new changes to commit."

            # 4. git push
            res_push = subprocess.run(["git", "push", "origin", "main"], cwd=self.site_dir, capture_output=True, text=True)
            if res_push.returncode == 0:
                return True, "Successfully pushed to GitHub! Cloudflare Pages build triggered."
            else:
                return False, f"Git push failed: {res_push.stderr}"
        except Exception as e:
            return False, f"Git workflow error: {e}"


if __name__ == "__main__":
    publisher = SitePublisher()
    print("SitePublisher initialized.")
    print("Site content destination:", publisher.site_content_dir)
