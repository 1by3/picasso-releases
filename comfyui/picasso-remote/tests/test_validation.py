import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import loader  # noqa: E402

loader.load()
from picasso_remote import validation as v  # noqa: E402


class UrlTests(unittest.TestCase):
    def test_allowed_hosts(self):
        for url in (
            "https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/resolve/main/a.safetensors",
            "https://cdn-lfs.huggingface.co/repos/ab/cd/file",
            "https://cas-bridge.xethub.hf.co/xet-bridge-us/abc/def?X-Amz=1",
            "https://hf.co/x",
            "https://github.com/owner/repo/releases/download/v1/a.pth",
            "https://objects.githubusercontent.com/github-production-release-asset/1",
            "https://HuggingFace.co/x",
        ):
            self.assertEqual(v.check_url(url).scheme, "https", url)

    def test_refused_hosts(self):
        for url in (
            "https://civitai.com/api/download/models/1",
            "https://evilhuggingface.co/x",
            "https://huggingface.co.evil.example/x",
            "https://huggingface.co@evil.example/x",
            "https://user:pw@huggingface.co/x",
            "https://example.com/x",
            "https://127.0.0.1/x",
            "https://localhost/x",
            "https://githubusercontent.com.evil.example/x",
            "https://notgithub.com/x",
            "https://hf.co.evil.example/x",
            "https://huggingface.co:bad/x",
            "https://huggingface.co/a b",
        ):
            with self.assertRaises(v.ValidationError, msg=url):
                v.check_url(url)

    def test_scheme_must_be_https(self):
        for url in ("http://huggingface.co/x", "ftp://huggingface.co/x", "file:///etc/passwd", "//huggingface.co/x"):
            with self.assertRaises(v.ValidationError, msg=url):
                v.check_url(url)

    def test_not_a_link(self):
        for url in (None, 5, "", "x" * 3000, "https://huggingface.co/\n"):
            with self.assertRaises(v.ValidationError):
                v.check_url(url)

    def test_wildcard_needs_a_subdomain(self):
        self.assertFalse(v.host_matches(".hf.co", ["*.hf.co"]))
        self.assertFalse(v.host_matches("hf.co", ["*.hf.co"]))
        self.assertTrue(v.host_matches("a.hf.co.", ["*.hf.co"]))

    def test_token_hosts(self):
        self.assertTrue(v.token_allowed_for("https://huggingface.co/x"))
        self.assertTrue(v.token_allowed_for("https://cdn-lfs.huggingface.co/x"))
        self.assertFalse(v.token_allowed_for("https://cas-bridge.xethub.hf.co/x"))
        self.assertFalse(v.token_allowed_for("https://github.com/x"))
        self.assertFalse(v.token_allowed_for("https://evilhuggingface.co/x"))


class NameTests(unittest.TestCase):
    def test_good_names(self):
        for name in (
            "flux-2-klein-4b.safetensors",
            "RealESRGAN_x4plus.pth",
            "qwen_image_2.1_vae_bf16.safetensors",
            "GTM_Krea2_Wowser_v1.1_int8_convrot.safetensors",
            "model (1).gguf",
        ):
            self.assertEqual(v.check_filename(name), name)

    def test_bad_names(self):
        for name in (
            "",
            ".",
            "..",
            "../evil.safetensors",
            "..\\evil.safetensors",
            "sub/evil.safetensors",
            "sub\\evil.safetensors",
            "C:evil.safetensors",
            "/abs.safetensors",
            "a..b.safetensors",
            "evil.py",
            "evil.sh",
            "evil.safetensors.py",
            "model.safetensors.part",
            "model.safetensors.",
            "model.safetensors ",
            " model.safetensors",
            "nul.safetensors",
            "CON",
            "x\x00.safetensors",
            ".hidden.safetensors",
            "a" * 300 + ".safetensors",
            None,
            5,
        ):
            with self.assertRaises(v.ValidationError, msg=repr(name)):
                v.check_filename(name)


class FolderTests(unittest.TestCase):
    known = {"vae": ["/m/vae"], "custom_nodes": ["/c"], "loras": ["/m/loras"]}

    def test_known_folder(self):
        self.assertEqual(v.check_folder("vae", self.known), "vae")

    def test_unknown_or_forbidden(self):
        for folder in ("custom_nodes", "../vae", "nope", "", None, "vae/.."):
            with self.assertRaises(v.ValidationError, msg=repr(folder)):
                v.check_folder(folder, self.known)

    def test_folder_path_prefers_the_matching_name(self):
        paths = ["/m/unet", "/m/diffusion_models"]
        self.assertEqual(v.choose_folder_path("diffusion_models", paths), "/m/diffusion_models")
        self.assertEqual(v.choose_folder_path("other", paths), "/m/unet")
        with self.assertRaises(v.ValidationError):
            v.choose_folder_path("x", [])


class FieldTests(unittest.TestCase):
    def test_size(self):
        self.assertEqual(v.check_size(10), 10)
        for size in (0, -1, 1.5, "10", True, None, v.MAX_SIZE_BYTES + 1):
            with self.assertRaises(v.ValidationError, msg=repr(size)):
                v.check_size(size)

    def test_sha256(self):
        good = "a" * 64
        self.assertEqual(v.check_sha256(good.upper()), good)
        self.assertIsNone(v.check_sha256(None))
        for bad in ("abc", "g" * 64, 5, "a" * 65):
            with self.assertRaises(v.ValidationError, msg=repr(bad)):
                v.check_sha256(bad)

    def test_headers(self):
        self.assertEqual(v.check_headers(None), {})
        self.assertEqual(
            v.check_headers({"authorization": "Bearer hf_abc"}), {"Authorization": "Bearer hf_abc"}
        )
        for bad in (
            {"Cookie": "a=b"},
            {"Authorization": "Basic abc"},
            {"Authorization": "Bearer a\r\nX: y"},
            {"Authorization": "Bearer " + "a" * 600},
            {"Authorization": "Bearer a", "X-Other": "b"},
            [("Authorization", "Bearer a")],
            "Authorization: Bearer a",
        ):
            with self.assertRaises(v.ValidationError, msg=repr(bad)):
                v.check_headers(bad)


class DestinationTests(unittest.TestCase):
    def test_inside_the_folder(self):
        with tempfile.TemporaryDirectory() as d:
            dest = v.resolve_destination(d, "a.safetensors")
            self.assertEqual(os.path.dirname(dest), os.path.realpath(d))

    def test_traversal_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("../a.safetensors", "sub/a.safetensors", "..\\a.safetensors"):
                with self.assertRaises(v.ValidationError, msg=name):
                    v.resolve_destination(d, name)

    def test_link_that_leaves_the_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            target = os.path.join(outside, "x.safetensors")
            open(target, "w").close()
            link = os.path.join(d, "a.safetensors")
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks need privileges on this system")
            with self.assertRaises(v.ValidationError) as ctx:
                v.resolve_destination(d, "a.safetensors")
            self.assertEqual(ctx.exception.code, "outsideFolder")


if __name__ == "__main__":
    unittest.main()
