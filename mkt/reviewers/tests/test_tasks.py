import datetime

from django.conf import settings
from django.db.models import Q

import mock
from nose.tools import eq_

import amo
from abuse.models import AbuseReport
from amo.tasks import find_abuse_escalations, find_refund_escalations
from amo.tests import addon_factory, app_factory
from devhub.models import ActivityLog, AppLog
from editors.models import EscalationQueue, ReviewerScore
from market.models import AddonPurchase, Refund
from stats.models import Contribution
from users.models import UserProfile

import mkt.constants.reviewers as rvw
from mkt.reviewers.tasks import _batch_award_points
from mkt.site.fixtures import fixture


class TestAbuseEscalationTask(amo.tests.TestCase):
    fixtures = ['base/users']

    def setUp(self):
        self.app = app_factory(name='XXX')
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)

        patcher = mock.patch.object(settings, 'TASK_USER_ID', 4043307)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_abuses_no_history(self):
        find_abuse_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)

    def test_abuse_no_history(self):
        for x in range(2):
            AbuseReport.objects.create(addon=self.app)
        find_abuse_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 1)

    def test_abuse_already_escalated(self):
        for x in range(2):
            AbuseReport.objects.create(addon=self.app)
        find_abuse_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 1)
        find_abuse_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 1)

    def test_abuse_cleared_not_escalated(self):
        for x in range(2):
            ar = AbuseReport.objects.create(addon=self.app)
            ar.created = datetime.datetime.now() - datetime.timedelta(days=1)
            ar.save()
        find_abuse_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 1)

        # Simulate a reviewer clearing an escalation... remove app from queue,
        # and write a log.
        EscalationQueue.objects.filter(addon=self.app).delete()
        amo.log(amo.LOG.ESCALATION_CLEARED, self.app, self.app.current_version,
                details={'comments': 'All clear'})
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)

        # Task will find it again but not add it again.
        find_abuse_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)

    def test_older_abuses_cleared_then_new(self):
        for x in range(2):
            ar = AbuseReport.objects.create(addon=self.app)
            ar.created = datetime.datetime.now() - datetime.timedelta(days=1)
            ar.save()
        find_abuse_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 1)

        # Simulate a reviewer clearing an escalation... remove app from queue,
        # and write a log.
        EscalationQueue.objects.filter(addon=self.app).delete()
        amo.log(amo.LOG.ESCALATION_CLEARED, self.app, self.app.current_version,
                details={'comments': 'All clear'})
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)

        # Task will find it again but not add it again.
        find_abuse_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)

        # New abuse reports that come in should re-add to queue.
        for x in range(2):
            AbuseReport.objects.create(addon=self.app)
        find_abuse_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 1)

    def test_already_escalated_for_other_still_logs(self):
        # Add app to queue for high refunds.
        EscalationQueue.objects.create(addon=self.app)
        amo.log(amo.LOG.ESCALATED_HIGH_REFUNDS, self.app,
                self.app.current_version, details={'comments': 'hi refunds'})

        # Set up abuses.
        for x in range(2):
            AbuseReport.objects.create(addon=self.app)
        find_abuse_escalations(self.app.id)

        # Verify it logged the high abuse reports.
        action = amo.LOG.ESCALATED_HIGH_ABUSE
        assert AppLog.objects.filter(
            addon=self.app, activity_log__action=action.id).exists(), (
                u'Expected high abuse to be logged')


class TestRefundsEscalationTask(amo.tests.TestCase):
    fixtures = ['base/users']

    def setUp(self):
        self.app = app_factory(name='XXX')
        self.user1, self.user2, self.user3 = UserProfile.objects.all()[:3]

        patcher = mock.patch.object(settings, 'TASK_USER_ID', 4043307)
        patcher.start()
        self.addCleanup(patcher.stop)

        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)

    def _purchase(self, user=None, created=None):
        ap1 = AddonPurchase.objects.create(user=user or self.user1,
                                           addon=self.app)
        if created:
            ap1.update(created=created)

    def _refund(self, user=None, created=None):
        contribution = Contribution.objects.create(addon=self.app,
                                                   user=user or self.user1)
        ref = Refund.objects.create(contribution=contribution,
                                    user=user or self.user1)
        if created:
            ref.update(created=created)
            # Needed because these tests can run in the same second and the
            # refund detection task depends on timestamp logic for when to
            # escalate.
            applog = AppLog.objects.all().order_by('-created', '-id')[0]
            applog.update(created=created)

    def test_multiple_refunds_same_user(self):
        self._purchase(self.user1)
        self._refund(self.user1)
        self._refund(self.user1)
        eq_(Refund.recent_refund_ratio(
            self.app.id, datetime.datetime.now() - datetime.timedelta(days=1)),
            1.0)

    def test_no_refunds(self):
        find_refund_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)

    def test_refunds(self):
        self._purchase(self.user1)
        self._purchase(self.user2)
        self._refund(self.user1)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 1)

    def test_refunds_already_escalated(self):
        self._purchase(self.user1)
        self._purchase(self.user2)
        self._refund(self.user1)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 1)
        # Task was run on Refund.post_save, re-run task to make sure we don't
        # escalate again.
        find_refund_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 1)

    def test_refunds_cleared_not_escalated(self):
        stamp = datetime.datetime.now() - datetime.timedelta(days=2)
        self._purchase(self.user1, stamp)
        self._purchase(self.user2, stamp)
        self._refund(self.user1, stamp)

        # Simulate a reviewer clearing an escalation...
        #   remove app from queue and write a log.
        EscalationQueue.objects.filter(addon=self.app).delete()
        amo.log(amo.LOG.ESCALATION_CLEARED, self.app, self.app.current_version,
                details={'comments': 'All clear'})
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)
        # Task will find it again but not add it again.
        find_refund_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)

    def test_older_refund_escalations_then_new(self):
        stamp = datetime.datetime.now() - datetime.timedelta(days=2)
        self._purchase(self.user1, stamp)
        self._purchase(self.user2, stamp)

        # Triggers 33% for refund / purchase ratio.
        self._refund(self.user1, stamp)

        # Simulate a reviewer clearing an escalation...
        #   remove app from queue and write a log.
        EscalationQueue.objects.filter(addon=self.app).delete()
        amo.log(amo.LOG.ESCALATION_CLEARED, self.app, self.app.current_version,
                details={'comments': 'All ok'})
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)

        # Task will find it again but not add it again.
        find_refund_escalations(self.app.id)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 0)

        # Issue another refund, which should trigger another escalation.
        self._purchase(self.user3)
        self._refund(self.user3)
        eq_(EscalationQueue.objects.filter(addon=self.app).count(), 1)

    def test_already_escalated_for_other_still_logs(self):
        # Add app to queue for abuse reports.
        EscalationQueue.objects.create(addon=self.app)
        amo.log(amo.LOG.ESCALATED_HIGH_ABUSE, self.app,
                self.app.current_version, details={'comments': 'abuse'})
        # Set up purchases.
        stamp = datetime.datetime.now() - datetime.timedelta(days=2)
        self._purchase(self.user1, stamp)
        self._purchase(self.user2, stamp)
        # Triggers 33% for refund / purchase ratio.
        self._refund(self.user1, stamp)

        # Verify it logged the high refunds.
        action = amo.LOG.ESCALATED_HIGH_REFUNDS
        assert AppLog.objects.filter(
            addon=self.app, activity_log__action=action.id).exists(), (
                u'Expected high refunds to be logged')


class TestBatchAwardPoints(amo.tests.TestCase):
    fixtures = fixture('user_999', 'user_2519')

    def test_batch_award_points(self):
        user_999 = UserProfile.objects.get(username='regularuser')
        addon_999 = addon_factory()
        amo.log(amo.LOG.THEME_REVIEW, addon_999, details={
            'action': rvw.ACTION_APPROVE
        }, user=user_999)

        # No points for this one.
        amo.log(amo.LOG.THEME_REVIEW, addon_factory(), details={
            'action': rvw.ACTION_MOREINFO
        }, user=user_999)

        user_2519 = UserProfile.objects.get(username='cfinke')
        addon_2519 = addon_factory()
        amo.log(amo.LOG.THEME_REVIEW, addon_2519, details={
            'action': rvw.ACTION_REJECT
        }, user=user_2519)

        # Mostly copied and pasted from migration award_theme_rev_points.py.
        approve = '"action": %s' % rvw.ACTION_APPROVE
        reject = '"action": %s' % rvw.ACTION_REJECT
        logs = ActivityLog.objects.filter(
            (Q(_details__contains=approve) | Q(_details__contains=reject)),
            action=amo.LOG.THEME_REVIEW.id)

        _batch_award_points(logs)

        eq_(ReviewerScore.objects.count(), 2)

        points = amo.REVIEWED_SCORES.get(amo.REVIEWED_PERSONA)
        r1 = ReviewerScore.objects.get(user=user_999)
        eq_(r1.score, points)
        eq_(r1.note, 'RETROACTIVE')
        eq_(r1.addon, addon_999)
        eq_(r1.note_key, amo.REVIEWED_PERSONA)

        r2 = ReviewerScore.objects.get(user=user_2519)
        eq_(r2.score, points)
        eq_(r2.note, 'RETROACTIVE')
        eq_(r2.addon, addon_2519)
        eq_(r2.note_key, amo.REVIEWED_PERSONA)
