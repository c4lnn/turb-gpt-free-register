# -*- coding: utf-8 -*-
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core import masi_cdk_pool as pool


class MasiCdkPoolTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(pool, "_POOL_PATH", Path(self.tempdir.name) / "pool.json")
        self.path_patch.start()
        pool.reset_runtime_leases()

    def tearDown(self):
        pool.reset_runtime_leases()
        self.path_patch.stop()
        self.tempdir.cleanup()

    def test_import_deduplicates_across_both_pools_and_masks_secret(self):
        result = pool.import_cdks("CDK-AAAA-1111\n\nCDK-BBBB-2222\nCDK-AAAA-1111")
        self.assertEqual(result["added_count"], 2)
        rows = pool.list_cdks()
        self.assertEqual(len(rows), 2)
        self.assertNotIn("cdk", rows[0])
        self.assertIn("****", rows[0]["masked_cdk"])

        pool.update_quota(rows[0]["id"], {"total_uses": 1, "remaining_uses": 0, "pending_uses": 0, "available_uses": 0})
        duplicate = pool.import_cdks("CDK-AAAA-1111")
        self.assertEqual(duplicate["duplicate_count"], 1)
        self.assertEqual(len(pool.list_cdks()), 2)

    def test_legacy_rows_default_to_enabled_and_new_rows_persist_enabled(self):
        legacy = {
            "version": 1,
            "items": [{
                "id": "legacy-id", "provider": "masi", "cdk": "CDK-LEGACY",
                "fingerprint": pool.fingerprint("CDK-LEGACY"), "pool": "selectable",
            }],
        }
        pool._POOL_PATH.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertTrue(pool.list_cdks()[0]["enabled"])
        imported = pool.import_cdks("CDK-NEW")["added"][0]
        self.assertTrue(imported["enabled"])
        stored = json.loads(pool._POOL_PATH.read_text(encoding="utf-8"))["items"]
        new_row = next(row for row in stored if row["id"] == imported["id"])
        self.assertIs(new_row["enabled"], True)

    def test_enablement_is_idempotent_and_reports_missing_ids(self):
        imported = pool.import_cdks("CDK-A\nCDK-B")
        first_id, second_id = [row["id"] for row in imported["added"]]
        changed = pool.set_enablement(enabled=False, ids=[first_id, first_id, "missing"])
        self.assertEqual(changed, {
            "enabled": False,
            "matched_count": 1,
            "changed_count": 1,
            "unchanged_count": 0,
            "not_found_ids": ["missing"],
        })
        unchanged = pool.set_enablement(enabled=False, ids=[first_id])
        self.assertEqual((unchanged["changed_count"], unchanged["unchanged_count"]), (0, 1))
        by_id = {row["id"]: row for row in pool.list_cdks()}
        self.assertFalse(by_id[first_id]["enabled"])
        self.assertTrue(by_id[second_id]["enabled"])

    def test_pool_enablement_and_summary_only_count_enabled_availability(self):
        imported = pool.import_cdks("CDK-A\nCDK-B")
        for row in imported["added"]:
            pool.update_quota(row["id"], {"total_uses": 10, "remaining_uses": 4, "pending_uses": 1, "available_uses": 3})
        pool.set_enablement(enabled=False, ids=[imported["added"][0]["id"]])
        summary = pool.pool_summary()
        self.assertEqual(summary["enabled_selectable_count"], 1)
        self.assertEqual(summary["disabled_selectable_count"], 1)
        self.assertEqual(summary["total_available_uses"], 6)
        self.assertEqual(summary["enabled_available_uses"], 3)
        result = pool.set_enablement(enabled=False, pool=pool.POOL_SELECTABLE)
        self.assertEqual((result["matched_count"], result["changed_count"], result["unchanged_count"]), (2, 1, 1))

    def test_duplicate_import_and_quota_moves_preserve_disabled_state(self):
        cdk_id = pool.import_cdks("CDK-A")["added"][0]["id"]
        pool.set_enablement(enabled=False, ids=[cdk_id])
        duplicate = pool.import_cdks("CDK-A")["duplicates"][0]
        self.assertFalse(duplicate["enabled"])
        pool.update_quota(cdk_id, {"total_uses": 10, "remaining_uses": 0, "pending_uses": 0, "available_uses": 0})
        self.assertFalse(pool.list_cdks(pool=pool.POOL_EXHAUSTED)[0]["enabled"])
        pool.update_quota(cdk_id, {"total_uses": 10, "remaining_uses": 2, "pending_uses": 0, "available_uses": 2})
        self.assertFalse(pool.list_cdks(pool=pool.POOL_SELECTABLE)[0]["enabled"])

    def test_lease_skips_disabled_records_but_secret_remains_readable(self):
        imported = pool.import_cdks("CDK-A\nCDK-B")
        first_id = imported["added"][0]["id"]
        pool.set_enablement(enabled=False, ids=[first_id])
        lease = pool.lease_next()
        self.assertEqual(lease["id"], imported["added"][1]["id"])
        self.assertEqual(pool.get_secret(first_id)["cdk"], "CDK-A")
        pool.release_lease(lease["id"])

    def test_only_remaining_zero_moves_to_exhausted(self):
        cdk_id = pool.import_cdks("CDK-A")["added"][0]["id"]
        busy = pool.update_quota(cdk_id, {"total_uses": 10, "remaining_uses": 8, "pending_uses": 8, "available_uses": 0})
        self.assertEqual(busy["pool"], pool.POOL_SELECTABLE)
        exhausted = pool.update_quota(cdk_id, {"total_uses": 10, "remaining_uses": 0, "pending_uses": 0, "available_uses": 0})
        self.assertEqual(exhausted["pool"], pool.POOL_EXHAUSTED)
        restored = pool.update_quota(cdk_id, {"total_uses": 20, "remaining_uses": 5, "pending_uses": 0, "available_uses": 5})
        self.assertEqual(restored["pool"], pool.POOL_SELECTABLE)

    def test_positions_stay_stable_for_available_and_busy_moves_to_tail(self):
        imported = pool.import_cdks("CDK-A\nCDK-B\nCDK-C")
        first_id = imported["added"][0]["id"]
        pool.update_quota(first_id, {"total_uses": 10, "remaining_uses": 8, "pending_uses": 1, "available_uses": 7})
        self.assertEqual([(row["id"], row["position"]) for row in pool.list_cdks(pool=pool.POOL_SELECTABLE)], [
            (imported["added"][0]["id"], 1),
            (imported["added"][1]["id"], 2),
            (imported["added"][2]["id"], 3),
        ])

        pool.update_quota(first_id, {"total_uses": 10, "remaining_uses": 8, "pending_uses": 8, "available_uses": 0})
        self.assertEqual([row["id"] for row in pool.list_cdks(pool=pool.POOL_SELECTABLE)], [
            imported["added"][1]["id"], imported["added"][2]["id"], first_id,
        ])
        self.assertEqual([row["position"] for row in pool.list_cdks(pool=pool.POOL_SELECTABLE)], [1, 2, 3])

    def test_cross_pool_move_appends_to_target_pool_tail(self):
        imported = pool.import_cdks("CDK-A\nCDK-B\nCDK-C")
        second_id = imported["added"][1]["id"]
        pool.update_quota(second_id, {"total_uses": 1, "remaining_uses": 0, "pending_uses": 0, "available_uses": 0})
        self.assertEqual(pool.list_cdks(pool=pool.POOL_EXHAUSTED)[0]["position"], 1)
        pool.update_quota(second_id, {"total_uses": 10, "remaining_uses": 8, "pending_uses": 1, "available_uses": 7})
        selectable = pool.list_cdks(pool=pool.POOL_SELECTABLE)
        self.assertEqual([row["id"] for row in selectable], [imported["added"][0]["id"], imported["added"][2]["id"], second_id])
        self.assertEqual([row["position"] for row in selectable], [1, 2, 3])

    def test_query_error_preserves_pool_and_previous_quota(self):
        cdk_id = pool.import_cdks("CDK-A")["added"][0]["id"]
        pool.update_quota(cdk_id, {"total_uses": 10, "remaining_uses": 4, "pending_uses": 1, "available_uses": 3})
        row = pool.record_query_error(cdk_id, "network failed")
        self.assertEqual(row["pool"], pool.POOL_SELECTABLE)
        self.assertEqual(row["remaining_uses"], 4)
        self.assertEqual(row["last_error"], "network failed")

    def test_lease_moves_released_item_to_tail(self):
        pool.import_cdks("CDK-A\nCDK-B")
        first = pool.lease_next()
        pool.release_lease(first["id"])
        second = pool.lease_next()
        self.assertNotEqual(first["id"], second["id"])
        pool.release_lease(second["id"])

    def test_leased_cdk_cannot_be_deleted(self):
        cdk_id = pool.import_cdks("CDK-A")["added"][0]["id"]
        pool.lease_by_id(cdk_id)
        with self.assertRaises(pool.CdkLeaseBusy):
            pool.delete_cdk(cdk_id)
        pool.release_lease(cdk_id)

    def test_only_one_thread_can_lease_single_cdk(self):
        pool.import_cdks("CDK-A")
        barrier = threading.Barrier(3)
        leases = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            value = pool.lease_next()
            with lock:
                leases.append(value)
            barrier.wait()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(1 for value in leases if value), 1)
        leased = next(value for value in leases if value)
        pool.release_lease(leased["id"])

    def test_different_cdks_can_be_leased_concurrently(self):
        pool.import_cdks("CDK-A\nCDK-B")
        first = pool.lease_next()
        second = pool.lease_next()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["id"], second["id"])
        pool.release_lease(first["id"])
        pool.release_lease(second["id"])


if __name__ == "__main__":
    unittest.main()
