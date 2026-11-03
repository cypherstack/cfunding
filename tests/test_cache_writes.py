"""Exercise the real cache helpers/routes, with temp files and fake HTTP only.

No AST extraction. Isolated package namespaces avoid importing the application's
factory, database and wallet. Coin length policy and unrelated web-framework
imports are faked; QR rendering uses the real Pillow/PyQRCode dependencies.
Run with the app's aiohttp, Pillow, PyQRCode and pypng dependencies installed.
"""
import asyncio
import importlib
from io import BytesIO
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
HASH = "0123456789abcdef0123456789abcdef"
ADDRESS = "a" + "1" * 33


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class BadRequest(Exception):
    pass


class Blueprint:
    def __init__(self, *args, **kwargs):
        pass

    def route(self, *args, **kwargs):
        return lambda function: function

    get = post = route


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    return output.getvalue()


class CacheFixtures:
    def setUp(self):
        # Python 3.9 binds module-level asyncio locks at import time. Synchronous
        # cases need their own loop; async cases already have an isolated one.
        if not isinstance(self, unittest.IsolatedAsyncioTestCase):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.addCleanup(asyncio.set_event_loop, None)
            self.addCleanup(loop.close)
        temporary = tempfile.TemporaryDirectory(prefix="cfunding-cache-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.static = self.root / "funding" / "static"
        self.static.mkdir(parents=True)
        self.directory = self.static / "gravatar"
        self.directory.mkdir()
        self.web_cache = SimpleNamespace(get=AsyncMock(), set=AsyncMock())
        self.web_cache.get.return_value = None
        self.app = SimpleNamespace(static_folder=str(self.static),
                                   session_interface=self.web_cache, logger=Mock())
        self.send = AsyncMock(return_value="image response")
        self.abort = Mock(side_effect=BadRequest)
        # This is the existing configured-length policy, not a checksum check.
        self.validate_address = Mock(side_effect=lambda address: len(address) == 34)
        identity = lambda function: function
        schema_decorator = lambda *args, **kwargs: identity
        namespaces = {
            "funding": module("funding", __path__=[str(ROOT / "funding")],
                              login_required=identity),
            "funding.utils": module("funding.utils", __path__=[str(ROOT / "funding/utils")],
                                    validate_crypto_address=self.validate_address,
                                    get_ip=lambda: "test-client"),
            "settings": module("settings", cwd=str(self.root)),
            "quart": module("quart", session={}, request=None, current_app=self.app,
                            render_template=AsyncMock(), redirect=Mock(), url_for=Mock(),
                            jsonify=Mock(), Blueprint=Blueprint, abort=self.abort,
                            flash=AsyncMock(), send_from_directory=self.send,
                            make_response=Mock(), Response=Mock(), send_file=Mock(), g=None),
            "quart_schema": module("quart_schema", QuartSchema=Mock(),
                                   validate_request=schema_decorator,
                                   validate_response=schema_decorator),
            "quart_schema.validation": module("quart_schema.validation",
                                              DataSource=SimpleNamespace(JSON="json", FORM="form")),
            "pydantic": module("pydantic", BaseModel=object, EmailStr=str),
            "funding.factory": module("funding.factory", openid=None),
            "funding.utils.crumbs": module("funding.utils.crumbs"),
            "funding.utils.markdown": module("funding.utils.markdown", generate_html=Mock()),
            "funding.models": module("funding.models", __path__=[]),
            "funding.models.database": module("funding.models.database", User=object,
                                              Proposal=object, ProposalStatus=object,
                                              ProposalCategory=object),
        }
        namespace_patch = patch.dict(sys.modules, namespaces)
        namespace_patch.start()
        self.addCleanup(namespace_patch.stop)
        self.cache = importlib.import_module("funding.utils.cache")
        self.gravatar = importlib.import_module("funding.utils.gravatar")
        self.qr = importlib.import_module("funding.utils.qr")
        self.routes = importlib.import_module("funding.routes")
        # Keep lock ownership local to each isolated async test.
        self.gravatar.GRAVATAR_LOCK = asyncio.Lock()
        self.qr.QR_LOCK = asyncio.Lock()
        self.routes.QR_LOCK = self.qr.QR_LOCK


class CachePathTests(CacheFixtures, unittest.TestCase):
    def test_atomic_write_and_replace(self):
        destination = self.directory / "avatar.png"
        destination.write_bytes(b"old")
        result = self.cache.write_cache_file(str(self.directory), "avatar.png", b"new")
        self.assertEqual(result, str(destination))
        self.assertEqual(destination.read_bytes(), b"new")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
        self.assertEqual(list(self.directory.iterdir()), [destination])

    def test_trusted_cache_directory_symlink_resolves_to_its_real_directory(self):
        alias = self.root / "cache-alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        result = self.cache.write_cache_file(str(alias), "avatar.png", b"new")
        self.assertEqual(result, str(self.directory / "avatar.png"))
        self.assertEqual(Path(result).read_bytes(), b"new")

    def test_rejects_paths_and_non_ascii_filenames(self):
        names = ["../outside.png", "/tmp/outside.png", "..\\outside.png", "a/b.png",
                 "a\\b.png", "a%2fb.png", "a?.png", "a\x00.png", "а.png",
                 "a\n.png", "a.png/", "a" * 256 + ".png"]
        for name in names:
            with self.subTest(filename=name):
                with self.assertRaises(ValueError):
                    self.cache.write_cache_file(str(self.directory), name, b"bad")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_existing_symlink_is_rejected_without_touching_target(self):
        target = self.root / "outside.png"
        target.write_bytes(b"preserve")
        (self.directory / "avatar.png").symlink_to(target)
        with self.assertRaises(ValueError):
            self.cache.write_cache_file(str(self.directory), "avatar.png", b"bad")
        self.assertEqual(target.read_bytes(), b"preserve")

    def test_dangling_symlink_is_rejected(self):
        target = self.root / "absent.png"
        (self.directory / "avatar.png").symlink_to(target)
        with self.assertRaises(ValueError):
            self.cache.cache_file_path(str(self.directory), "avatar.png")
        self.assertFalse(target.exists())

    def test_symlink_race_replaces_link_not_its_target(self):
        target = self.root / "outside.png"
        target.write_bytes(b"preserve")
        destination = self.directory / "avatar.png"
        replace = os.replace

        def introduce_link(temporary, output):
            destination.symlink_to(target)
            replace(temporary, output)

        with patch.object(self.cache.os, "replace", side_effect=introduce_link):
            self.cache.write_cache_file(str(self.directory), "avatar.png", b"new")
        self.assertEqual(target.read_bytes(), b"preserve")
        self.assertFalse(destination.is_symlink())
        self.assertEqual(destination.read_bytes(), b"new")

    def test_failed_replace_cleans_temp_and_preserves_old_cache(self):
        destination = self.directory / "avatar.png"
        destination.write_bytes(b"old")
        with patch.object(self.cache.os, "replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                self.cache.write_cache_file(str(self.directory), "avatar.png", b"new")
        self.assertEqual(destination.read_bytes(), b"old")
        self.assertEqual(list(self.directory.iterdir()), [destination])


class QRWriteTests(CacheFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.generator = self.qr.QrCodeGenerator()
        self.base = Path(self.generator.base)

    def test_invalid_addresses_rejected_before_render_or_cache_lookup(self):
        addresses = [None, 123, "", "a" * 33, "a" * 35,
                     "../" + "a" * 31, "/" + "a" * 33, "\\" + "a" * 33,
                     "a" * 33 + ".", "a" * 33 + "%", "a" * 33 + "\n", "а" * 34]
        with patch.object(self.qr.pyqrcode, "create") as render:
            for address in addresses:
                with self.subTest(address=address):
                    with self.assertRaises(ValueError):
                        self.generator.exists(address)
                    with self.assertRaises(ValueError):
                        self.generator.create(address)
            render.assert_not_called()
        self.assertEqual(list(self.base.iterdir()), [])

    def test_cached_traversal_does_not_bypass_validation(self):
        address = "../" + "a" * 31  # Correct configured length, unsafe path.
        target = self.base.parent / ("a" * 31 + ".png")
        target.write_bytes(b"preserve")
        with self.assertRaises(ValueError):
            self.generator.exists(address)
        with self.assertRaises(ValueError):
            self.generator.create(address)
        self.assertEqual(target.read_bytes(), b"preserve")

    def test_traversal_payload_cannot_create_an_outside_png(self):
        address = "../" + "a" * 31
        target = self.base.parent / ("a" * 31 + ".png")
        try:
            self.generator.create(address)
        except ValueError:
            pass
        self.assertFalse(target.exists())

    def test_explicit_destination_cannot_escape_cache(self):
        target = self.root / "outside.png"
        target.write_bytes(b"preserve")
        with patch.object(self.qr.pyqrcode, "create") as render:
            with self.assertRaises(ValueError):
                self.generator.create(ADDRESS, dest=str(target))
            render.assert_not_called()
        self.assertEqual(target.read_bytes(), b"preserve")

    def test_destination_symlink_cannot_overwrite_another_file(self):
        target = self.root / "outside.png"
        target.write_bytes(b"preserve")
        (self.base / (ADDRESS + ".png")).symlink_to(target)
        with self.assertRaises(ValueError):
            self.generator.exists(ADDRESS)
        with self.assertRaises(ValueError):
            self.generator.create(ADDRESS)
        self.assertEqual(target.read_bytes(), b"preserve")

    def test_real_gradient_qr_is_a_readable_png_and_cache_hit(self):
        result = self.generator.create(ADDRESS)
        self.assertEqual(result, str(self.base / (ADDRESS + ".png")))
        self.assertTrue(self.generator.exists(ADDRESS))
        with Image.open(result) as image:
            self.assertEqual(image.format, "PNG")
            self.assertLessEqual(max(image.size), 300)
        self.assertFalse(any(p.name.startswith(".cache-") for p in self.base.iterdir()))

    def test_plain_qr_and_explicit_contained_destination(self):
        target = self.base / "custom.png"
        result = self.generator.create(ADDRESS, dest=str(target), color_from=None, color_to=None)
        self.assertEqual(result, str(target))
        with Image.open(result) as image:
            self.assertEqual(image.format, "PNG")

    def test_explicit_destination_through_trusted_directory_alias(self):
        alias = self.root / "qr-alias"
        alias.symlink_to(self.base, target_is_directory=True)
        result = self.generator.create(ADDRESS, dest=str(alias / "custom.png"))
        self.assertEqual(result, str(self.base / "custom.png"))
        self.assertTrue(Path(result).is_file())

    def test_ascii_alphanumeric_addresses_are_not_limited_to_base58(self):
        address = "sm" + "0l" * 16  # Preserve characters also used by Bech32-like addresses.
        self.assertEqual(len(address), 34)
        result = self.generator.create(address)
        self.assertTrue(Path(result).is_file())


class Response:
    def __init__(self, chunks, status=200, headers=None, length=None, failure=None):
        self.status = status
        self.headers = {"content-type": "image/png"} if headers is None else headers
        self.content_length = length
        self.content = self
        self.chunks = chunks
        self.failure = failure
        self.iterated = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self):
        # Also supports running the directory-escape regression on the old helper.
        if self.failure:
            raise self.failure
        return b"".join(self.chunks)

    async def iter_chunked(self, size):
        self.iterated = True
        for chunk in self.chunks:
            for offset in range(0, len(chunk), size):
                yield chunk[offset:offset + size]
        if self.failure:
            raise self.failure


class Session:
    def __init__(self, response):
        self.get = Mock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class GravatarWriteTests(CacheFixtures, unittest.IsolatedAsyncioTestCase):
    async def download(self, response, hash=HASH, filename=None):
        self.session = Session(response)
        with patch.object(self.gravatar.aiohttp, "ClientSession", return_value=self.session):
            return await self.gravatar.gravatar_download(
                hash, str(self.directory), filename if filename is not None else HASH + ".png")

    async def test_valid_hash_downloads_and_caches_real_image(self):
        image = image_bytes()
        result = await self.download(Response([image], length=len(image)), hash=HASH.upper())
        self.session.get.assert_called_once_with("https://www.gravatar.com/avatar/" + HASH)
        self.assertEqual(result, str(self.directory / (HASH + ".png")))
        self.assertEqual(Path(result).read_bytes(), image)
        with Image.open(result) as saved:
            self.assertEqual(saved.format, "PNG")

    async def test_malformed_hashes_do_not_start_http_or_write(self):
        values = [None, "", "a" * 31, "a" * 33, "g" * 32, "../" + "a" * 29,
                  "/" + "a" * 31, "a" * 31 + "\\", "a" * 31 + "\n", "а" * 32]
        with patch.object(self.gravatar.aiohttp, "ClientSession") as client:
            for value in values:
                with self.subTest(hash=value):
                    with self.assertRaises(ValueError):
                        await self.gravatar.gravatar_download(value, str(self.directory), HASH + ".png")
            client.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_valid_hash_cannot_supply_a_different_or_traversing_filename(self):
        with patch.object(self.gravatar.aiohttp, "ClientSession") as client:
            for filename in ["../outside.png", "/tmp/outside.png", "other.png"]:
                with self.subTest(filename=filename):
                    with self.assertRaises(ValueError):
                        await self.gravatar.gravatar_download(HASH, str(self.directory), filename)
            client.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_traversal_payload_cannot_write_outside_cache(self):
        target = self.static / "outside.png"
        try:
            await self.download(Response([image_bytes()]), filename="../outside.png")
        except ValueError:
            pass
        self.assertFalse(target.exists())

    async def test_symlink_destination_rejected_before_http(self):
        target = self.root / "outside.png"
        target.write_bytes(b"preserve")
        (self.directory / (HASH + ".png")).symlink_to(target)
        with patch.object(self.gravatar.aiohttp, "ClientSession") as client:
            with self.assertRaises(ValueError):
                await self.gravatar.gravatar_download(HASH, str(self.directory), HASH + ".png")
            client.assert_not_called()
        self.assertEqual(target.read_bytes(), b"preserve")

    async def test_bad_status_or_missing_non_image_content_type_not_cached(self):
        responses = [Response([b"bad"], status=404), Response([b"bad"], headers={}),
                     Response([b"bad"], headers={"content-type": "text/html"})]
        for response in responses:
            with self.subTest(status=response.status, headers=response.headers):
                with self.assertRaises(RuntimeError):
                    await self.download(response)
                self.assertFalse(response.iterated)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_advertised_oversized_image_rejected_before_streaming(self):
        response = Response([b"x"], length=self.gravatar.GRAVATAR_MAX_BYTES + 1)
        with self.assertRaisesRegex(RuntimeError, "size limit"):
            await self.download(response)
        self.assertFalse(response.iterated)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_chunked_or_underreported_oversized_body_not_cached(self):
        for length in (None, 1):
            with self.subTest(length=length):
                response = Response([b"x" * self.gravatar.GRAVATAR_MAX_BYTES, b"x"], length=length)
                with self.assertRaisesRegex(RuntimeError, "size limit"):
                    await self.download(response)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_exact_size_limit_is_accepted(self):
        result = await self.download(Response([b"x" * self.gravatar.GRAVATAR_MAX_BYTES]))
        self.assertEqual(Path(result).stat().st_size, self.gravatar.GRAVATAR_MAX_BYTES)

    async def test_empty_body_is_not_cached(self):
        with self.assertRaisesRegex(RuntimeError, "Empty"):
            await self.download(Response([]))
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_interrupted_download_preserves_existing_file(self):
        target = self.directory / (HASH + ".png")
        target.write_bytes(b"old")
        with self.assertRaises(OSError):
            await self.download(Response([b"partial"], failure=OSError("disconnected")))
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(list(self.directory.iterdir()), [target])


class CacheRouteTests(CacheFixtures, unittest.IsolatedAsyncioTestCase):
    async def test_bad_gravatar_hashes_use_default_without_download(self):
        for value in [None, "../outside", "/tmp/outside", "g" * 32]:
            with self.subTest(hash=value):
                with patch.object(self.routes, "gravatar_download", new_callable=AsyncMock) as fetch:
                    await self.routes.utils_gravatar(value)
                    fetch.assert_not_awaited()
                self.send.assert_awaited_with(directory=str(self.static), file_name="user.png")
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_uppercase_gravatar_uses_canonical_cached_file(self):
        (self.directory / (HASH + ".png")).write_bytes(image_bytes())
        with patch.object(self.routes, "gravatar_download", new_callable=AsyncMock) as fetch:
            await self.routes.utils_gravatar(HASH.upper())
            fetch.assert_not_awaited()
        self.send.assert_awaited_once_with(directory=str(self.directory), file_name=HASH + ".png")

    async def test_gravatar_cache_miss_downloads_to_canonical_path(self):
        session = Session(Response([image_bytes()]))
        with patch.object(self.gravatar.aiohttp, "ClientSession", return_value=session):
            await self.routes.utils_gravatar(HASH.upper())
        self.assertTrue((self.directory / (HASH + ".png")).is_file())
        session.get.assert_called_once_with("https://www.gravatar.com/avatar/" + HASH)
        self.send.assert_awaited_once_with(directory=str(self.directory), file_name=HASH + ".png")

    async def test_oversized_gravatar_falls_back_without_cache_file(self):
        session = Session(Response([b"x"], length=self.gravatar.GRAVATAR_MAX_BYTES + 1))
        with patch.object(self.gravatar.aiohttp, "ClientSession", return_value=session):
            await self.routes.utils_gravatar(HASH)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.send.assert_awaited_once_with(directory=str(self.static), file_name="user.png")

    async def test_gravatar_symlink_cache_hit_falls_back_to_default(self):
        target = self.root / "outside.png"
        target.write_bytes(b"preserve")
        (self.directory / (HASH + ".png")).symlink_to(target)
        with patch.object(self.routes, "gravatar_download", new_callable=AsyncMock) as fetch:
            await self.routes.utils_gravatar(HASH)
            fetch.assert_not_awaited()
        self.send.assert_awaited_once_with(directory=str(self.static), file_name="user.png")
        self.assertEqual(target.read_bytes(), b"preserve")

    async def test_qr_traversal_returns_400_before_cache_or_render(self):
        with patch.object(self.qr.pyqrcode, "create") as render:
            with self.assertRaises(BadRequest):
                await self.routes.utils_qr_image("../" + "a" * 31)
            render.assert_not_called()
        self.abort.assert_called_once_with(400)
        self.web_cache.get.assert_not_awaited()
        self.web_cache.set.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_qr_symlink_cache_hit_returns_400(self):
        base = self.static / "qr"
        base.mkdir()
        target = self.root / "outside.png"
        target.write_bytes(b"preserve")
        (base / (ADDRESS + ".png")).symlink_to(target)
        with self.assertRaises(BadRequest):
            await self.routes.utils_qr_image(ADDRESS)
        self.abort.assert_called_once_with(400)
        self.send.assert_not_awaited()
        self.assertEqual(target.read_bytes(), b"preserve")

    async def test_normal_qr_generation_and_cache_hit(self):
        await self.routes.utils_qr_image(ADDRESS)
        self.web_cache.set.assert_awaited_once()
        with Image.open(self.static / "qr" / (ADDRESS + ".png")) as image:
            self.assertEqual(image.format, "PNG")
        with patch.object(self.qr.pyqrcode, "create") as render:
            await self.routes.utils_qr_image(ADDRESS)
            render.assert_not_called()
        self.send.assert_awaited_with(str(self.static / "qr"), ADDRESS + ".png")


if __name__ == "__main__":
    unittest.main()
