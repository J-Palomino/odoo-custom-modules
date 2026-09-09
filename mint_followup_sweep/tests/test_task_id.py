# -*- coding: utf-8 -*-
"""Which record a follow-up event actually points at.

This is the only part of the sweep that can be wrong *quietly*. If the task id
comes out wrong, the digest still renders and still looks correct - it just
links to the wrong record and reports the real task as untracked. That is
precisely what happened with event 10408, whose description cites a
maintenance.request before the task it is blocking.

The descriptions below are the real shapes the wrap-up skill has emitted.
"""
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..models.mail_activity import _task_id


@tagged('post_install', '-at_install')
class TestTaskId(TransactionCase):

    def test_modern_task_url(self):
        """The /odoo/project/<p>/task/<id> shape is unambiguous."""
        self.assertEqual(
            _task_id('Task: https://letsgomint.us/odoo/project/105/task/124952'),
            124952,
        )

    def test_legacy_web_hash_url(self):
        """The older /web#id=..&model=project.task shape still resolves."""
        self.assertEqual(
            _task_id('https://letsgomint.us/web#id=125295&model=project.task&view_type=form'),
            125295,
        )

    def test_prefers_the_task_over_an_earlier_other_model(self):
        """Regression: event 10408 cites a maintenance.request FIRST.

        Taking the first `id=` yielded 1440 - a maintenance.request id, rendered
        as a project.task link - and left task 94568 reported as having no
        calendar event at all.
        """
        desc = (
            'Chase Pablo on MR-1440 for the PrintNode credentials. '
            'Ticket: https://letsgomint.us/web#id=1440&model=maintenance.request&view_type=form '
            'Blocked task: https://letsgomint.us/web#id=94568&model=project.task&view_type=form'
        )
        self.assertEqual(_task_id(desc), 94568)

    def test_modern_url_wins_over_a_legacy_one(self):
        """A /task/ URL is trusted even when an id= for another model precedes it."""
        desc = (
            'Ticket: https://letsgomint.us/web#id=1440&model=maintenance.request '
            'Task: https://letsgomint.us/odoo/project/104/task/125609'
        )
        self.assertEqual(_task_id(desc), 125609)

    def test_ignores_a_non_task_model(self):
        """An id= belonging to another model is not a task id."""
        self.assertIsNone(
            _task_id('https://letsgomint.us/web#id=1440&model=maintenance.request&view_type=form')
        )

    def test_ignores_suffixed_id_keys(self):
        """`nodeid=` / `sessionid=` must not satisfy the bare `id=` scan."""
        self.assertIsNone(_task_id('agent nodeid=123456 sessionid=98765 restarted'))

    def test_unlinkable_description(self):
        """A bare "task #NNNN" mention carries no URL and is reported as unlinked.

        Event 10520 looks like this. The sweep's `unlinked` section exists to
        surface these rather than guess at them.
        """
        self.assertIsNone(_task_id('Follow-up from the gift-card session (Odoo task #123288).'))

    def test_empty_and_none(self):
        self.assertIsNone(_task_id(''))
        self.assertIsNone(_task_id(None))
