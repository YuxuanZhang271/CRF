"""CPU-only language query, synchronous model calls in the process owner thread."""
import argparse
import base64
import json
from pathlib import Path
import sys
import traceback

protocol_stdout = sys.stdout
sys.stdout = sys.stderr
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def emit(result):
    """Write one flushed JSON reply onto the language worker's protocol stream."""
    print(json.dumps(result), file=protocol_stdout, flush=True)


def main():
    """Load the CPU language helper and handle one classification or control request per line."""
    parser=argparse.ArgumentParser()
    parser.add_argument('--model-dir',type=Path,required=True)
    parser.add_argument('--int8',action='store_true')
    parser.add_argument('--condition-english',action='store_true')
    parser.add_argument('--minimum-language-probability',type=float,default=0.95)
    parser.add_argument('--feature-dither',type=float,default=None)
    args=parser.parse_args()
    import torch
    from streamingcommand._runtime.acoustic_language import AcousticLanguageHint
    torch.set_num_threads(2)
    helper=AcousticLanguageHint('cpu',args.model_dir,int8=args.int8,condition_english=args.condition_english,
        minimum_language_probability=args.minimum_language_probability,feature_dither=args.feature_dither)
    helper._load()
    emit({'ok':True,'event':'ready','device':'cpu','int8':args.int8})
    for line in sys.stdin:
        try:
            request=json.loads(line)
            if request['op']!='classify':raise ValueError('Expected classify')
            hint=helper._predict(base64.b64decode(request['pcm16_b64'],validate=True))
            emit({'ok':True,'op':'classify','hint':hint})
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            emit({'ok':False,'error':str(error)})


if __name__=='__main__':main()
