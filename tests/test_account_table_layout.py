import unittest
from pathlib import Path


class AccountTableLayoutTests(unittest.TestCase):
    def test_note_column_is_immediately_before_created_time(self):
        root = Path(__file__).parents[1] / "webui" / "templates"
        for name, table_marker, row_marker, row_note_marker, row_time_marker in (
            ("index.html", '<table class="accounts-table-v2">', "const rowHtmlV2", 'class="col-note"', 'class="col-time"'),
            ("index_legacy.html", '<table class="accounts-table">', "$('#accountsBody')", 'class="wrap"', "r.created_at"),
        ):
            template = (root / name).read_text(encoding="utf-8")
            table_start = template.index(table_marker)
            table_end = template.index("</table>", table_start)
            table = template[table_start:table_end]
            self.assertLess(table.index('class="col-note"'), table.index('class="col-time"'), name)

            row_start = template.index(row_marker)
            row_end = template.index("</tr>", row_start)
            row = template[row_start:row_end]
            self.assertLess(row.index(row_note_marker), row.index(row_time_marker), name)


if __name__ == "__main__":
    unittest.main()
