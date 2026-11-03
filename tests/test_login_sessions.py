"""Real Quart HTTP cookies and quart-session storage, with SQLite/memory Redis.

No external services or real credentials. Use the application's Python environment
and a local test settings.py; factory startup hooks are deliberately not run.
"""
from http.cookies import SimpleCookie
import unittest
from unittest.mock import AsyncMock, patch

import bcrypt
import peewee
from quart import g, session
from quart_session.sessions import RedisSessionInterface

import settings
from funding import factory
from funding.auth import routes
from funding.models.database import User, PasswordReset, Proposal
from funding.models.enums import UserRole


class MemoryRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, name, value, time):
        self.values[name] = value

    async def delete(self, key):
        self.values.pop(key, None)


class LoginSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = peewee.SqliteDatabase(":memory:")
        self.addCleanup(self.db.close)
        binding = self.db.bind_ctx([User, PasswordReset, Proposal],
                                   bind_refs=False, bind_backrefs=False)
        binding.__enter__()
        self.addCleanup(binding.__exit__, None, None, None)
        self.db.create_tables([User, PasswordReset, Proposal])
        password = bcrypt.hashpw(b"old-password", bcrypt.gensalt(rounds=4)).decode()
        self.user = User.create(username="victim", mail="victim@example.com",
                                password=password, role=UserRole.user)
        self.admin = User.create(username="admin", mail="admin@example.com",
                                 password=password, role=UserRole.admin)
        for target in (
            patch.object(factory, "database", self.db),
            patch.object(routes, "database", self.db),
            patch.object(routes, "mail_enabled", return_value=True),
            patch.object(routes, "render_template", AsyncMock(return_value="form")),
            patch.object(settings, "OPENID_CFG", None),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.app = factory.create_app()
        self.app.config.update(TESTING=True, SERVER_NAME="funding.example",
                               SECRET_KEY="local-session-tests-only")
        self.app.before_serving_funcs.clear()
        self.app.register_blueprint(routes.bp_auth)
        self.redis = MemoryRedis()
        self.app.session_interface = RedisSessionInterface(
            self.redis, key_prefix="session:", permanent=True,
            SESSION_REVERSE_PROXY=False, SESSION_PROTECTION=True,
            SESSION_STATIC_FILE=False)

        @self.app.route("/", endpoint="bp_routes.root")
        async def root():
            return "root"

        @self.app.route("/user/<name>", endpoint="bp_routes.user_page")
        async def user_page(name):
            return name

        @self.app.route("/seed")
        async def seed():
            session.update(captcha="abcd", csrf_token="old-token", prelogin="discard")
            return "seeded"

        @self.app.route("/identity")
        async def identity():
            return {"user": None if g.user.is_anon else g.user.username}

        self.client = self.app.test_client(use_cookies=False)

    async def request(self, method, path, cookie=None, **kwargs):
        headers = {"Host": "funding.example", "X-Forwarded-For": "192.0.2.10"}
        if cookie:
            headers["Cookie"] = "session=" + cookie
        return await self.client.open(
            path, method=method, headers=headers,
            scope_base={"client": ("127.0.0.1", 12345)}, **kwargs)

    def cookie(self, response):
        cookies = SimpleCookie()
        for header in response.headers.getlist("Set-Cookie"):
            cookies.load(header)
        return cookies["session"].value

    def key(self, cookie):
        interface = self.app.session_interface
        if interface.use_signer:
            cookie = interface._get_signer(self.app).unsign(cookie).decode()
        return interface.key_prefix + cookie

    async def login(self, password="old-password", cookie=None, username="victim"):
        response = await self.request("POST", "/auth/login/", cookie,
                                      form={"username": username, "password": password})
        self.assertEqual(response.status_code, 302)
        return self.cookie(response)

    async def identity(self, cookie):
        response = await self.request("GET", "/identity", cookie)
        self.assertEqual(response.status_code, 200)
        return (await response.get_json())["user"]

    async def test_login_rotates_sid_deletes_old_record_and_discards_preauth_state(self):
        for signed, proxy, permanent in ((False, False, True), (True, True, False)):
            with self.subTest(signed=signed, proxy=proxy, permanent=permanent):
                self.app.session_interface.use_signer = signed
                self.app.session_interface.permanent = permanent
                self.app.config["SESSION_REVERSE_PROXY"] = proxy
                self.app.session_interface._config["SESSION_REVERSE_PROXY"] = proxy
                old = self.cookie(await self.request("GET", "/seed"))
                fresh = await self.login(cookie=old)
                self.assertNotEqual(fresh, old)
                self.assertNotIn(self.key(old), self.redis.values)
                data = self.app.session_interface.serializer.loads(self.redis.values[self.key(fresh)])
                self.assertNotIn("prelogin", data)
                self.assertNotIn("captcha", data)
                self.assertNotIn("csrf_token", data)
                self.assertEqual(data["_permanent"], permanent)
                self.assertEqual(data["_addr"], "192.0.2.10" if proxy else "127.0.0.1")
                self.assertEqual(await self.identity(fresh), "victim")
                self.assertIsNone(await self.identity(old))

    async def test_successful_relogin_invalidates_the_previous_authenticated_cookie(self):
        stolen = await self.login()
        fresh = await self.login(cookie=stolen)
        self.assertNotEqual(fresh, stolen)
        self.assertNotIn(self.key(stolen), self.redis.values)
        self.assertEqual(await self.identity(fresh), "victim")
        self.assertIsNone(await self.identity(stolen))

    async def test_password_reset_cannot_revive_a_stolen_cookie_on_relogin(self):
        stolen = await self.login()
        token = PasswordReset.issue(self.user)
        # Reset in another browser; do not probe the stolen cookie before login.
        response = await self.request("POST", "/auth/reset/" + token,
                                      form={"password": "new-password",
                                            "password_confirm": "new-password"})
        self.assertEqual(response.status_code, 302)
        fresh = await self.login("new-password", cookie=stolen)
        self.assertIsNone(await self.identity(stolen))
        self.assertNotEqual(fresh, stolen)
        self.assertNotIn(self.key(stolen), self.redis.values)
        self.assertEqual(await self.identity(fresh), "victim")

    async def test_admin_password_change_cannot_revive_a_stolen_cookie_on_relogin(self):
        stolen = await self.login()
        admin = await self.login(username="admin")
        # Populate a synchronizer token in the admin's actual Redis session.
        seeded = await self.request("GET", "/seed", admin)
        admin = self.cookie(seeded)
        response = await self.request("POST", "/auth/user/victim/password", admin,
                                      form={"password": "new-password", "csrf_token": "old-token"})
        self.assertEqual(response.status_code, 302)
        fresh = await self.login("new-password", cookie=stolen)
        self.assertIsNone(await self.identity(stolen))
        self.assertNotEqual(fresh, stolen)
        self.assertNotIn(self.key(stolen), self.redis.values)
        self.assertEqual(await self.identity(fresh), "victim")

    async def test_registration_also_rotates_the_anonymous_cookie(self):
        old = self.cookie(await self.request("GET", "/seed"))
        response = await self.request("POST", "/auth/register/", old,
                                      form={"username": "new-user", "email": "new@example.com",
                                            "password": "new-password", "captcha": "abcd"})
        self.assertEqual(response.status_code, 302)
        fresh = self.cookie(response)
        self.assertNotEqual(fresh, old)
        self.assertNotIn(self.key(old), self.redis.values)
        self.assertEqual(await self.identity(fresh), "new-user")
        self.assertIsNone(await self.identity(old))

    async def test_failed_login_does_not_authenticate_or_rotate_sid(self):
        old = self.cookie(await self.request("GET", "/seed"))
        response = await self.request("POST", "/auth/login/", old,
                                      form={"username": "victim", "password": "incorrect"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.cookie(response), old)
        self.assertIsNone(await self.identity(old))

    async def test_session_delete_failure_does_not_create_authenticated_session(self):
        old = self.cookie(await self.request("GET", "/seed"))
        with patch.object(self.redis, "delete", AsyncMock(side_effect=OSError("Redis unavailable"))):
            response = await self.request("POST", "/auth/login/", old,
                                          form={"username": "victim", "password": "old-password"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(await self.identity(old))


if __name__ == "__main__":
    unittest.main()
