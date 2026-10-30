import importlib.machinery
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))   # ai-jira·ai-approve 가 dispatcher 를 import


def load_script(rel, name):
    """확장자 없는 실행 파일(src/ai-approve 등)을 모듈로 불러온다."""
    loader = importlib.machinery.SourceFileLoader(name, os.path.join(ROOT, rel))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod
