"""Exercise actual proposal models with SQLite and a fake validation RPC.

Run with the application's dependencies and local test settings configured.
No factory startup, network services, real addresses or credentials are used.
"""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import peewee
import settings
from funding.models import database as models
from funding.models.database import Event, Proposal, User
from funding.models.enums import ProposalCategory, ProposalStatus, UserRole
from funding.proposals.models import ProposalUpsert


class ProposalAddressValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = peewee.SqliteDatabase(":memory:")
        self.addCleanup(self.db.close)
        binding = self.db.bind_ctx([User, Proposal, Event],
                                   bind_refs=False, bind_backrefs=False)
        binding.__enter__()
        self.addCleanup(binding.__exit__, None, None, None)
        self.db.create_tables([User, Proposal, Event])
        self.owner = User.create(username="owner", mail="owner@example.com", password="unused")
        self.proposal = Proposal.create(
            user=self.owner, title="Original proposal", slug="original-owner",
            addr_receiving="a-original-address", markdown="Original proposal content.",
            html="Original proposal content.", funds_target=1.0,
            category=ProposalCategory.misc, status=ProposalStatus.idea)
        self.provider = SimpleNamespace(check_address=AsyncMock())
        self.discourse = SimpleNamespace(new_topic=AsyncMock(), new_post=AsyncMock())
        self.state = SimpleNamespace(user=self.owner)
        for target in (
            patch.object(models, "g", self.state),
            patch.object(models, "crypto_provider", self.provider),
            patch.object(models, "discourse", self.discourse),
            patch.object(Proposal, "by_slug", AsyncMock(return_value=self.proposal)),
            patch.object(settings, "DISCOURSE_ENABLED", False),
        ):
            target.start()
            self.addCleanup(target.stop)

    def payload(self, address, new=False):
        return ProposalUpsert(
            title="Updated proposal", slug=None if new else self.proposal.slug,
            markdown="Original proposal content.", funds_target=1.0,
            category=ProposalCategory.misc, status=None, discourse_topic_id=None,
            addr_receiving=address)

    def assert_no_changes(self):
        self.assertEqual(self.proposal.title, "Original proposal")
        self.assertEqual(self.proposal.addr_receiving, "a-original-address")
        stored = Proposal.get_by_id(self.proposal.uuid)
        self.assertEqual(stored.title, "Original proposal")
        self.assertEqual(stored.addr_receiving, "a-original-address")
        self.assertEqual(Proposal.select().count(), 1)
        self.assertEqual(Event.select().count(), 0)
        self.discourse.new_topic.assert_not_awaited()
        self.discourse.new_post.assert_not_awaited()

    async def test_invalid_results_rejected_for_every_prefix_on_create_and_edit(self):
        self.provider.check_address.return_value = {"isvalid": False, "isvalidSpark": False}
        for new in (False, True):
            for address in ("a-invalid-address", "s-invalid-address", "A-invalid-address",
                            "S-invalid-address", "STkH-invalid-address", "Spar-invalid-address",
                            "not-an-address", "4-invalid-address", "  a-invalid-address"):
                with self.subTest(new=new, address=address):
                    with self.assertRaisesRegex(Exception, "Invalid Address"):
                        await Proposal.upsert(self.payload(address, new))
                    self.assert_no_changes()

    async def test_valid_standard_results_do_not_require_a_firo_mainnet_prefix(self):
        self.provider.check_address.return_value = {"isvalid": True}
        for address in ("a-valid-address", "T-valid-address", "4-valid-address"):
            with self.subTest(address=address):
                result = await Proposal.upsert(self.payload(address))
                self.assertEqual(result.addr_receiving, address)
                self.assertEqual(Proposal.get_by_id(result.uuid).addr_receiving, address)
                self.provider.check_address.assert_awaited_with(address)

    async def test_valid_spark_result_is_accepted_when_standard_flag_is_false(self):
        self.provider.check_address.return_value = {"isvalid": False, "isvalidSpark": True}
        result = await Proposal.upsert(self.payload("s-valid-spark-address"))
        self.assertEqual(result.addr_receiving, "s-valid-spark-address")
        self.assertEqual(Proposal.get_by_id(result.uuid).addr_receiving, result.addr_receiving)

    async def test_new_valid_standard_and_spark_proposals_are_saved(self):
        for index, flags in enumerate(({"isvalid": True}, {"isvalidSpark": True})):
            with self.subTest(flags=flags):
                self.provider.check_address.return_value = flags
                address = "a-valid-address" if index == 0 else "s-valid-spark-address"
                data = self.payload(address, new=True)
                data.title = "New proposal " + str(index)
                result = await Proposal.upsert(data)
                self.assertEqual(Proposal.get_by_id(result.uuid).addr_receiving, address)
        self.assertEqual(Proposal.select().count(), 3)

    async def test_missing_malformed_and_non_boolean_flags_fail_closed(self):
        for result in (None, [], "invalid", {}, {"isvalid": False}, {"isvalidSpark": False},
                       {"isvalid": "false"}, {"isvalid": 1}, {"isvalidSpark": "true"},
                       {"isvalidSpark": 1}, {"isvalid": None, "isvalidSpark": None}):
            with self.subTest(result=result):
                self.provider.check_address.return_value = result
                with self.assertRaisesRegex(Exception, "Invalid Address"):
                    await Proposal.upsert(self.payload("not-an-address"))
                self.assert_no_changes()

    async def test_validation_rpc_error_does_not_change_or_save_proposal(self):
        self.provider.check_address.side_effect = OSError("validation RPC unavailable")
        with self.assertRaisesRegex(OSError, "validation RPC unavailable"):
            await Proposal.upsert(self.payload("a-valid-address"))
        self.assert_no_changes()

    async def test_unauthorized_edit_is_rejected_before_validation_rpc(self):
        self.state.user = User(username="other-user", role=UserRole.user)
        with self.assertRaisesRegex(Exception, "insufficient permissions"):
            await Proposal.upsert(self.payload("a-valid-address"))
        self.provider.check_address.assert_not_awaited()
        self.assert_no_changes()


if __name__ == "__main__":
    unittest.main()
