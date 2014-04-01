import mock

from changes.config import db
from changes.constants import Result
from changes.models import ProjectOption, Patch, LogSource, LogChunk, ItemOption
from changes.listeners.mail import job_finished_handler, MailNotificationHandler
from changes.testutils.cases import TestCase


class GetRecipientsTestCase(TestCase):
    def test_default_options(self):
        author = self.create_author('foo@example.com')
        build = self.create_build(self.project, result=Result.passed, author=author)
        job = self.create_job(build)

        handler = MailNotificationHandler()
        recipients = handler.get_recipients(job)

        assert recipients == ['{0} <foo@example.com>'.format(author.name)]

    def test_without_author_option(self):
        db.session.add(ProjectOption(
            project=self.project, name='mail.notify-author', value='0'))
        author = self.create_author('foo@example.com')
        build = self.create_build(self.project, result=Result.failed, author=author)
        job = self.create_job(build)
        db.session.commit()

        handler = MailNotificationHandler()
        recipients = handler.get_recipients(job)

        assert recipients == []

    def test_with_addressees(self):
        db.session.add(ProjectOption(
            project=self.project, name='mail.notify-author', value='1'))
        db.session.add(ProjectOption(
            project=self.project, name='mail.notify-addresses',
            value='test@example.com, bar@example.com'))

        author = self.create_author('foo@example.com')
        build = self.create_build(self.project, result=Result.failed, author=author)
        job = self.create_job(build)
        db.session.commit()

        handler = MailNotificationHandler()
        recipients = handler.get_recipients(job)

        assert recipients == [
            '{0} <foo@example.com>'.format(author.name),
            'test@example.com',
            'bar@example.com',
        ]

    def test_with_revision_addressees(self):
        db.session.add(ProjectOption(
            project=self.project, name='mail.notify-author', value='1'))
        db.session.add(ProjectOption(
            project=self.project, name='mail.notify-addresses-revisions',
            value='test@example.com, bar@example.com'))

        author = self.create_author('foo@example.com')
        patch = Patch(
            repository=self.repo, project=self.project, label='foo',
            diff='',
        )
        source = self.create_source(self.project, patch=patch)
        build = self.create_build(
            project=self.project,
            source=source,
            author=author,
            result=Result.failed,
        )
        job = self.create_job(build=build)
        db.session.commit()

        handler = MailNotificationHandler()
        recipients = handler.get_recipients(job)

        assert recipients == ['{0} <foo@example.com>'.format(author.name)]

        build = self.create_build(
            project=self.project,
            result=Result.failed,
            author=author,
        )
        job = self.create_job(build=build)

        job_finished_handler(job)

        handler = MailNotificationHandler()
        recipients = handler.get_recipients(job)

        assert recipients == [
            '{0} <foo@example.com>'.format(author.name),
            'test@example.com',
            'bar@example.com',
        ]


class SendTestCase(TestCase):
    @mock.patch.object(MailNotificationHandler, 'get_recipients')
    def test_simple(self, get_recipients):
        build = self.create_build(self.project)
        job = self.create_job(build=build, result=Result.failed)
        logsource = LogSource(
            project=self.project,
            job=job,
            name='console',
        )
        db.session.add(logsource)

        logchunk = LogChunk(
            project=self.project,
            job=job,
            source=logsource,
            offset=0,
            size=11,
            text='hello world',
        )
        db.session.add(logchunk)

        job_link = 'http://example.com/projects/test/builds/%s/jobs/%s/' % (build.id.hex, job.id.hex,)
        log_link = '%slogs/%s/' % (job_link, logsource.id.hex)
        db.session.commit()

        get_recipients.return_value = ['foo@example.com', 'Bob <bob@example.com>']

        handler = MailNotificationHandler()
        handler.send(job, None)

        assert len(self.outbox) == 1
        msg = self.outbox[0]

        assert msg.subject == 'Build Failed - %s #%s.%s (%s)' % (
            job.project.name, job.build.number, job.number, job.build.source.revision_sha)
        assert msg.recipients == ['foo@example.com', 'Bob <bob@example.com>']
        assert msg.extra_headers['Reply-To'] == 'foo@example.com, Bob <bob@example.com>'
        print msg.body

        assert job_link in msg.html
        assert job_link in msg.body
        assert log_link in msg.html
        assert log_link in msg.body

        assert msg.as_string()


class GetJobOptionsTestCase(TestCase):
    def test_simple(self):
        project = self.project
        plan = self.create_plan()
        plan.projects.append(project)
        build = self.create_build(project)
        job = self.create_job(build)
        self.create_job_plan(job, plan)

        db.session.add(ItemOption(
            item_id=plan.id,
            name='mail.notify-author',
            value='0',
        ))

        db.session.add(ProjectOption(
            project_id=project.id,
            name='mail.notify-author',
            value='1',
        ))

        db.session.add(ProjectOption(
            project_id=project.id,
            name='mail.notify-addresses',
            value='foo@example.com',
        ))
        db.session.commit()

        handler = MailNotificationHandler()
        assert handler.get_job_options(job) == {
            'mail.notify-addresses': 'foo@example.com',
            'mail.notify-author': '0',
        }
