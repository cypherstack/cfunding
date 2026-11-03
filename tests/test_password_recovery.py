"""Focused recovery tests; run in the application's configured Python environment."""
import asyncio
import ssl
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import peewee
import settings
import funding
from funding import factory
from funding.auth import routes
from funding.models.database import User, PasswordReset
from funding.models.enums import UserRole
from funding.utils import mail


class FormRequest:
    def __init__(self, **form):
        self.method = "POST"
        self.data = form

    @property
    def form(self):
        async def value():
            return self.data
        return value()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        # Quart 0.17 needs a current loop when constructed on Python 3.9.
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.addCleanup(asyncio.set_event_loop, None)
        self.addCleanup(self.loop.close)
        self.db = peewee.SqliteDatabase(":memory:")
        self.addCleanup(self.db.close)
        self.bind = self.db.bind_ctx([User, PasswordReset], bind_refs=False, bind_backrefs=False)
        self.bind.__enter__()
        self.addCleanup(self.bind.__exit__, None, None, None)
        self.db.create_tables([User, PasswordReset])
        self.admin = User.create(username="admin", mail="admin@example.com",
                                 password="old-admin", role=UserRole.admin)
        self.user = User.create(username="user", mail="user@example.com",
                                password="old-user", role=UserRole.user)
        self.session = {"csrf_token": "valid-token"}
        self.g = types.SimpleNamespace(user=self.admin)
        self.app = Mock()
        targets = [
            patch.object(factory, "database", self.db),
            patch.object(routes, "database", self.db),
            patch.object(routes, "session", self.session),
            patch.object(funding, "g", self.g),
            patch.object(funding, "flash", AsyncMock()),
            patch.object(funding, "redirect", side_effect=lambda url: url),
            patch.object(funding, "url_for", return_value="/auth/login/"),
            patch.object(routes, "app", self.app),
            patch.object(routes, "mail_enabled", return_value=True),
            patch.object(routes, "flash", AsyncMock()),
            patch.object(routes, "render_template", AsyncMock(return_value="form")),
            patch.object(routes, "redirect", side_effect=lambda url: url),
            patch.object(routes, "url_for", return_value="/auth/reset/token"),
            patch.object(settings, "OPENID_CFG", None),
            patch.object(settings, "DOMAIN", "funding.example"),
        ]
        for target in targets:
            target.start()
            self.addCleanup(target.stop)

    def call(self, function, *args, **form):
        with patch.object(routes, "request", FormRequest(**form)):
            return self.loop.run_until_complete(function(*args))

    def reset(self, token, password="new-password"):
        return self.call(routes.reset, token, password=password, password_confirm=password)

    def set_password(self, **form):
        form.setdefault("password", "new-password")
        return self.call(routes.user_password_set, self.user.username, **form)

    def test_admin_rejects_missing_wrong_and_unicode_csrf(self):
        for token in ("", "wrong-token", "\N{SNOWMAN}"):
            with self.subTest(token=token), self.assertRaises(Exception) as error:
                self.set_password(csrf_token=token)
            self.assertEqual(error.exception.code, 403)
        self.assertEqual(User.get_by_id(self.user.uuid).password, "old-user")

    def test_admin_template_token_is_stable_for_the_session(self):
        app = factory.create_app()
        processor = app.template_context_processors[None][-1]
        session = {}
        with patch.object(factory, "session", session), patch.object(factory, "g", self.g):
            with patch.object(factory, "request", types.SimpleNamespace(path="/user/user")):
                with patch.object(settings, "COIN_NAME", "firo"):
                    first = processor()["csrf_token"]
                    self.assertEqual(processor()["csrf_token"], first)
        self.assertGreaterEqual(len(first), 32)
        self.session["csrf_token"] = first
        self.set_password(csrf_token=first)
        self.assertNotEqual(User.get_by_id(self.user.uuid).password, "old-user")

    def test_non_admin_cannot_set_password(self):
        self.g.user = self.user
        self.set_password(csrf_token="valid-token")
        self.assertEqual(User.get_by_id(self.user.uuid).password, "old-user")

    def test_admin_change_revokes_only_target_users_links(self):
        tokens = [PasswordReset.issue(self.user) for _ in range(2)]
        other = PasswordReset.issue(self.admin)
        old_version = self.user.password_version
        self.set_password(csrf_token="valid-token")
        self.assertTrue(all(PasswordReset.resolve(token) is None for token in tokens))
        self.assertIsNotNone(PasswordReset.resolve(other))
        self.assertNotEqual(User.get_by_id(self.user.uuid).password_version, old_version)

    def test_admin_failure_rolls_back_password_and_tokens(self):
        token = PasswordReset.issue(self.user)
        with patch.object(PasswordReset, "revoke_user", side_effect=RuntimeError("database failure")):
            with self.assertRaises(RuntimeError):
                self.set_password(csrf_token="valid-token")
        self.assertEqual(User.get_by_id(self.user.uuid).password, "old-user")
        self.assertIsNotNone(PasswordReset.resolve(token))

    def test_reset_consumes_link_and_sibling_links(self):
        tokens = [PasswordReset.issue(self.user) for _ in range(2)]
        old_version = self.user.password_version
        self.reset(tokens[0])
        self.assertTrue(all(PasswordReset.resolve(token) is None for token in tokens))
        self.assertIsNone(PasswordReset.consume(tokens[0]))
        self.assertNotEqual(User.get_by_id(self.user.uuid).password_version, old_version)

    def test_reset_failure_rolls_back_token(self):
        token = PasswordReset.issue(self.user)
        with patch.object(User, "save", side_effect=RuntimeError("database failure")):
            with self.assertRaises(RuntimeError):
                self.reset(token)
        self.assertIsNotNone(PasswordReset.resolve(token))
        self.assertEqual(User.get_by_id(self.user.uuid).password, "old-user")

    def test_five_characters_rejected_six_accepted_in_both_paths(self):
        token = PasswordReset.issue(self.user)
        self.reset(token, password="12345")
        self.assertIsNotNone(PasswordReset.resolve(token))
        self.set_password(password="12345", csrf_token="valid-token")
        self.assertEqual(User.get_by_id(self.user.uuid).password, "old-user")
        self.reset(token, password="123456")
        self.assertIsNone(PasswordReset.resolve(token))
        self.session["csrf_token"] = "valid-token"
        self.set_password(password="abcdef", csrf_token="valid-token")
        self.assertTrue(User.validate(self.user.username, "abcdef"))

    def test_expiry_replay_and_request_limit_preserved(self):
        tokens = [PasswordReset.issue(self.user) for _ in range(3)]
        self.assertIsNone(PasswordReset.issue(self.user))
        PasswordReset.update(expires=datetime.now() - timedelta(seconds=1)).execute()
        self.assertTrue(all(PasswordReset.consume(token) is None for token in tokens))

    def test_disabled_and_openid_accounts_cannot_consume_tokens(self):
        for field, value in (("enabled", False), ("oip", "provider")):
            with self.subTest(field=field):
                token = PasswordReset.issue(self.user)
                User.update(**{field: value}).where(User.uuid == self.user.uuid).execute()
                self.assertIsNone(PasswordReset.consume(token))
                User.update(**{field: getattr(self.user, field)}).where(User.uuid == self.user.uuid).execute()

    def test_forgot_normalizes_and_defers_all_account_lookups(self):
        for validated in (types.SimpleNamespace(normalized="user@example.com"),
                          types.SimpleNamespace(email="user@example.com")):
            self.session["captcha"] = "abcd"
            self.app.reset_mock()
            with patch.object(routes, "validate_email", return_value=validated):
                with patch.object(User, "select", side_effect=AssertionError("lookup before response")):
                    self.call(routes.forgot, email="user@EXAMPLE.COM", captcha="abcd")
            self.app.add_background_task.assert_called_once_with(routes.send_reset_mail, "user@example.com")

    def test_invalid_captcha_does_not_schedule_mail(self):
        self.session["captcha"] = "abcd"
        self.call(routes.forgot, email="user@example.com", captcha="wrong")
        self.app.add_background_task.assert_not_called()

    def test_delivery_failure_releases_request_capacity(self):
        with patch.object(routes, "send_mail", AsyncMock(side_effect=OSError("SMTP failure"))):
            self.loop.run_until_complete(routes.send_reset_mail(self.user.mail))
        self.assertEqual(PasswordReset.select().count(), 0)

    def test_background_mail_uses_local_path_and_configured_domain(self):
        with patch.object(routes, "send_mail", AsyncMock()) as send:
            self.loop.run_until_complete(routes.send_reset_mail(self.user.mail))
        self.assertIn("https://funding.example/auth/reset/token", send.call_args.args[2])
        self.assertEqual(routes.url_for.call_args.kwargs["_external"], False)

    def test_unknown_disabled_and_openid_accounts_do_not_receive_mail(self):
        with patch.object(routes, "send_mail", AsyncMock()) as send:
            self.loop.run_until_complete(routes.send_reset_mail("missing@example.com"))
            self.user.enabled = False
            self.user.save()
            self.loop.run_until_complete(routes.send_reset_mail(self.user.mail))
            self.user.enabled = True
            self.user.oip = "provider"
            self.user.save()
            self.loop.run_until_complete(routes.send_reset_mail(self.user.mail))
        send.assert_not_awaited()
        self.assertEqual(PasswordReset.select().count(), 0)

    def test_session_is_rejected_after_either_password_change(self):
        app = factory.create_app()
        hook = app.before_request_funcs[None][0]
        for change in ("reset", "admin"):
            with self.subTest(change=change):
                self.session["csrf_token"] = "valid-token"
                current = User.get_by_id(self.user.uuid)
                old_session = {"user": self.loop.run_until_complete(current.to_json())}
                token = PasswordReset.issue(current)
                if change == "reset":
                    self.reset(token)
                else:
                    self.set_password(csrf_token="valid-token")
                state = types.SimpleNamespace()
                with patch.object(factory, "session", old_session), patch.object(factory, "g", state):
                    with patch.object(factory, "request", types.SimpleNamespace(url="https://funding.example/")):
                        with patch.object(User, "by_uuid", return_value=User.get_by_id(self.user.uuid)):
                            self.loop.run_until_complete(hook())
                self.assertEqual(old_session, {})
                self.assertTrue(state.user.is_anon)

    def test_fresh_session_accepted_and_legacy_session_rejected(self):
        app = factory.create_app()
        hook = app.before_request_funcs[None][0]
        for valid in (True, False):
            current = User.get_by_id(self.user.uuid)
            data = self.loop.run_until_complete(current.to_json()) if valid else {"uuid": current.uuid}
            state = types.SimpleNamespace()
            session = {"user": data}
            with patch.object(factory, "session", session), patch.object(factory, "g", state):
                with patch.object(factory, "request", types.SimpleNamespace(url="https://funding.example/")):
                    with patch.object(User, "by_uuid", return_value=current):
                        self.loop.run_until_complete(hook())
            self.assertEqual(not state.user.is_anon, valid)

    def test_registration_stores_a_session_version_that_matches_database(self):
        self.session["captcha"] = "abcd"
        data = types.SimpleNamespace(username="new-user", email="new@example.com",
                                     password="new-password", captcha="abcd")
        # SID rotation is exercised with real HTTP cookies in test_login_sessions.
        with patch.object(routes, "rotate_session", AsyncMock()) as rotate:
            self.call(routes.register_post.__wrapped__, data)
        rotate.assert_awaited_once()
        saved = User.get(User.username == data.username)
        self.assertEqual(self.session["user"]["password_version"], saved.password_version)


class MailTests(unittest.TestCase):
    def test_both_tls_modes_verify_certificates_and_hostname(self):
        for implicit in (False, True):
            with self.subTest(implicit=implicit):
                with patch.object(mail, "cfg", side_effect=lambda name, default=None: {
                    "SMTP_HOST": "smtp.example.com", "SMTP_SSL": implicit,
                }.get(name, default)):
                    with patch.object(mail.smtplib, "SMTP") as smtp, patch.object(mail.smtplib, "SMTP_SSL") as smtp_ssl:
                        mail._send_blocking(Mock())
                        context = (smtp_ssl.call_args.kwargs["context"] if implicit else
                                   smtp.return_value.starttls.call_args.kwargs["context"])
                        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                        self.assertTrue(context.check_hostname)

    def test_starttls_certificate_failure_prevents_login_and_delivery(self):
        with patch.object(mail, "cfg", side_effect=lambda name, default=None: {
            "SMTP_HOST": "smtp.example.com", "SMTP_USER": "user",
        }.get(name, default)):
            with patch.object(mail.smtplib, "SMTP") as smtp:
                server = smtp.return_value
                server.starttls.side_effect = ssl.SSLCertVerificationError("untrusted")
                with self.assertRaises(ssl.SSLCertVerificationError):
                    mail._send_blocking(Mock())
                server.login.assert_not_called()
                server.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
