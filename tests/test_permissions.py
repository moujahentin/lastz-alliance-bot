import unittest

from lastz_bot.permissions import can_manage_target, has_minimum_rank


class PermissionMatrixTests(unittest.TestCase):
    def test_r5_can_manage_all_supported_ranks(self) -> None:
        self.assertTrue(can_manage_target("R5", "R5"))
        self.assertTrue(can_manage_target("R5", "R4"))
        self.assertTrue(can_manage_target("R5", "MEMBER"))

    def test_r4_can_manage_r4_and_member_but_not_r5(self) -> None:
        self.assertFalse(can_manage_target("R4", "R5"))
        self.assertTrue(can_manage_target("R4", "R4"))
        self.assertTrue(can_manage_target("R4", "MEMBER"))

    def test_member_cannot_manage_any_rank(self) -> None:
        self.assertFalse(can_manage_target("MEMBER", "R5"))
        self.assertFalse(can_manage_target("MEMBER", "R4"))
        self.assertFalse(can_manage_target("MEMBER", "MEMBER"))

    def test_minimum_r4_access(self) -> None:
        self.assertTrue(has_minimum_rank("R5", "R4"))
        self.assertTrue(has_minimum_rank("R4", "R4"))
        self.assertFalse(has_minimum_rank("MEMBER", "R4"))

    def test_unknown_actor_rank_has_no_management_access(self) -> None:
        self.assertFalse(can_manage_target("UNKNOWN", "MEMBER"))
        self.assertFalse(has_minimum_rank("UNKNOWN", "R4"))


if __name__ == "__main__":
    unittest.main()


class GetMemberRankIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from lastz_bot.database.base import Base
        from lastz_bot.database.models import Alliance, Guild, Member
        import lastz_bot.permissions as permissions

        self.permissions = permissions
        self.original_session_local = permissions.SessionLocal

        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

        self.TestSessionLocal = sessionmaker(bind=self.engine)
        permissions.SessionLocal = self.TestSessionLocal

        with self.TestSessionLocal() as session:
            guild = Guild(
                id=1001,
                name="Test Guild",
            )

            alliance_one = Alliance(
                guild_id=1001,
                name="Alpha",
            )

            alliance_two = Alliance(
                guild_id=1001,
                name="Bravo",
            )

            guild.alliances.extend([alliance_one, alliance_two])
            session.add(guild)
            session.flush()

            session.add_all(
                [
                    Member(
                        alliance_id=alliance_one.id,
                        game_name="Leader",
                        rank="R5",
                        discord_user_id=555,
                    ),
                    Member(
                        alliance_id=alliance_two.id,
                        game_name="Other Player",
                        rank="R4",
                        discord_user_id=777,
                    ),
                ]
            )

            session.commit()

    def tearDown(self) -> None:
        self.permissions.SessionLocal = self.original_session_local
        self.engine.dispose()

    def test_returns_member_rank_for_matching_alliance(self) -> None:
        rank = self.permissions.get_member_rank(
            guild_id=1001,
            alliance_name="Alpha",
            discord_user_id=555,
        )

        self.assertEqual(rank, "R5")

    def test_does_not_leak_rank_between_alliances(self) -> None:
        rank = self.permissions.get_member_rank(
            guild_id=1001,
            alliance_name="Bravo",
            discord_user_id=555,
        )

        self.assertIsNone(rank)

    def test_returns_none_for_unknown_alliance(self) -> None:
        rank = self.permissions.get_member_rank(
            guild_id=1001,
            alliance_name="Missing",
            discord_user_id=555,
        )

        self.assertIsNone(rank)
