# Picasso remote downloads for ComfyUI.
# Part of Picasso (https://github.com/1by3/picasso-releases).
"""Lets the Picasso desktop app tell this ComfyUI to download model files itself.

Install by copying this folder into ComfyUI/custom_nodes and restarting ComfyUI (see README.md).
It adds no nodes and no pip packages; it only registers a few routes under /picasso/v1.
"""

import logging
import shutil

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

try:
    from server import PromptServer  # ComfyUI's own module; absent when the tests import this folder
except ImportError:
    PromptServer = None

if PromptServer is not None and getattr(PromptServer, "instance", None) is not None:
    import folder_paths

    from . import validation
    from .downloader import Downloads
    from .routes import add_routes

    def _folders():
        out = {}
        for name in list(folder_paths.folder_names_and_paths):
            if name in validation.FORBIDDEN_FOLDERS:
                continue
            out[name] = list(folder_paths.get_folder_paths(name))
        return out

    def _free_bytes():
        try:
            return shutil.disk_usage(folder_paths.models_dir).free
        except OSError:
            return None

    def _file_added(folder):
        # ComfyUI keeps a list of the files in each folder; make it look again.
        cache = getattr(folder_paths, "filename_list_cache", None)
        if isinstance(cache, dict):
            cache.pop(folder, None)

    _downloads = Downloads(_folders, on_file_added=_file_added)
    add_routes(PromptServer.instance.routes, _downloads, _folders, _free_bytes)
    logging.info("[picasso-remote] ready: Picasso can download model files into this ComfyUI")
