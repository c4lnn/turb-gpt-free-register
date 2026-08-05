import unittest
from pathlib import Path


class ExtractLinkStatusColumnTests(unittest.TestCase):
    ROOT = Path(__file__).parents[1] / "webui" / "templates"

    def _template(self, name: str) -> str:
        return (self.ROOT / name).read_text(encoding="utf-8")

    def test_extract_link_column_is_separate_from_plan_column(self):
        for name, table_marker, row_marker in (
            ("index.html", '<table class="accounts-table-v2">', "const rowHtmlV2"),
            ("index_legacy.html", '<table class="accounts-table">', "$('#accountsBody')"),
        ):
            template = self._template(name)
            table_start = template.index(table_marker)
            table_end = template.index("</table>", table_start)
            table = template[table_start:table_end]
            self.assertIn('class="col-extract-link"', table, name)
            self.assertLess(table.index('class="col-plan"'), table.index('class="col-extract-link"'), name)
            self.assertLess(table.index('class="col-extract-link"'), table.index('class="col-small"'), name)
            self.assertIn("提链", table, name)

            row_start = template.index(row_marker)
            row_end = template.index("</tr>", row_start)
            row = template[row_start:row_end]
            self.assertIn('class="col-plan"', row, name)
            self.assertIn('class="col-extract-link"', row, name)
            self.assertLess(row.index('class="col-plan"'), row.index('class="col-extract-link"'), name)
            self.assertNotIn('${_planCell(r)}<div class="sub-cell">${_extractLinkCell(r)}</div>', row, name)
            self.assertNotIn('${_planCell(r)}<div class="acc-v2-sub">${_extractLinkCell(r)}</div>', row, name)

    def test_render_contract_contains_route_fields_statuses_and_empty_placeholder(self):
        for name in ("index.html", "index_legacy.html"):
            template = self._template(name)
            helper_start = template.index("function _extractLinkStatusLabel")
            helper_end = template.index("function _extractLinkAction", helper_start)
            helper = template[helper_start:helper_end]

            for field in (
                "extract_link_type",
                "extract_link_provider",
                "extract_link_status",
                "extract_link_error",
                "extract_link_message",
            ):
                self.assertIn(field, helper, name)
            for status in ("queued", "running", "success", "failed", "canceled", "stopped"):
                self.assertIn(status, helper, name)
            self.assertIn("(value || '-')", helper, name)
            self.assertIn("function _extractLinkSummary", helper, name)
            self.assertIn(" · ", helper, name)
            self.assertIn("提炼失败", helper, name)
            self.assertIn("extract-link-actions", helper, name)
            self.assertIn("extract_link_provider || '-'", helper, name)
            self.assertNotIn("extract-link-route", helper, name)


if __name__ == "__main__":
    unittest.main()
