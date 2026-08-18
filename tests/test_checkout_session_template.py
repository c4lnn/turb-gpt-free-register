# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class CheckoutSessionTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            Path(__file__).parents[1] / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")

    def test_checkout_column_is_immediately_after_plan_and_has_stable_width(self):
        table_start = self.template.index('<table class="accounts-table-v2">')
        table_end = self.template.index("</table>", table_start)
        table = self.template[table_start:table_end]
        self.assertLess(table.index('<th class="col-plan">套餐</th>'), table.index('<th class="col-checkout">'))
        self.assertIn('<col class="col-token"><col class="col-plan"><col class="col-checkout">', table)
        self.assertIn(".accounts-table-v2 .col-checkout { width: 190px; min-width: 190px; }", self.template)
        self.assertIn('colspan="14"', self.template)

    def test_bulk_checkout_button_is_bound_to_bulk_endpoint(self):
        self.assertIn('id="btnCheckSelectedCheckoutV2"', self.template)
        self.assertIn("function checkSelectedCheckoutSessions()", self.template)
        self.assertIn("/api/accounts/check-checkout-session-bulk", self.template)
        self.assertIn("bind('btnCheckSelectedCheckoutV2', checkSelectedCheckoutSessions)", self.template)
        selection_block = self.template[self.template.index("const v2Ids = ["):self.template.index("  ];", self.template.index("const v2Ids = ["))]
        self.assertIn("btnCheckSelectedCheckoutV2", selection_block)

    def test_checkout_type_filter_is_selectable_and_applies_to_list_and_poll(self):
        for value in ("", "none", "oaics", "cs_live", "other_cs", "unknown"):
            self.assertIn(f'<option value="{value}">', self.template)
        self.assertIn('id="checkoutTypeFilterV2"', self.template)
        self.assertIn("function applyAccountsCheckoutTypeFilter(value)", self.template)
        self.assertIn("checkout_type=${encodeURIComponent(checkoutType)}", self.template)
        self.assertIn("applyAccountsCheckoutTypeFilter(checkoutTypeV2.value)", self.template)

    def test_cell_covers_all_states_types_and_failure_tooltip(self):
        start = self.template.index("function _checkoutSessionErrorTitle")
        end = self.template.index("function _planCell", start)
        renderer = self.template[start:end]
        for value in ("queued", "running", "success", "failed"):
            self.assertIn(value, renderer)
        for value in ("oaics", "cs_live", "other_cs", "unknown"):
            self.assertIn(value, renderer)
        self.assertIn("HTTP ${r.checkout_check_http_status}", renderer)
        self.assertIn("传输错误: ${message}", renderer)
        status_start = self.template.index("function _checkoutStatusMeta")
        status_end = self.template.index("function _checkoutSessionErrorTitle", status_start)
        status_renderer = self.template[status_start:status_end]
        self.assertIn("function _checkoutStatusMeta", status_renderer)
        self.assertIn("checkout_check_updated_at", status_renderer)
        cell_start = self.template.index("function _checkoutSessionCell")
        cell_end = self.template.index("function _planCell", cell_start)
        self.assertNotIn('data-checkout-session="${esc(r.id)}"', self.template[cell_start:cell_end])
        menu_start = self.template.index("function _accountsV2MoreMenu")
        menu_end = self.template.index("function closeAccountsV2MoreMenus", menu_start)
        self.assertIn("_checkoutSessionAction(r)", self.template[menu_start:menu_end])

    def test_existing_poll_and_click_delegate_are_reused_without_id_leak(self):
        self.assertIn("wasCheckouting", self.template)
        self.assertIn("pollAccountPlanStatuses", self.template)
        self.assertIn("await checkOneCheckoutSession", self.template)
        self.assertEqual(self.template.count("setInterval(() => { if (!$('#tab-accounts').classList.contains('hidden')) pollAccountPlanStatuses(); }, 2000);"), 1)
        self.assertNotIn("checkout_session_id", self.template)


if __name__ == "__main__":
    unittest.main()
