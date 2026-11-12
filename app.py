import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from raw_store import RawStore
from semantics import ContextRegistry, analyze_page

ANALYSIS_TAG = '#analysis'


def change_state(store, source, page_digest, context_digest):
    """对比同一来源上一次解析,区分正文变化与语义解释变化。"""
    previous = [row for row in store.observations() if row['source'] == source]
    if not previous:
        return '首次解析'
    latest = max(previous, key=lambda row: (row['last_seen'], row['digest']))
    metadata = json.loads(latest['metadata'])
    same_page = metadata.get('页面摘要') == page_digest
    same_context = metadata.get('上下文摘要') == context_digest
    if same_page and same_context:
        return '无变化'
    if same_page:
        return '语义解释变化'
    if same_context:
        return '正文变化'
    return '正文与语义解释均变化'


def collect_page(store, source, raw, registry, seen_at):
    page_digest = hashlib.sha256(raw).hexdigest()
    analysis, scripts = analyze_page(raw, registry, page_digest)
    store.save(source, raw, {'script_count': len(scripts)}, seen_at)
    for info in analysis['原始脚本']:
        body = scripts[info['序号']]['raw'].encode('utf-8')
        store.save(f"{source}#script[{info['序号']}]", body, {'行号': info['行号']}, seen_at)
    change = change_state(store, f'{source}{ANALYSIS_TAG}', page_digest, registry.digest)
    body = json.dumps(analysis, ensure_ascii=False, sort_keys=True).encode('utf-8')
    store.save(f'{source}{ANALYSIS_TAG}', body,
               {'页面摘要': page_digest, '上下文摘要': registry.digest}, seen_at)
    return {'页面摘要': page_digest, '上下文摘要': registry.digest, '变化': change, '分析': analysis}


def demo(store, registry, fixtures):
    seen_at = datetime.now(timezone.utc).isoformat()
    pages = []
    for path in sorted(Path(fixtures).glob('*.html')):
        pages.append({'文件': path.name,
                      **collect_page(store, path.name, path.read_bytes(), registry, seen_at)})
    return {'上下文登记摘要': registry.digest, '页面': pages,
            '说明': '报价仅在价格与币种唯一且无冲突时可发布;缺少币种不默认任何币种'}


def analyze(store, registry):
    """用当前上下文登记重新解析库中原页,供上下文副本更新后比对。"""
    seen_at = datetime.now(timezone.utc).isoformat()
    latest = {}
    for observation in store.observations():
        source = observation['source']
        if '#' in source:
            continue
        if source not in latest or observation['last_seen'] >= latest[source]['last_seen']:
            latest[source] = observation
    pages = []
    for source in sorted(latest):
        raw = store.read(latest[source]['digest'])
        pages.append({'文件': source, **collect_page(store, source, raw, registry, seen_at)})
    return {'上下文登记摘要': registry.digest, '页面': pages,
            '说明': '按当前上下文登记重新解析库中原页,用于区分正文变化与语义解释变化'}


def main():
    parser = argparse.ArgumentParser(description='本地原文采集与可追溯的产品报价分析')
    parser.add_argument('command', choices=['demo', 'analyze', 'show'])
    parser.add_argument('digest', nargs='?', help='show 命令要查看的对象摘要')
    parser.add_argument('--output', default='output/raw.sqlite3')
    parser.add_argument('--contexts', default='fixtures/contexts.json')
    parser.add_argument('--fixtures', default='fixtures')
    args = parser.parse_args()
    store = RawStore(args.output)
    try:
        if args.command == 'show':
            if not args.digest:
                parser.error('show 需要摘要参数')
            try:
                body = store.read(args.digest)
            except KeyError:
                raise SystemExit(f'找不到摘要对应的对象: {args.digest}')
            sys.stdout.buffer.write(body)
            sys.stdout.buffer.write(b'\n')
            return
        if args.digest:
            parser.error('仅 show 命令接受摘要参数')
        registry = ContextRegistry(args.contexts)
        result = demo(store, registry, args.fixtures) if args.command == 'demo' else analyze(store, registry)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
