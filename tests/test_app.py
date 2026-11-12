import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = str(Path(self.directory.name) / 'raw.sqlite3')

    def run_app(self, *args):
        process = subprocess.run(
            [sys.executable, 'app.py', *args, '--output', self.output],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        return process.stdout

    def test_demo_links_main_product(self):
        result = json.loads(self.run_app('demo'))
        pages = {page['文件']: page for page in result['页面']}
        catalog = pages['catalog.html']
        self.assertEqual(catalog['变化'], '首次解析')
        main = catalog['分析']['主产品']
        self.assertEqual(main['@id'], 'https://catalog.example/items/main')
        self.assertEqual([offer['@id'] for offer in main['报价']],
                         ['https://catalog.example/items/offer-main'])
        conflict_offer = pages['conflict.html']['分析']['主产品']['报价'][0]
        self.assertFalse(conflict_offer['可发布'])

    def test_repeat_run_reports_no_change(self):
        self.run_app('demo')
        result = json.loads(self.run_app('demo'))
        self.assertEqual({page['变化'] for page in result['页面']}, {'无变化'})

    def test_context_update_is_semantic_change(self):
        contexts = Path(self.directory.name) / 'contexts.json'
        shutil.copy('fixtures/contexts.json', contexts)
        first = json.loads(self.run_app('demo', '--contexts', str(contexts)))
        contexts.write_text(contexts.read_text().replace('"cost": "price"', '"cost": "listprice"'))
        second = json.loads(self.run_app('demo', '--contexts', str(contexts)))
        for before, after in zip(first['页面'], second['页面']):
            self.assertEqual(after['变化'], '语义解释变化')
            self.assertEqual(before['页面摘要'], after['页面摘要'])
            self.assertNotEqual(before['上下文摘要'], after['上下文摘要'])

    def test_content_change_is_distinguished(self):
        fixtures = Path(self.directory.name) / 'fixtures'
        fixtures.mkdir()
        page = fixtures / 'catalog.html'
        shutil.copy('fixtures/catalog.html', page)
        self.run_app('demo', '--fixtures', str(fixtures))
        page.write_text(page.read_text() + '\n<!-- 正文变化 -->\n')
        result = json.loads(self.run_app('demo', '--fixtures', str(fixtures)))
        self.assertEqual(result['页面'][0]['变化'], '正文变化')

    def test_analyze_reparses_stored_originals(self):
        contexts = Path(self.directory.name) / 'contexts.json'
        shutil.copy('fixtures/contexts.json', contexts)
        self.run_app('demo', '--contexts', str(contexts))
        contexts.write_text(contexts.read_text().replace('"cost": "price"', '"cost": "listprice"'))
        result = json.loads(self.run_app('analyze', '--contexts', str(contexts)))
        self.assertEqual({page['变化'] for page in result['页面']}, {'语义解释变化'})

    def test_show_prints_raw_script(self):
        result = json.loads(self.run_app('demo'))
        digest = result['页面'][0]['分析']['原始脚本'][0]['摘要']
        output = self.run_app('show', digest)
        self.assertIn('@graph', output)


if __name__ == '__main__':
    unittest.main()
