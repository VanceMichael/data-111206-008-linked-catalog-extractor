"""JSON-LD 子集解释:按登记上下文展开脚本、连接语义图并产出可追溯的产品与报价。

支持的上下文特性与 fixtures/contexts.json 对应:本地与远程上下文、词项别名、
前缀、@base、@vocab、语言映射、嵌套 @graph 与 @id 引用。远程上下文只从预先
登记的本地副本解析,运行时不访问网络;未登记或循环的上下文记入"上下文问题",
不静默忽略。相对 @id 按各脚本有效 base 解释;匿名节点身份带页面摘要前缀,
不跨页面串用;数组始终保留为多个值,不折叠成唯一值。
"""

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from linked_source import extract

SCHEMA = 'https://schema.org/'
_SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
_SAFE_KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class ContextError(Exception):
    """上下文无法解析。kind 取值:未登记 / 循环 / 无效。"""

    def __init__(self, kind, url, detail=''):
        self.kind = kind
        self.url = url
        super().__init__(f'{kind}: {url} {detail}'.strip())


class ContextRegistry:
    """远程上下文的本地登记副本;digest 代表语义解释版本,副本更新后随之变化。"""

    def __init__(self, path):
        raw = Path(path).read_bytes()
        self.digest = hashlib.sha256(raw).hexdigest()
        self.copies = json.loads(raw.decode('utf-8'))
        if not isinstance(self.copies, dict):
            raise ValueError('上下文登记必须是对象')

    def resolve(self, url, stack):
        if url in stack:
            raise ContextError('循环', url, ' → '.join(list(stack) + [url]))
        if url not in self.copies:
            raise ContextError('未登记', url)
        body = self.copies[url]
        if not isinstance(body, dict) or '@context' not in body:
            raise ContextError('无效', url, '登记副本缺少 @context')
        return body['@context']


class _DocumentHead(HTMLParser):
    """读取页面级 base 与规范地址(canonical),不参与节点归属猜测。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base = None
        self.canonical = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'base' and self.base is None and attributes.get('href'):
            self.base = attributes['href']
        elif tag == 'link' and self.canonical is None:
            rel = (attributes.get('rel') or '').lower().split()
            if 'canonical' in rel and attributes.get('href'):
                self.canonical = attributes['href']


def document_info(html):
    parser = _DocumentHead()
    parser.feed(html)
    return {'base': parser.base, 'canonical': parser.canonical}


def _child(path, key):
    if isinstance(key, int):
        return f'{path}[{key}]'
    if _SAFE_KEY.match(key):
        return f'{path}.{key}'
    return f'{path}[{json.dumps(key, ensure_ascii=False)}]'


def _term_definition(value):
    if isinstance(value, str):
        return {'@id': value}
    if isinstance(value, dict):
        definition = {key: value[key] for key in ('@id', '@type', '@container') if key in value}
        return definition or None
    return None


def _identity(entry):
    if entry['kind'] == 'ref':
        return ('ref', entry['@id'])
    return ('literal', json.dumps(entry['值'], ensure_ascii=False, sort_keys=True), entry['语言'])


class Node:
    """图节点。属性值永远保持列表,数组合并后不折叠成唯一值。"""

    def __init__(self, node_id):
        self.id = node_id
        self.types = []
        self.props = {}

    def add_type(self, type_iri, prov):
        for entry in self.types:
            if entry['值'] == type_iri:
                if prov not in entry['来源']:
                    entry['来源'].append(prov)
                return
        self.types.append({'值': type_iri, '来源': [prov]})

    def add_value(self, iri, entry):
        entries = self.props.setdefault(iri, [])
        identity = _identity(entry)
        for existing in entries:
            if _identity(existing) == identity:
                for prov in entry['来源']:
                    if prov not in existing['来源']:
                        existing['来源'].append(prov)
                return
        entries.append(entry)


class Graph:
    def __init__(self):
        self.nodes = {}

    def node(self, node_id):
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(node_id)
        return self.nodes[node_id]


class _Expander:
    """把一页的脚本按各自上下文展开成一张图,问题逐条登记。"""

    def __init__(self, registry, document_base, page_digest):
        self.registry = registry
        self.document_base = document_base
        self.scope = f'_:{page_digest[:12]}-'
        self.counter = 0
        self.graph = Graph()
        self.context_problems = []
        self.script_problems = []
        self.graph_problems = []

    def prov(self, meta, path):
        return {'脚本': meta['脚本'], '行号': meta['行号'], '路径': path, '脚本摘要': meta['脚本摘要']}

    # ---- 上下文 ----

    def apply_context(self, value, active, stack):
        if value is None:
            return active
        if isinstance(value, list):
            for item in value:
                active = self.apply_context(item, active, stack)
            return active
        if isinstance(value, str):
            remote = self.registry.resolve(value, stack)
            return self.apply_context(remote, active, stack + (value,))
        if isinstance(value, dict):
            terms = dict(active['terms'])
            vocab = active['vocab']
            base = active['base']
            for key, definition in value.items():
                if key == '@vocab':
                    if definition is None or isinstance(definition, str):
                        vocab = definition
                elif key == '@base':
                    if definition is None:
                        base = self.document_base
                    elif isinstance(definition, str):
                        base = urljoin(base, definition) if base else definition
                elif key.startswith('@'):
                    continue
                else:
                    terms[key] = _term_definition(definition)
            return {'terms': terms, 'vocab': vocab, 'base': base}
        return active

    def expand_iri(self, term, active, seen=()):
        if term.startswith('@'):
            return term
        if term not in seen:
            definition = active['terms'].get(term)
            if definition:
                target = definition.get('@id')
                if target:
                    if target.startswith('@') or _SCHEME.match(target):
                        return target
                    return self.expand_iri(target, active, seen + (term,))
        if ':' in term:
            prefix, suffix = term.split(':', 1)
            if prefix != '_':
                head = active['terms'].get(prefix)
                if head and head.get('@id'):
                    return head['@id'] + suffix
            return term
        vocab = active['vocab']
        return vocab + term if vocab else term

    def resolve_id(self, value, base, meta, path):
        if value.startswith('_:'):
            return self.scope + value[2:]
        if _SCHEME.match(value):
            return value
        if base:
            return urljoin(base, value)
        self.graph_problems.append({'问题': '相对 @id 缺少可用 base', '值': value,
                                    '来源': self.prov(meta, path)})
        return None

    # ---- 展开 ----

    def expand_script(self, script, meta):
        value = script['value']
        active = {'terms': {}, 'vocab': None, 'base': self.document_base}
        if isinstance(value, dict) and '@context' in value:
            try:
                active = self.apply_context(value['@context'], active, ())
            except ContextError as error:
                self.context_problems.append({'脚本': meta['脚本'], '行号': meta['行号'],
                                              '上下文': error.url, '问题': error.kind})
                return
        base = active['base']
        if isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    self.expand_node(item, active, base, _child('$', index), meta)
        elif isinstance(value, dict):
            graph = value.get('@graph')
            extras = [key for key in value if key not in ('@context', '@graph')]
            if extras:
                node_object = {key: item for key, item in value.items() if key != '@context'}
                self.expand_node(node_object, active, base, '$', meta)
            elif isinstance(graph, list):
                for index, item in enumerate(graph):
                    if isinstance(item, dict):
                        self.expand_node(item, active, base, _child(_child('$', '@graph'), index), meta)
            elif isinstance(graph, dict):
                self.expand_node(graph, active, base, _child('$', '@graph'), meta)
        else:
            self.script_problems.append({'脚本': meta['脚本'], '行号': meta['行号'],
                                         '问题': '顶层不是对象或数组'})

    def expand_node(self, obj, active, base, path, meta):
        local = active
        if '@context' in obj:
            try:
                local = self.apply_context(obj['@context'], active, ())
            except ContextError as error:
                self.context_problems.append({'脚本': meta['脚本'], '行号': meta['行号'],
                                              '上下文': error.url, '问题': error.kind, '路径': path})
                return None
        base = local['base']
        raw_id = obj.get('@id')
        node_id = self.resolve_id(raw_id, base, meta, _child(path, '@id')) if isinstance(raw_id, str) else None
        if node_id is None:
            self.counter += 1
            node_id = f'{self.scope}blank-{self.counter}'
        node = self.graph.node(node_id)
        for key, raw in obj.items():
            if key in ('@context', '@id'):
                continue
            child = _child(path, key)
            if key == '@type':
                items = raw if isinstance(raw, list) else [raw]
                for index, item in enumerate(items):
                    if isinstance(item, str):
                        type_path = _child(child, index) if isinstance(raw, list) else child
                        node.add_type(self.expand_iri(item, local), self.prov(meta, type_path))
                continue
            if key == '@graph':
                items = raw if isinstance(raw, list) else [raw]
                for index, item in enumerate(items):
                    if isinstance(item, dict):
                        item_path = _child(child, index) if isinstance(raw, list) else child
                        self.expand_node(item, local, base, item_path, meta)
                continue
            if key.startswith('@'):
                continue
            if key in local['terms'] and local['terms'][key] is None:
                continue
            iri = self.expand_iri(key, local)
            definition = local['terms'].get(key) or {}
            self.expand_property(node, iri, definition, raw, local, base, child, meta)
        return node_id

    def expand_property(self, node, iri, definition, raw, active, base, path, meta):
        items = raw if isinstance(raw, list) else [raw]
        for index, item in enumerate(items):
            child = _child(path, index) if isinstance(raw, list) else path
            self.expand_value(node, iri, definition, item, active, base, child, meta)

    def expand_value(self, node, iri, definition, raw, active, base, path, meta):
        if isinstance(raw, dict):
            if '@value' in raw:
                node.add_value(iri, {'kind': 'literal', '值': raw.get('@value'),
                                     '语言': raw.get('@language'), '来源': [self.prov(meta, path)]})
            elif self._is_language_map(definition):
                for language, texts in raw.items():
                    items = texts if isinstance(texts, list) else [texts]
                    for index, text in enumerate(items):
                        text_path = _child(_child(path, language), index) if isinstance(texts, list) else _child(path, language)
                        if isinstance(text, str):
                            node.add_value(iri, {'kind': 'literal', '值': text, '语言': language,
                                                 '来源': [self.prov(meta, text_path)]})
            elif set(raw) == {'@id'} and isinstance(raw['@id'], str):
                ref = self.resolve_id(raw['@id'], base, meta, _child(path, '@id'))
                if ref:
                    node.add_value(iri, {'kind': 'ref', '@id': ref, '来源': [self.prov(meta, path)]})
            else:
                nested = self.expand_node(raw, active, base, path, meta)
                if nested:
                    node.add_value(iri, {'kind': 'ref', '@id': nested, '来源': [self.prov(meta, path)]})
        elif isinstance(raw, str):
            if definition.get('@type') == '@id':
                ref = self.resolve_id(raw, base, meta, path)
                if ref:
                    node.add_value(iri, {'kind': 'ref', '@id': ref, '来源': [self.prov(meta, path)]})
            else:
                node.add_value(iri, {'kind': 'literal', '值': raw, '语言': None,
                                     '来源': [self.prov(meta, path)]})
        elif raw is not None and isinstance(raw, (int, float, bool)):
            node.add_value(iri, {'kind': 'literal', '值': raw, '语言': None,
                                 '来源': [self.prov(meta, path)]})

    @staticmethod
    def _is_language_map(definition):
        container = definition.get('@container')
        return container == '@language' or (isinstance(container, list) and '@language' in container)


# ---- 业务识别:先连图,再定主产品、报价与卖方 ----


def _literals(node, iri):
    return [entry for entry in node.props.get(iri, []) if entry['kind'] == 'literal']


def _references(node, iri):
    return [entry for entry in node.props.get(iri, []) if entry['kind'] == 'ref']


def _names(node):
    return [{'值': entry['值'], '语言': entry['语言'], '来源': entry['来源']}
            for entry in _literals(node, SCHEMA + 'name')]


def _offer(node, graph, undetermined):
    offer = {'@id': node.id}
    blocked = []
    prices = _literals(node, SCHEMA + 'price')
    if len(prices) == 1:
        offer['价格'] = {'值': prices[0]['值'], '来源': prices[0]['来源']}
    elif prices:
        offer['价格候选'] = [{'值': entry['值'], '来源': entry['来源']} for entry in prices]
        blocked.append('价格冲突')
    else:
        blocked.append('缺少价格')
        undetermined.append({'节点': node.id, '问题': '报价缺少价格'})
    currencies = _literals(node, SCHEMA + 'priceCurrency')
    if len(currencies) == 1:
        offer['币种'] = {'值': currencies[0]['值'], '来源': currencies[0]['来源']}
    elif currencies:
        offer['币种候选'] = [{'值': entry['值'], '来源': entry['来源']} for entry in currencies]
        blocked.append('币种冲突')
    else:
        blocked.append('缺少币种')
        undetermined.append({'节点': node.id, '问题': '报价缺少币种,不默认任何币种'})
    sellers = []
    for entry in _references(node, SCHEMA + 'seller'):
        seller = {'@id': entry['@id'], '来源': entry['来源']}
        target = graph.nodes.get(entry['@id'])
        if target is None:
            undetermined.append({'节点': node.id, '问题': f"卖方 {entry['@id']} 没有对应节点"})
        else:
            seller['名称'] = _names(target)
        sellers.append(seller)
    if sellers:
        offer['卖方'] = sellers
    else:
        undetermined.append({'节点': node.id, '问题': '报价缺少卖方'})
    offer['可发布'] = not blocked
    if blocked:
        offer['阻止原因'] = blocked
    return offer


def _offer_gaps(node, undetermined):
    """未接入主产品的报价也登记完整性缺口,只是不参与发布判断。"""
    if not _literals(node, SCHEMA + 'price'):
        undetermined.append({'节点': node.id, '问题': '报价缺少价格'})
    if not _literals(node, SCHEMA + 'priceCurrency'):
        undetermined.append({'节点': node.id, '问题': '报价缺少币种,不默认任何币种'})
    if not _references(node, SCHEMA + 'seller'):
        undetermined.append({'节点': node.id, '问题': '报价缺少卖方'})


def _reachable(graph, start_id):
    seen = set()
    stack = [start_id]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        node = graph.nodes.get(node_id)
        if node is None:
            continue
        for entries in node.props.values():
            for entry in entries:
                if entry['kind'] == 'ref':
                    stack.append(entry['@id'])
    return seen


def analyze_graph(graph, canonical):
    """主产品只认 @id 与规范地址一致的产品节点,不按可见页面位置猜测。"""
    undetermined = []
    products = [node for node in graph.nodes.values()
                if any(entry['值'] == SCHEMA + 'Product' for entry in node.types)]
    main = None
    if canonical:
        matches = [node for node in products if node.id == canonical]
        if len(matches) == 1:
            main = matches[0]
        elif matches:
            undetermined.append({'问题': '多个产品节点使用规范地址',
                                 '候选': sorted(node.id for node in matches)})
        else:
            undetermined.append({'问题': '规范地址未对应任何产品节点', '规范地址': canonical,
                                 '产品候选': sorted(node.id for node in products)})
    else:
        undetermined.append({'问题': '页面缺少规范地址,主产品无法确定',
                             '产品候选': sorted(node.id for node in products)})
    result = {'主产品': None}
    connected = set()
    if main is not None:
        connected = _reachable(graph, main.id)
        offers = []
        for entry in _references(main, SCHEMA + 'offers'):
            target = graph.nodes.get(entry['@id'])
            if target is None:
                undetermined.append({'节点': main.id, '问题': f"报价 {entry['@id']} 没有对应节点",
                                     '来源': entry['来源']})
                continue
            offer = _offer(target, graph, undetermined)
            offer['关联来源'] = entry['来源']
            offers.append(offer)
        offers.sort(key=lambda item: item['@id'])
        result['主产品'] = {'@id': main.id, '名称': _names(main), '报价': offers}
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        if node_id not in connected and any(
                entry['值'] == SCHEMA + 'Offer' for entry in node.types):
            _offer_gaps(node, undetermined)
    detached = []
    for node_id in sorted(graph.nodes):
        if node_id in connected:
            continue
        node = graph.nodes[node_id]
        entry = {'@id': node_id, '类型': [item['值'] for item in node.types],
                 '说明': '与主产品无显式关联,未接入'}
        names = _names(node)
        if names:
            entry['名称'] = names
        detached.append(entry)
    result['未接入主产品'] = detached
    result['无法确定'] = undetermined
    return result


def analyze_page(page_bytes, registry, page_digest):
    """解释一页的全部脚本,返回 (分析结果, 原始脚本列表)。

    每个业务字段都带来源(脚本序号、行号、JSON 路径、脚本摘要),可回到原文。
    """
    html = page_bytes.decode('utf-8', errors='replace')
    scripts = extract(html)
    info = document_info(html)
    expander = _Expander(registry, info['base'] or info['canonical'], page_digest)
    script_entries = []
    for index, script in enumerate(scripts):
        digest = hashlib.sha256(script['raw'].encode('utf-8')).hexdigest()
        script_entries.append({'序号': index, '行号': script['line'], '摘要': digest})
        if 'error' in script:
            expander.script_problems.append({'脚本': index, '行号': script['line'],
                                             '问题': 'JSON 解析失败', '详情': script['error']})
            continue
        expander.expand_script(script, {'脚本': index, '行号': script['line'], '脚本摘要': digest})
    result = analyze_graph(expander.graph, info['canonical'])
    result['规范地址'] = info['canonical']
    result['原始脚本'] = script_entries
    result['上下文问题'] = expander.context_problems
    result['脚本问题'] = expander.script_problems
    result['图问题'] = expander.graph_problems
    return result, scripts
