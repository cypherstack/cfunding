"""Regression tests for proposal-to-forum authorization, with no service setup.

The model module imports the application factory and its database/wallet clients
at import time. Extract the real model methods by AST so these tests need only
the standard library; fake their surrounding ORM and network dependencies.
These are model-level tests, not HTTP/database integration tests.

Run: python3 -m unittest discover -s tests -v
"""
import ast
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from typing import Optional


MODEL_PATH = Path(__file__).resolve().parents[1] / "funding/models/database.py"


class Status(Enum):
    idea = 1


def load_proposal_model():
    source = ast.parse(MODEL_PATH.read_text())
    model = next(node for node in source.body
                 if isinstance(node, ast.ClassDef) and node.name == "Proposal")
    names = {
        "check_discourse_topic_id_permission", "set_discourse_topic_id",
        "generate_discourse_topic", "upsert",
    }
    methods = [node for node in model.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in names]
    extracted = ast.Module(body=[ast.ClassDef(
        name="Proposal", bases=[], keywords=[], body=methods, decorator_list=[]
    )], type_ignores=[])
    ast.fix_missing_locations(extracted)
    scope = {
        "Optional": Optional, "User": object, "ProposalUpsert": object,
        "ProposalStatus": Status, "datetime": datetime, "json": json,
        "settings": SimpleNamespace(DISCOURSE_ENABLED=True),
        "g": SimpleNamespace(user=None), "app": SimpleNamespace(logger=Mock()),
        "url_for": Mock(return_value="https://example.invalid/proposals/test"),
        "crypto_provider": SimpleNamespace(check_address=AsyncMock(
            return_value={"isvalid": True, "isvalidSpark": True})),
        "Event": Mock(side_effect=lambda **fields: SimpleNamespace(
            **fields, save=Mock())),
    }
    exec(compile(extracted, str(MODEL_PATH), "exec"), scope)
    proposal_class = scope["Proposal"]
    instances = []

    def init(self):
        self.discourse_topic_id = None
        self.modified = None
        self.title = "Original title"
        self.slug = "original-owner"
        self.status = Status.idea
        self._is_new = False
        self._events = []
        self.user = None
        self.generate_slug = Mock(return_value="new-proposal-owner")
        self.set_addr_receiving = Mock()
        self.set_category = AsyncMock()
        self.set_status = AsyncMock()
        self.set_funds_target = AsyncMock()
        self.generate_deposit_address = AsyncMock()
        self.set_markdown = Mock()
        self.save = Mock()
        instances.append(self)

    proposal_class.__init__ = init
    proposal_class.by_slug = AsyncMock()
    proposal_class.post_topic = AsyncMock(return_value=9001)
    return proposal_class, scope, instances


def user(name="owner", role=1, anonymous=False):
    return SimpleNamespace(username=name, is_moderator=role >= 2,
                           is_anon=anonymous)


def payload(**overrides):
    fields = dict(slug="original-owner", title="Changed title",
                  addr_receiving="a-valid-test-address", category=3,
                  status=None, funds_target=2.0,
                  discourse_topic_id=1, markdown="Changed proposal content")
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TopicAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.Proposal, self.scope, self.instances = load_proposal_model()
        self.owner = user()
        self.proposal = self.Proposal()
        self.proposal.user = self.owner
        self.proposal.discourse_topic_id = 4401
        self.Proposal.by_slug.return_value = self.proposal
        self.scope["g"].user = self.owner

    def assert_no_submitted_effects(self, proposal):
        self.assertEqual(proposal.title, "Original title")
        self.assertIsNone(proposal.modified)
        self.scope["crypto_provider"].check_address.assert_not_awaited()
        proposal.set_addr_receiving.assert_not_called()
        proposal.set_category.assert_not_awaited()
        proposal.set_status.assert_not_awaited()
        proposal.set_funds_target.assert_not_awaited()
        proposal.generate_deposit_address.assert_not_awaited()
        proposal.set_markdown.assert_not_called()
        proposal.save.assert_not_called()
        self.Proposal.post_topic.assert_not_awaited()
        self.scope["url_for"].assert_not_called()
        for event in proposal._events:
            event.save.assert_not_called()

    def test_direct_setter_rejects_owner_reassignment(self):
        with self.assertRaisesRegex(Exception, "discourse_topic_id"):
            self.proposal.set_discourse_topic_id(1, self.owner)
        self.assertEqual(self.proposal.discourse_topic_id, 4401)
        self.assertIsNone(self.proposal.modified)

    async def test_generator_rejects_owner_reassignment(self):
        with self.assertRaisesRegex(Exception, "discourse_topic_id"):
            await self.proposal.generate_discourse_topic(1, self.owner)
        self.assertEqual(self.proposal.discourse_topic_id, 4401)
        self.assertEqual(self.proposal._events, [])
        self.assert_no_submitted_effects(self.proposal)

    async def test_generator_rejects_owner_attaching_unlinked_proposal(self):
        self.proposal.discourse_topic_id = None
        with self.assertRaisesRegex(Exception, "discourse_topic_id"):
            await self.proposal.generate_discourse_topic(1, self.owner)
        self.assertIsNone(self.proposal.discourse_topic_id)
        self.assertEqual(self.proposal._events, [])
        self.assert_no_submitted_effects(self.proposal)

    async def test_reassignment_rejected_even_when_discourse_disabled(self):
        self.scope["settings"].DISCOURSE_ENABLED = False
        with self.assertRaisesRegex(Exception, "discourse_topic_id"):
            await self.proposal.generate_discourse_topic(1, self.owner)
        self.assertEqual(self.proposal.discourse_topic_id, 4401)

    async def test_missing_actor_cannot_reassign(self):
        with self.assertRaisesRegex(Exception, "discourse_topic_id"):
            await self.proposal.generate_discourse_topic(1)
        self.assertEqual(self.proposal.discourse_topic_id, 4401)

    async def test_owner_can_submit_unchanged_topic(self):
        await self.proposal.generate_discourse_topic(4401, self.owner)
        self.assertEqual(self.proposal.discourse_topic_id, 4401)
        self.assertEqual(self.proposal._events, [])
        self.assert_no_submitted_effects(self.proposal)

    async def test_owner_can_omit_topic_on_existing_proposal(self):
        await self.proposal.generate_discourse_topic(None, self.owner)
        self.assertEqual(self.proposal.discourse_topic_id, 4401)
        self.assertEqual(self.proposal._events, [])
        self.assert_no_submitted_effects(self.proposal)

    async def test_moderator_and_admin_can_reassign(self):
        for role in (2, 3):
            with self.subTest(role=role):
                self.proposal.discourse_topic_id = 4401
                self.proposal._events = []
                actor = user("staff", role=role)
                await self.proposal.generate_discourse_topic(1, actor)
                self.assertEqual(self.proposal.discourse_topic_id, 1)
                self.assertIsInstance(self.proposal.modified, datetime)
                self.assertEqual(len(self.proposal._events), 1)
                self.assertEqual(self.proposal._events[0].message,
                                 "Discourse topic id changed")
                self.assertIs(self.proposal._events[0].user, actor)
        self.Proposal.post_topic.assert_not_awaited()

    async def test_automatic_topic_creation_for_owner_still_works(self):
        self.proposal.discourse_topic_id = None
        await self.proposal.generate_discourse_topic(None, self.owner)
        self.Proposal.post_topic.assert_awaited_once_with(
            self.proposal, "https://example.invalid/proposals/test")
        self.assertEqual(self.proposal.discourse_topic_id, 9001)
        self.assertEqual(self.proposal._events[0].message, "Discourse topic posted")

    async def test_disabled_discourse_does_not_create_topic(self):
        self.scope["settings"].DISCOURSE_ENABLED = False
        self.proposal.discourse_topic_id = None
        await self.proposal.generate_discourse_topic(None, self.owner)
        self.assertIsNone(self.proposal.discourse_topic_id)
        self.assertEqual(self.proposal._events, [])
        self.Proposal.post_topic.assert_not_awaited()

    async def test_upsert_rejects_owner_before_applying_changes(self):
        with self.assertRaisesRegex(Exception, "discourse_topic_id"):
            await self.Proposal.upsert(payload())
        self.assertEqual(self.proposal.discourse_topic_id, 4401)
        self.assertEqual(self.proposal._events, [])
        self.assert_no_submitted_effects(self.proposal)

    async def test_new_proposal_cannot_choose_existing_forum_topic(self):
        with self.assertRaisesRegex(Exception, "discourse_topic_id"):
            await self.Proposal.upsert(payload(slug=None))
        created = self.instances[-1]
        self.assertTrue(created._is_new)
        self.assertIsNone(created.discourse_topic_id)
        self.assert_no_submitted_effects(created)

    async def test_upsert_owner_ordinary_edits_with_unchanged_topic(self):
        result = await self.Proposal.upsert(payload(discourse_topic_id=4401))
        self.assertIs(result, self.proposal)
        self.assertEqual(result.title, "Changed title")
        self.assertEqual(result.discourse_topic_id, 4401)
        result.save.assert_called_once_with(force_insert=False)
        result.set_funds_target.assert_awaited_once_with(2.0, self.owner)
        self.Proposal.post_topic.assert_not_awaited()

    async def test_upsert_owner_ordinary_edits_with_omitted_topic(self):
        result = await self.Proposal.upsert(payload(discourse_topic_id=None))
        self.assertEqual(result.discourse_topic_id, 4401)
        result.save.assert_called_once_with(force_insert=False)
        self.Proposal.post_topic.assert_not_awaited()

    async def test_new_owner_proposal_gets_server_created_topic(self):
        result = await self.Proposal.upsert(payload(slug=None, discourse_topic_id=None))
        self.assertTrue(result._is_new)
        self.assertIs(result.user, self.owner)
        self.assertEqual(result.discourse_topic_id, 9001)
        result.save.assert_called_once_with(force_insert=True)
        self.Proposal.post_topic.assert_awaited_once()
        self.assertEqual([e.message for e in result._events],
                         ["Proposal created", "Discourse topic posted"])

    async def test_upsert_moderator_and_admin_can_edit_other_owners_topic(self):
        for role in (2, 3):
            with self.subTest(role=role):
                self.proposal.discourse_topic_id = 4401
                self.proposal._events = []
                self.scope["g"].user = user("staff", role=role)
                result = await self.Proposal.upsert(payload())
                self.assertEqual(result.discourse_topic_id, 1)
                self.assertIs(result.user, self.owner)
                self.assertEqual(result._events[0].message, "Discourse topic id changed")
        self.Proposal.post_topic.assert_not_awaited()

    async def test_other_ordinary_user_still_cannot_edit_proposal(self):
        self.scope["g"].user = user("other")
        with self.assertRaisesRegex(Exception, "insufficient permissions"):
            await self.Proposal.upsert(payload(discourse_topic_id=4401))
        self.assert_no_submitted_effects(self.proposal)

    async def test_anonymous_user_still_cannot_edit_proposal(self):
        self.scope["g"].user = user(anonymous=True)
        with self.assertRaisesRegex(Exception, "insufficient permissions"):
            await self.Proposal.upsert(payload(discourse_topic_id=4401))
        self.assert_no_submitted_effects(self.proposal)


if __name__ == "__main__":
    unittest.main()
