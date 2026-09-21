import json
from pathlib import Path
text=Path('output/playwright/browser-cloud.log').read_text()
assert text.startswith('### Result\n'), text[-2000:]
results=json.loads(text.split('### Result\n',1)[1].split('\n### ',1)[0])
assert len(results)==10, results
Path('output/playwright/browser-results.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results,indent=2))
assert all(r['passed'] for r in results), 'Browser runtime checks failed'
