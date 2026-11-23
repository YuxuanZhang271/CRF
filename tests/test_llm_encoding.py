"""The Windows GBK pipe must preserve Cantonese/Mandarin before tokenization."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ProtocolEncodingTests(unittest.TestCase):
    def test_utf8_protocol_overrides_legacy_worker_stdio(self):
        source = str(Path(__file__).resolve().parents[1] / 'src/llm/streamingcommand_sdk/src')
        code = (
            'import sys,json; '
            f'sys.path.insert(0,{source!r}); '
            'from streamingcommand._runtime.protocol import configure_protocol_stdio; '
            'configure_protocol_stdio(); '
            'value=json.loads(sys.stdin.readline()); '
            "print(json.dumps({'text':value,'encoding':sys.stdin.encoding},ensure_ascii=False))"
        )
        texts = ['嗰個係咩？', '向上望少少。', '你好，停止。', 'Stop now.']
        environment = dict(os.environ, PYTHONUTF8='0', PYTHONIOENCODING='gbk:surrogateescape')
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8=0', '-c', code],
            input=(json.dumps(texts, ensure_ascii=False)+'\n').encode('utf-8'),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        received = json.loads(result.stdout)
        self.assertEqual(received['text'], texts)
        self.assertEqual(received['encoding'], 'utf-8')


if __name__ == '__main__':
    unittest.main()
