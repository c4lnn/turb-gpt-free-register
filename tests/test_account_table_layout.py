import unittest
from pathlib import Path


class AccountTableLayoutTests(unittest.TestCase):
    def test_note_column_is_immediately_before_created_time(self):
        root = Path(__file__).parents[1] / "webui" / "templates"
        template = (root / "index.html").read_text(encoding="utf-8")
        table_start = template.index('<table class="accounts-table-v2">')
        table_end = template.index("</table>", table_start)
        table = template[table_start:table_end]
        self.assertLess(table.index('class="col-note"'), table.index('class="col-time"'))

        row_start = template.index("const rowHtmlV2")
        row_end = template.index("</tr>", row_start)
        row = template[row_start:row_end]
        self.assertLess(row.index('class="col-note"'), row.index('class="col-time"'))


if __name__ == "__main__":
    unittest.main()
