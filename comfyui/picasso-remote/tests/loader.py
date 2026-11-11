# Loads the helper folder as a package named `picasso_remote` without ComfyUI.
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load():
    if "picasso_remote" in sys.modules:
        return sys.modules["picasso_remote"]
    spec = importlib.util.spec_from_file_location(
        "picasso_remote", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["picasso_remote"] = module
    spec.loader.exec_module(module)
    return module
