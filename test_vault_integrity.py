"""
================================================================================
  UNIT & INTEGRATION TESTS FOR VAULT I/O, VALIDATORS & MEDIA PIPELINE
================================================================================
"""

import os
import json
import tempfile
import shutil
from pathlib import Path
from vault_io import write_json_atomic, read_manifest_safe
from validators import ValidationGate, APPROVED_CATEGORIES


def test_atomic_write_and_recovery():
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_file = Path(tmp_dir) / "manifest.json"
        data = {"version": "1.0", "products": {"B01IJNJAZ0": {"title": "Test Lumbar Cushion"}}}

        # 1. Test Atomic Write
        success = write_json_atomic(test_file, data, keep_backup=True)
        assert success, "write_json_atomic failed to return True"
        assert test_file.exists(), "Target file does not exist after write"

        read_data = json.loads(test_file.read_text(encoding="utf-8"))
        assert read_data["version"] == "1.0"

        # 2. Test Second Write creates .bak
        data["version"] = "2.0"
        write_json_atomic(test_file, data, keep_backup=True)
        bak_file = test_file.with_suffix(".json.bak")
        assert bak_file.exists(), ".bak file was not created"

        bak_data = json.loads(bak_file.read_text(encoding="utf-8"))
        assert bak_data["version"] == "1.0", "Backup does not contain previous state"

        # 3. Test Corruption Recovery
        test_file.write_text("{ broken json content ...", encoding="utf-8")
        recovered = read_manifest_safe(test_file)
        assert recovered["version"] == "1.0", "Failed to recover from .bak"

        print("[OK] test_atomic_write_and_recovery PASSED")


def test_validation_gate():
    # Valid product
    valid_p = {
        "asin": "B01IJNJAZ0",
        "site_category": "tech-accessories",
        "commercial": {
            "price": 34.99,
            "direct_amazon_url": "https://www.amazon.com/dp/B01IJNJAZ0?tag=prismeia-tw-20"
        },
        "metrics": {"rating": 4.5, "reviews_count": 46500},
        "field_status": {
            "price": {"source": "live", "verified_at": "2026-10-27T06:00:00Z"},
            "rating": {"source": "live", "verified_at": "2026-10-27T06:00:00Z"},
            "reviews_count": {"source": "live", "verified_at": "2026-10-27T06:00:00Z"}
        },
        "affiliate_url": "https://www.amazon.com/dp/B01IJNJAZ0?tag=prismeia-tw-20",
        "editorial": {
            "pros": ["Comfortable", "Ergonomic"],
            "cons": ["Slightly firm initially"],
            "evidence_source": "Amazon Customer Reviews"
        },
        "assets": {
            "images": ["images/hero_800x800.webp", "images/gallery_01.webp", "images/gallery_02.webp"]
        }
    }

    can_pub, blocks = ValidationGate.can_publish(valid_p)
    assert can_pub, f"Valid product was blocked: {blocks}"

    # Invalid ASIN
    bad_asin = dict(valid_p, asin="INVALID_ASIN_123")
    can_pub, blocks = ValidationGate.can_publish(bad_asin)
    assert not can_pub and any("ASIN" in b for b in blocks), "Failed to block invalid ASIN"

    # Default / Unverified Price Source
    bad_price_source = dict(valid_p)
    bad_price_source["field_status"] = dict(valid_p["field_status"])
    bad_price_source["field_status"]["price"] = {"source": "default"}
    can_pub, blocks = ValidationGate.can_publish(bad_price_source)
    assert not can_pub and any("price" in b for b in blocks), "Failed to block unverified default price source"

    # Fabricated cons without evidence
    unsupported_cons = dict(valid_p)
    unsupported_cons["editorial"] = {"pros": [], "cons": ["Fake con"], "evidence_source": ""}
    can_pub, blocks = ValidationGate.can_publish(unsupported_cons)
    assert not can_pub and any("cons" in b.lower() for b in blocks), "Failed to block unsubstantiated cons"

    print("[OK] test_validation_gate PASSED")


if __name__ == "__main__":
    test_atomic_write_and_recovery()
    test_validation_gate()
    print("\n>>> ALL TESTS COMPLETED SUCCESSFULLY <<<")
