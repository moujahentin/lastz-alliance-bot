"""Upgrade PR #7 populated databases without losing identity or operational state."""
import unittest

from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

import test_publication_migrations as previous


class MembershipAudienceMigrationTests(unittest.TestCase):
    snapshot = previous.PublicationMigrationTests.snapshot
    publication = previous.PublicationMigrationTests.publication

    def setUp(self):
        previous.PublicationMigrationTests.setUp(self)
        command.upgrade(self.config,'e93b20a714c8')
        self.publication()
        self.publication(message=1001,occurrence=4)
        self.publication(message=None,occurrence=1)
        with self.engine.begin() as connection:
            for id,alliance,rank,user in ((1,1,'MEMBER',30),(2,1,'R4',10),(3,1,'R5',20),(4,2,'R4',30)):
                connection.execute(text("INSERT INTO members (id,alliance_id,game_name,rank,discord_user_id) VALUES (:id,:alliance,:name,:rank,:user)"),
                                   dict(id=id,alliance=alliance,name=f'Player{id}',rank=rank,user=user))
        self.columns['event_publications']=[c['name'] for c in inspect(self.engine).get_columns('event_publications')]
        self.before=self.snapshot()

    def test_upgrade_preserves_pr7_state_maps_members_and_seeds_honest_baselines(self):
        command.upgrade(self.config,'head')
        self.assertEqual(self.snapshot(),self.before)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT id,alliance_id,rank,active,discord_user_id FROM members ORDER BY id')).all(),
                             [(1,1,'R1',1,30),(2,1,'R4',1,10),(3,1,'R5',1,20),(4,2,'R4',1,30)])
            self.assertEqual(connection.execute(text('SELECT member_id,previous_rank,new_rank,actor_id,source FROM membership_changes ORDER BY id')).all(),
                             [(1,'MEMBER','R1',None,'migration'),(2,'R4','R4',None,'migration'),(3,'R5','R5',None,'migration'),(4,'R4','R4',None,'migration')])
            for table in ('events','event_series','weekly_schedules'):
                self.assertEqual(connection.scalar(text(f'SELECT COUNT(*) FROM {table} WHERE audience != 31')),0)
            self.assertEqual(connection.scalar(text('SELECT COUNT(*) FROM event_audience_changes')),2)
            self.assertEqual(connection.execute(text('PRAGMA foreign_key_check')).all(),[])
        command.current(self.config); command.check(self.config)
        self.assertIn('f14c38b925d0 (head)',self.config.stdout.getvalue())
        self.assertIn('No new upgrade operations detected',self.config.stdout.getvalue())

    def test_migrated_rank_audience_constraints_uniqueness_and_tombstones(self):
        command.upgrade(self.config,'head')
        for query in ("UPDATE members SET rank='MEMBER' WHERE id=1", "UPDATE events SET audience=0 WHERE id=1",
                      "UPDATE weekly_schedules SET audience=32 WHERE id=1",
                      "UPDATE event_series SET audience=-1 WHERE id=1",
                      "UPDATE members SET discord_user_id=10 WHERE id=1"):
            with self.assertRaises(IntegrityError),self.engine.begin() as connection: connection.execute(text(query))
        with self.engine.begin() as connection:
            for rank in ('R1','R2','R3','R4','R5'): connection.execute(text('UPDATE members SET rank=:rank WHERE id=1'),{'rank':rank})
            connection.execute(text('DELETE FROM events WHERE id=1'))
            self.assertIsNone(connection.scalar(text('SELECT event_id FROM event_publications WHERE message_id=1000')))
            self.assertEqual(connection.scalar(text('SELECT COUNT(*) FROM event_rsvps WHERE event_id=1')),0)
            self.assertEqual(connection.scalar(text('SELECT COUNT(*) FROM event_audience_changes WHERE event_id=1')),0)
            old_id=connection.scalar(text('SELECT MAX(id) FROM event_publications'))
            connection.execute(text('DELETE FROM event_publications WHERE id=:id'),{'id':old_id})
        self.publication(message=1002,occurrence=4)
        with self.engine.connect() as connection:
            self.assertGreater(connection.scalar(text('SELECT MAX(id) FROM event_publications')),old_id)
            self.assertEqual(connection.execute(text('PRAGMA foreign_key_check')).all(),[])

    def test_baseline_only_downgrade_roundtrip_preserves_existing_data(self):
        command.upgrade(self.config,'head'); command.downgrade(self.config,'e93b20a714c8')
        self.assertEqual(self.snapshot(),self.before)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT rank FROM members ORDER BY id')).all(),[('MEMBER',),('R4',),('R5',),('R4',)])
        command.upgrade(self.config,'head'); command.check(self.config)
        self.assertEqual(self.snapshot(),self.before)

    def test_downgrade_refuses_new_membership_history(self):
        command.upgrade(self.config,'head')
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE membership_changes SET source='management',actor_id=10 WHERE member_id=1"))
        with self.assertRaisesRegex(RuntimeError,'membership or audience changes'): command.downgrade(self.config,'e93b20a714c8')
        self.assertEqual(self.snapshot(),self.before)

    def test_downgrade_refuses_restricted_audience_or_inactive_membership(self):
        command.upgrade(self.config,'head')
        for query,undo in (("UPDATE events SET audience=4 WHERE id=1","UPDATE events SET audience=31 WHERE id=1"),
                           ("UPDATE members SET active=0 WHERE id=1","UPDATE members SET active=1 WHERE id=1"),
                           ("UPDATE members SET rank='R2' WHERE id=1","UPDATE members SET rank='R1' WHERE id=1")):
            with self.engine.begin() as connection: connection.execute(text(query))
            with self.assertRaisesRegex(RuntimeError,'membership or audience changes'): command.downgrade(self.config,'e93b20a714c8')
            with self.engine.begin() as connection: connection.execute(text(undo))
