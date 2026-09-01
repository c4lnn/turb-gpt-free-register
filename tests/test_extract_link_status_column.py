import unittest
from pathlib import Path


class ExtractLinkStatusColumnTests(unittest.TestCase):
    ROOT = Path(__file__).parents[1] / "webui" / "templates"

    def _template(self, name: str) -> str:
        return (self.ROOT / name).read_text(encoding="utf-8")

    def test_extract_link_column_is_separate_from_plan_column(self):
        template = self._template("index.html")
        table_start = template.index('<table class="accounts-table-v2">')
        table_end = template.index("</table>", table_start)
        table = template[table_start:table_end]
        self.assertIn('class="col-extract-link"', table)
        self.assertLess(table.index('class="col-plan"'), table.index('class="col-extract-link"'))
        self.assertLess(table.index('class="col-extract-link"'), table.index('class="col-small"'))
        self.assertIn("提链", table)

        row_start = template.index("const rowHtmlV2")
        row_end = template.index("</tr>", row_start)
        row = template[row_start:row_end]
        self.assertIn('class="col-plan"', row)
        self.assertIn('class="col-extract-link"', row)
        self.assertLess(row.index('class="col-plan"'), row.index('class="col-extract-link"'))
        self.assertNotIn('${_planCell(r)}<div class="acc-v2-sub">${_extractLinkCell(r)}</div>', row)

    def test_render_contract_contains_route_fields_statuses_and_empty_placeholder(self):
        template = self._template("index.html")
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
            self.assertIn(field, helper)
        for status in ("queued", "running", "success", "failed", "canceled"):
            self.assertIn(status, helper)
        self.assertNotIn("s === 'stopped'", helper)
        self.assertNotIn("s === 'cancelled'", helper)
        self.assertIn("_statusLabel('extract', value)", helper)
        self.assertIn("unknown: '未知'", template)
        self.assertIn("function _extractLinkSummary", helper)
        self.assertIn(" · ", helper)
        self.assertIn("failed: '提炼失败'", template)
        self.assertIn("extract-link-actions", helper)
        self.assertIn("extract_link_provider || '-'", helper)
        self.assertNotIn("extract-link-route", helper)


if __name__ == "__main__":
    unittest.main()
