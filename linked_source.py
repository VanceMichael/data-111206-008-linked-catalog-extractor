import json
from html.parser import HTMLParser


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.results = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'script' and (attributes.get('type') or '').split(';')[0].strip().lower() == 'application/ld+json':
            self.current = {'line': self.getpos()[0], 'column': self.getpos()[1], 'raw': ''}

    def handle_data(self, data):
        if self.current is not None:
            self.current['raw'] += data

    def handle_endtag(self, tag):
        if tag == 'script' and self.current is not None:
            try:
                self.current['value'] = json.loads(self.current['raw'])
            except json.JSONDecodeError as error:
                self.current['error'] = str(error)
            self.results.append(self.current)
            self.current = None


def extract(html):
    parser = Scripts()
    parser.feed(html)
    if parser.current is not None:
        raise ValueError('JSON-LD 脚本未结束')
    return parser.results
