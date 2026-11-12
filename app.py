import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from raw_store import RawStore


def demo(store):
    observed_at = datetime.now(timezone.utc).isoformat()
    from linked_source import extract
    result=[]
    for path in sorted(Path('fixtures').glob('*.html')):
        raw=path.read_bytes()
        scripts=extract(raw.decode())
        digest=store.save(path.name,raw,{'script_count':len(scripts)},observed_at)
        result.append({'文件':path.name,'脚本数':len(scripts),'脚本行号':[script['line'] for script in scripts],'原文摘要':digest})
    return {'页面脚本':result,'说明':'未展开上下文或连接语义图'}


def main():
    parser = argparse.ArgumentParser(description='本地原文采集演示')
    parser.add_argument('command', choices=['demo'])
    parser.add_argument('--output', default='output/raw.sqlite3')
    args = parser.parse_args()
    store = RawStore(args.output)
    try:
        result = demo(store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
