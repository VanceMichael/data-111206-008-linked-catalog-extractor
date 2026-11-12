import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from semantics import ContextRegistry, analyze_page

FIXTURE_REGISTRY = ContextRegistry('fixtures/contexts.json')


def analyze_html(html, registry=FIXTURE_REGISTRY, digest=None):
    raw = html.encode('utf-8') if isinstance(html, str) else html
    digest = digest or hashlib.sha256(raw).hexdigest()
    return analyze_page(raw, registry, digest)[0]


def analyze_fixture(name):
    return analyze_html(Path('fixtures', name).read_bytes())


CANONICAL = '<link rel="canonical" href="https://catalog.example/items/main">'
BASE_CONTEXT = ('[{"@vocab": "https://schema.org/"}, '
                '{"@base": "https://catalog.example/items/", '
                '"offers": {"@id": "offers", "@type": "@id"}, '
                '"seller": {"@id": "seller", "@type": "@id"}}]')


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = analyze_fixture('catalog.html')

    def test_main_product_matches_canonical(self):
        self.assertEqual(self.result['主产品']['@id'], 'https://catalog.example/items/main')

    def test_language_versions_coexist(self):
        names = {(item['值'], item['语言']) for item in self.result['主产品']['名称']}
        self.assertEqual(names, {('合成电机', 'zh'), ('Synthetic motor', 'en')})

    def test_only_explicit_offer_attached(self):
        offers = self.result['主产品']['报价']
        self.assertEqual([offer['@id'] for offer in offers],
                         ['https://catalog.example/items/offer-main'])
        detached = {node['@id'] for node in self.result['未接入主产品']}
        self.assertIn('https://catalog.example/items/recommend', detached)
        self.assertIn('https://catalog.example/items/offer-other', detached)

    def test_offer_details_and_seller(self):
        offer = self.result['主产品']['报价'][0]
        self.assertEqual(offer['价格']['值'], '120.10')
        self.assertEqual(offer['币种']['值'], 'USD')
        self.assertTrue(offer['可发布'])
        seller = offer['卖方'][0]
        self.assertEqual(seller['@id'], 'https://catalog.example/items/seller-main')
        self.assertEqual({(item['值'], item['语言']) for item in seller['名称']},
                         {('虚构商会企业', 'zh')})

    def test_fields_trace_to_script_and_path(self):
        offer = self.result['主产品']['报价'][0]
        source = offer['价格']['来源'][0]
        self.assertEqual(source['脚本'], 1)
        self.assertEqual(source['行号'], 3)
        self.assertIn('cost', source['路径'])
        self.assertEqual(len(source['脚本摘要']), 64)
        name_source = self.result['主产品']['名称'][0]['来源'][0]
        self.assertEqual(name_source['脚本'], 0)
        self.assertIn('@graph', name_source['路径'])

    def test_missing_seller_reported_not_fabricated(self):
        problems = json.dumps(self.result['无法确定'], ensure_ascii=False)
        self.assertIn('缺少卖方', problems)
        self.assertIn('offer-other', problems)

    def test_raw_script_entries_kept(self):
        self.assertEqual(len(self.result['原始脚本']), 2)
        self.assertTrue(all(len(item['摘要']) == 64 for item in self.result['原始脚本']))


class ConflictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = analyze_fixture('conflict.html')

    def test_conflicting_offer_blocked(self):
        offer = self.result['主产品']['报价'][0]
        self.assertFalse(offer['可发布'])
        self.assertEqual({item['值'] for item in offer['价格候选']}, {'120.10', '999.00'})
        self.assertEqual({item['值'] for item in offer['币种候选']}, {'USD', 'EUR'})
        self.assertIn('价格冲突', offer['阻止原因'])
        self.assertIn('币种冲突', offer['阻止原因'])

    def test_candidates_keep_their_provenance(self):
        offer = self.result['主产品']['报价'][0]
        scripts = {tuple(sorted(prov['脚本'] for prov in item['来源']))
                   for item in offer['价格候选']}
        self.assertEqual(scripts, {(1,), (2,)})


class ContextProblemTests(unittest.TestCase):
    def test_unknown_context_reported(self):
        result = analyze_html('<script type="application/ld+json">'
                              '{"@context": "https://contexts.example/missing", "@type": "Product"}'
                              '</script>')
        self.assertEqual(result['上下文问题'][0]['问题'], '未登记')
        self.assertIsNone(result['主产品'])

    def test_circular_context_reported(self):
        result = analyze_html('<script type="application/ld+json">'
                              '{"@context": "https://contexts.example/loop-a", "@type": "Product"}'
                              '</script>')
        self.assertEqual(result['上下文问题'][0]['问题'], '循环')

    def test_prefix_terms_expand(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'contexts.json'
            path.write_text(json.dumps({'https://ctx.example/p': {
                '@context': {'ex': 'https://example.com/ns/'}}}))
            registry = ContextRegistry(path)
            result = analyze_html('<script type="application/ld+json">'
                                  '{"@context": "https://ctx.example/p", '
                                  '"@id": "https://x.example/a", "@type": "ex:Widget"}'
                                  '</script>', registry)
        node = result['未接入主产品'][0]
        self.assertEqual(node['类型'], ['https://example.com/ns/Widget'])


class GraphRuleTests(unittest.TestCase):
    def test_blank_nodes_scoped_per_page(self):
        html = ('<script type="application/ld+json">'
                '{"@context": {"@vocab": "https://schema.org/"}, "@type": "Product", "name": "x"}'
                '</script>')
        first = analyze_html(html, digest='a' * 64)
        second = analyze_html(html, digest='b' * 64)
        first_id = first['未接入主产品'][0]['@id']
        second_id = second['未接入主产品'][0]['@id']
        self.assertNotEqual(first_id, second_id)
        self.assertTrue(first_id.startswith('_:' + 'a' * 12))
        self.assertTrue(second_id.startswith('_:' + 'b' * 12))

    def test_arrays_keep_all_values(self):
        html = (CANONICAL +
                '<script type="application/ld+json">'
                f'{{"@context": {BASE_CONTEXT}, "@id": "main", "@type": "Product", '
                '"name": ["甲", "乙"], "offers": ["o1", "o2"]}</script>'
                '<script type="application/ld+json">'
                f'{{"@context": {BASE_CONTEXT}, "@graph": ['
                '{"@id": "o1", "@type": "Offer", "price": "1", "priceCurrency": "USD"},'
                '{"@id": "o2", "@type": "Offer", "price": "2", "priceCurrency": "EUR"}]}</script>')
        result = analyze_html(html)
        self.assertEqual([offer['@id'] for offer in result['主产品']['报价']],
                         ['https://catalog.example/items/o1', 'https://catalog.example/items/o2'])
        self.assertEqual({item['值'] for item in result['主产品']['名称']}, {'甲', '乙'})

    def test_missing_currency_not_defaulted(self):
        html = (CANONICAL +
                '<script type="application/ld+json">'
                f'{{"@context": {BASE_CONTEXT}, "@graph": ['
                '{"@id": "main", "@type": "Product", "offers": {"@id": "o1"}},'
                '{"@id": "o1", "@type": "Offer", "price": "7"}]}</script>')
        result = analyze_html(html)
        offer = result['主产品']['报价'][0]
        self.assertFalse(offer['可发布'])
        self.assertIn('缺少币种', offer['阻止原因'])
        output = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('CNY', output)
        self.assertNotIn('人民币', output)

    def test_same_id_across_scripts_merges(self):
        html = (CANONICAL +
                '<script type="application/ld+json">'
                f'{{"@context": {BASE_CONTEXT}, "@graph": ['
                '{"@id": "main", "@type": "Product", "offers": {"@id": "o1"}},'
                '{"@id": "o1", "@type": "Offer", "price": "10", "priceCurrency": "USD"}]}</script>'
                '<script type="application/ld+json">'
                f'{{"@context": {BASE_CONTEXT}, "@id": "o1", "seller": {{"@id": "s1"}}}}</script>'
                '<script type="application/ld+json">'
                f'{{"@context": {BASE_CONTEXT}, "@id": "s1", "@type": "Organization", "name": "卖方"}}</script>')
        result = analyze_html(html)
        offer = result['主产品']['报价'][0]
        self.assertTrue(offer['可发布'])
        self.assertEqual(offer['卖方'][0]['@id'], 'https://catalog.example/items/s1')
        self.assertEqual(offer['卖方'][0]['名称'][0]['值'], '卖方')

    def test_nested_graph_and_id_reference_connected(self):
        html = (CANONICAL +
                '<script type="application/ld+json">'
                f'{{"@context": {BASE_CONTEXT}, "@graph": ['
                '{"@id": "main", "@type": "Product", "offers": {"@id": "o1"}, "@graph": ['
                '{"@id": "o1", "@type": "Offer", "price": "5", "priceCurrency": "USD"}]}]}</script>')
        result = analyze_html(html)
        offer = result['主产品']['报价'][0]
        self.assertEqual(offer['价格']['值'], '5')
        self.assertTrue(offer['可发布'])

    def test_relative_id_without_base_reported(self):
        result = analyze_html('<script type="application/ld+json">'
                              '{"@context": {"@vocab": "https://schema.org/"}, '
                              '"@id": "main", "@type": "Product"}</script>')
        self.assertTrue(any('base' in item['问题'] for item in result['图问题']))

    def test_same_value_twice_is_not_conflict(self):
        offer_script = ('{"@id": "o1", "@type": "Offer", "price": "10", "priceCurrency": "USD"}')
        html = (CANONICAL +
                '<script type="application/ld+json">'
                f'{{"@context": {BASE_CONTEXT}, "@graph": ['
                '{"@id": "main", "@type": "Product", "offers": {"@id": "o1"}},'
                f'{offer_script}]}}</script>'
                '<script type="application/ld+json">'
                f'{{"@context": {BASE_CONTEXT}, "@graph": [{offer_script}]}}</script>')
        result = analyze_html(html)
        offer = result['主产品']['报价'][0]
        self.assertTrue(offer['可发布'])
        self.assertEqual(offer['价格']['值'], '10')
        self.assertEqual(len(offer['价格']['来源']), 2)


if __name__ == '__main__':
    unittest.main()
