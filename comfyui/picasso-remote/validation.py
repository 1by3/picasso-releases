# Picasso remote downloads for ComfyUI.
# Part of Picasso (https://github.com/1by3/picasso-releases).
"""Everything the helper checks before it touches the network or the disk.

This module is pure: it imports nothing from ComfyUI, so the rules can be tested on their own
(`python -m unittest discover -s tests` from this folder). The desktop app sends links, file names
and checksums it took from its own model list, but this code never trusts that: every request is
checked again here, because anyone who can reach the ComfyUI server can send the same request.
"""

import os
import re
from urllib.parse import urlsplit

# Hosts files may come from. Mirrors the hosts the Picasso desktop app allows for its model list:
# Hugging Face (its large files are served from the hf.co CDN and the xet bridge under it) and
# GitHub release assets. Civitai is not here and never will be: it needs a login.
ALLOWED_HOSTS = (
    "huggingface.co",
    "*.huggingface.co",
    "hf.co",
    "*.hf.co",
    "github.com",
    "*.githubusercontent.com",
)

# Only these get the Hugging Face token, never a CDN a redirect leads to.
TOKEN_HOSTS = ("huggingface.co", "*.huggingface.co")

# Model files only. Nothing a Python process or a shell could run.
ALLOWED_EXTENSIONS = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")

# Folders that are never download targets, whatever folder_paths lists.
FORBIDDEN_FOLDERS = ("custom_nodes",)

MAX_FILENAME = 200
MAX_URL = 2048
MAX_SIZE_BYTES = 200 * 1024**3
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+()\- ]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ValidationError(Exception):
    """A request the helper refuses. `code` uses the same words as the Picasso app's errors."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def host_matches(host, patterns):
    host = host.lower().rstrip(".")
    for pattern in patterns:
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and len(host) > len(pattern) - 1:
                return True
        elif host == pattern:
            return True
    return False


def check_url(url, patterns=ALLOWED_HOSTS):
    """Return the parsed URL when it is https, has no credentials and the host is allowed."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        raise ValidationError("notAllowed", "not a link")
    if any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValidationError("notAllowed", "not a link")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 (raises ValueError for a bad port)
    except ValueError:
        raise ValidationError("notAllowed", "not a link") from None
    if parts.scheme != "https":
        raise ValidationError("insecure", "not https")
    if parts.username is not None or parts.password is not None:
        raise ValidationError("notAllowed", "credentials in link")
    if not host or not host_matches(host, patterns):
        raise ValidationError("notAllowed", "host not allowed: %s" % (host or ""))
    return parts


def token_allowed_for(url, patterns=TOKEN_HOSTS):
    host = urlsplit(url).hostname or ""
    return host_matches(host, patterns)


def check_folder(folder, known):
    """`folder` must be one of ComfyUI's model folder names (`known`) and not a forbidden one."""
    if not isinstance(folder, str) or folder not in known or folder in FORBIDDEN_FOLDERS:
        raise ValidationError("notAllowed", "unknown model folder")
    return folder


def check_filename(filename):
    """A plain file name: no folders, no dots-only parts, a model file extension."""
    if not isinstance(filename, str) or not filename or len(filename) > MAX_FILENAME:
        raise ValidationError("notAllowed", "bad file name")
    if "/" in filename or "\\" in filename or ":" in filename or "\x00" in filename:
        raise ValidationError("notAllowed", "file name has a path")
    if filename in (".", "..") or ".." in filename or filename != filename.strip():
        raise ValidationError("notAllowed", "bad file name")
    if filename.endswith(".") or filename.endswith(".part"):
        raise ValidationError("notAllowed", "bad file name")
    if not _SAFE_NAME.match(filename) or _WINDOWS_RESERVED.match(filename):
        raise ValidationError("notAllowed", "bad file name")
    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise ValidationError("notAllowed", "not a model file")
    return filename


def check_size(size):
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > MAX_SIZE_BYTES:
        raise ValidationError("size", "size must be a positive whole number of bytes")
    return size


def check_sha256(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _SHA256.match(value.lower()):
        raise ValidationError("hash", "checksum must be 64 hex characters")
    return value.lower()


def check_headers(headers):
    """Only an Authorization bearer token is accepted; nothing else is passed on to a download."""
    if headers is None:
        return {}
    if not isinstance(headers, dict) or len(headers) > 1:
        raise ValidationError("notAllowed", "headers not accepted")
    out = {}
    for name, value in headers.items():
        if not isinstance(name, str) or name.lower() != "authorization":
            raise ValidationError("notAllowed", "header not accepted")
        if (
            not isinstance(value, str)
            or not value.startswith("Bearer ")
            or len(value) > 512
            or not value.isascii()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValidationError("notAllowed", "bad authorization value")
        out["Authorization"] = value
    return out


def choose_folder_path(folder, paths):
    """The directory a folder's files go in: the one named like the folder, else the first.

    ComfyUI lists legacy names first for some folders (`unet` before `diffusion_models`); the
    Picasso app writes to `models/<folder>`, so the one named like the folder wins.
    """
    for path in paths:
        if os.path.basename(os.path.normpath(path)) == folder:
            return path
    if not paths:
        raise ValidationError("notAllowed", "folder has no path")
    return paths[0]


def resolve_destination(directory, filename):
    """The full path of a new file, checked to be directly inside `directory`.

    Raises when it would land anywhere else (a `..` that slipped through, a link that leads out of
    the folder). Does not check whether the file exists.
    """
    check_filename(filename)
    root = os.path.realpath(directory)
    dest = os.path.realpath(os.path.join(root, filename))
    if os.path.dirname(dest) != root:
        raise ValidationError("outsideFolder", "destination is outside the model folder")
    return dest
