import unittest
from pathlib import Path
from linked_source import extract


class ScriptTests(unittest.TestCase):
    def test_multiple_scripts_retained(self):
        rows = extract(Path('fixtures/catalog.html').read_text())
        self.assertEqual(len(rows), 2)
        self.assertGreater(rows[1]['line'], rows[0]['line'])

    def test_original_aliases_are_not_expanded(self):
        row = extract(Path('fixtures/catalog.html').read_text())[0]
        self.assertEqual(row['value']['@graph'][0]['@type'],'item')

    def test_broken_json_preserved(self):
        row=extract('<script type="application/ld+json">{invalid}</script>')[0]
        self.assertIn('error',row)
        self.assertEqual(row['raw'],'{invalid}')

    def test_plain_script_ignored(self):
        self.assertEqual(extract('<script>window.any=1</script>'),[])

    def test_unterminated_rejected(self):
        with self.assertRaises(ValueError):
            extract('<script type="application/ld+json">{}')
