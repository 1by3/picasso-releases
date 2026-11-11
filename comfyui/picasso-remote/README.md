# Picasso Remote Downloads (ComfyUI helper)

A small ComfyUI custom node package that lets the [Picasso](https://github.com/1by3/picasso-releases) desktop app tell a ComfyUI **on another computer** (a Runpod pod, a home server, a rented GPU) to download its own model files. The files go from the source (Hugging Face, GitHub releases) straight to the server's disk. They never pass through your computer.

Without it, Picasso still works with a remote ComfyUI, but you have to put model files in the server's folders yourself. With it, **Download Missing Files** works for that server, with progress, pause, resume and cancel in the Jobs drawer.

It adds no nodes and needs no pip packages (it uses `aiohttp`, which ComfyUI already installs). Python 3.9 or newer.

## Install

Pick the one that fits. Then **restart ComfyUI**, and in Picasso open Settings, Local Models and choose **Check Again** (or reopen the Download Missing Files dialog).

### On a Runpod pod (or any Linux terminal)

Open the pod's terminal (Connect, then Web Terminal or Jupyter terminal) and run this one line. Change `COMFY` if ComfyUI lives somewhere else (`/workspace/runpod-slim/ComfyUI` and `/workspace/ComfyUI` are common):

```sh
COMFY=/workspace/ComfyUI; mkdir -p "$COMFY/custom_nodes" && curl -fsSL https://github.com/1by3/picasso-releases/archive/refs/heads/main.tar.gz | tar -xz --strip-components=2 -C "$COMFY/custom_nodes" picasso-releases-main/comfyui/picasso-remote && ls "$COMFY/custom_nodes/picasso-remote"
```

Then restart ComfyUI (on Runpod templates: restart the ComfyUI service from the template's page, or stop and start the process). The ComfyUI log shows `[picasso-remote] ready` at startup.

### With git

```sh
cd ComfyUI/custom_nodes
git clone --depth 1 --filter=blob:none --sparse https://github.com/1by3/picasso-releases.git picasso-src.disabled
git -C picasso-src.disabled sparse-checkout set comfyui/picasso-remote
ln -s picasso-src.disabled/comfyui/picasso-remote picasso-remote
```

(The `.disabled` suffix makes ComfyUI skip the clone itself; it loads the `picasso-remote` link.)

### By hand

Download or clone the [picasso-releases](https://github.com/1by3/picasso-releases) repository and copy the `comfyui/picasso-remote` folder into `ComfyUI/custom_nodes/`, so that `ComfyUI/custom_nodes/picasso-remote/__init__.py` exists.

## Update and remove

Run the install line again to update (it replaces the files), then restart ComfyUI. To remove, delete the `picasso-remote` folder and restart.

## What it lets Picasso do

The routes are under `/picasso/v1` (also under `/api/picasso/v1`, like every ComfyUI route). Picasso sends its usual sign-in header for the server with each request, so a server behind a password or proxy keeps working.

| Route                                   | Purpose                                                                                                                               |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /picasso/v1/info`                  | Helper name and version, the model folders of this ComfyUI, free disk space.                                                          |
| `POST /picasso/v1/downloads`            | Start a download: `{ url, folder, filename, sizeBytes, sha256?, headers? }`. Returns a job. The same file asked for twice is one job. |
| `GET /picasso/v1/downloads`             | All jobs with progress. `GET /picasso/v1/downloads/<id>` shows one.                                                                   |
| `POST /picasso/v1/downloads/<id>/pause` | Stop and keep the partial file. Asking for the same file again continues where it stopped.                                            |
| `DELETE /picasso/v1/downloads/<id>`     | Cancel and delete the partial file (or forget a finished job).                                                                        |

Requests that change anything must carry an `X-Picasso-Client` header. That is what stops a web page open in your browser from starting downloads on a ComfyUI it can reach: browsers do not allow a page from another site to add that header without the server's permission.

## What it refuses

Anyone who can reach your ComfyUI can send these requests, so the helper checks every one itself and does not rely on what Picasso says:

- **Where from.** Only `https` links on `huggingface.co`, `*.huggingface.co`, `hf.co`, `*.hf.co`, `github.com` and `*.githubusercontent.com`. Redirects are followed by hand and every hop is checked against the same list. Links with a user name or password are refused. Civitai and other sites are never allowed (they need a login, so Picasso gives you manual steps instead).
- **Where to.** The folder must be one of ComfyUI's model folders (`checkpoints`, `vae`, `loras`, `diffusion_models`, and so on; never `custom_nodes`). The file name must be a plain name with a model extension (`.safetensors`, `.sft`, `.ckpt`, `.pt`, `.pth`, `.bin`, `.gguf`, `.onnx`): no folders, no `..`, no `.py`. The final path is resolved and must be directly inside the model folder.
- **Never overwrite.** A file that already exists in any of the folder's paths is never replaced. A download is written to `<name>.part`, its size and (when Picasso sends one) its SHA-256 are checked, and only then is it moved into place.
- **Limits.** Two downloads run at a time, up to 32 wait, a file may be at most 200 GB, and a download is refused when the disk has less free space than the file needs.
- **The Hugging Face token.** Picasso sends one only when you turned on "Send my Hugging Face token to this server" for that server. The helper accepts only a single `Authorization: Bearer ...` header, uses it only for `huggingface.co` itself, never for a CDN a redirect leads to, never writes it to disk and never logs it.

What it does not do: it has no sign-in of its own (neither has ComfyUI). Whoever can reach your ComfyUI can make it download any allowed public file into its model folders, until the disk is full. If that matters, keep the server behind a password, a proxy or a private network such as Tailscale.

## Tests

The rules and the download engine are tested without ComfyUI (`aiohttp` is needed; ComfyUI's own Python has it):

```sh
cd comfyui/picasso-remote
python -m unittest discover -s tests
```

`validation.py` is pure Python. `downloader.py` and `routes.py` need only `aiohttp`. `__init__.py` is the only file that touches ComfyUI (`server.PromptServer`, `folder_paths`).
