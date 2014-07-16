from __future__ import absolute_import

import mock

from datetime import datetime, timedelta

from changes.config import db
from changes.constants import Result, Status
from changes.jobs.sync_job_step import (
    sync_job_step, is_missing_tests, has_timed_out
)
from changes.models import (
    ItemOption, ItemStat, JobStep, HistoricalImmutableStep, Task, FileCoverage,
    TestCase, FailureReason
)
from changes.testutils import TestCase as BaseTestCase


class HasTimedOutTest(TestCase):
    def test_simple(self):
        project = self.create_project()
        plan = self.create_plan()
        plan.projects.append(project)
        step = self.create_step(plan)

        build = self.create_build(project=project)
        job = self.create_job(build=build, status=Status.queued)
        jobplan = self.create_job_plan(job, plan)

        option = ItemOption(
            item_id=step.id,
            name='build.timeout',
            value='5',
        )
        db.session.add(option)
        db.session.commit()

        jobphase = self.create_jobphase(job)
        jobstep = self.create_jobstep(jobphase)

        assert not has_timed_out(jobstep, jobplan)

        jobstep.status = Status.in_progress
        jobstep.date_started = datetime.utcnow()
        db.session.add(jobstep)
        db.session.commit()

        assert not has_timed_out(jobstep, jobplan)

        jobstep.date_started = datetime.utcnow() - timedelta(seconds=400)
        db.session.add(jobstep)
        db.session.commit()

        assert has_timed_out(jobstep, jobplan)

        jobplan.data['snapshot']['options'][option.name] = '0'
        db.session.add(jobplan)
        db.session.commit()

        assert not has_timed_out(jobstep, jobplan)

        jobplan.data['snapshot']['options'][option.name] = '500'
        db.session.add(jobplan)
        db.session.commit()

        assert not has_timed_out(jobstep, jobplan)


class IsMissingTestsTest(BaseTestCase):
    def test_single_phase(self):
        project = self.create_project()
        plan = self.create_plan()

        option = ItemOption(
            item_id=plan.id,
            name='build.expect-tests',
            value='0',
        )
        db.session.add(option)
        db.session.commit()

        build = self.create_build(project=project)
        job = self.create_job(build=build)
        jobplan = self.create_job_plan(job, plan)
        jobphase = self.create_jobphase(
            job=job,
            date_started=datetime(2013, 9, 19, 22, 15, 24),
        )
        jobstep = self.create_jobstep(jobphase)
        jobstep2 = self.create_jobstep(jobphase)

        assert not is_missing_tests(jobstep, jobplan)

        jobplan.data['snapshot']['options'][option.name] = '1'
        db.session.add(jobplan)
        db.session.commit()

        assert is_missing_tests(jobstep, jobplan)

        testcase = TestCase(
            project_id=project.id,
            job_id=job.id,
            step_id=jobstep2.id,
            name='test',
        )
        db.session.add(testcase)
        db.session.commit()

        assert is_missing_tests(jobstep, jobplan)

        testcase = TestCase(
            project_id=project.id,
            job_id=job.id,
            step_id=jobstep.id,
            name='test2',
        )
        db.session.add(testcase)
        db.session.commit()

        assert not is_missing_tests(jobstep, jobplan)

    def test_multi_phase(self):
        project = self.create_project()

        plan = self.create_plan()

        option = ItemOption(
            item_id=plan.id,
            name='build.expect-tests',
            value='1',
        )
        db.session.add(option)
        db.session.commit()

        build = self.create_build(project=project)
        job = self.create_job(build=build)
        jobplan = self.create_job_plan(job, plan)
        jobphase = self.create_jobphase(
            job=job,
            label='setup',
            # it's important that the date_created here is actually newer
            # than the second phase
            date_created=datetime(2013, 9, 19, 22, 17, 24),
            date_started=datetime(2013, 9, 19, 22, 15, 24),
        )
        jobphase2 = self.create_jobphase(
            job=job,
            label='test',
            date_created=datetime(2013, 9, 19, 22, 16, 24),
            date_started=datetime(2013, 9, 19, 22, 16, 24),
        )
        jobstep = self.create_jobstep(jobphase)
        jobstep2 = self.create_jobstep(jobphase2)

        assert not is_missing_tests(jobstep, jobplan)
        assert is_missing_tests(jobstep2, jobplan)

        testcase = TestCase(
            project_id=project.id,
            job_id=job.id,
            step_id=jobstep.id,
            name='test',
        )
        db.session.add(testcase)
        db.session.commit()

        assert not is_missing_tests(jobstep, jobplan)
        assert is_missing_tests(jobstep2, jobplan)

        testcase = TestCase(
            project_id=project.id,
            job_id=job.id,
            step_id=jobstep2.id,
            name='test2',
        )
        db.session.add(testcase)
        db.session.commit()

        assert not is_missing_tests(jobstep2, jobplan)


class SyncJobStepTest(BaseTestCase):
    @mock.patch('changes.config.queue.delay')
    @mock.patch.object(HistoricalImmutableStep, 'get_implementation')
    def test_in_progress(self, get_implementation, queue_delay):
        implementation = mock.Mock()
        get_implementation.return_value = implementation

        def mark_in_progress(step):
            step.status = Status.in_progress

        project = self.create_project()
        build = self.create_build(project=project)
        job = self.create_job(build=build)

        plan = self.create_plan()
        self.create_step(plan, implementation='test', order=0)
        self.create_job_plan(job, plan)

        phase = self.create_jobphase(job)
        step = self.create_jobstep(phase)
        task = self.create_task(
            parent_id=job.id,
            task_id=step.id,
            task_name='sync_job_step',
        )

        db.session.add(ItemStat(item_id=job.id, name='tests_missing', value=1))
        db.session.commit()

        implementation.update_step.side_effect = mark_in_progress

        sync_job_step(
            step_id=step.id.hex,
            task_id=step.id.hex,
            parent_task_id=job.id.hex,
        )

        get_implementation.assert_called_once_with()

        implementation.update_step.assert_called_once_with(
            step=step
        )

        db.session.expire(step)
        db.session.expire(task)

        step = JobStep.query.get(step.id)

        assert step.status == Status.in_progress

        task = Task.query.get(task.id)

        assert task.status == Status.in_progress

        queue_delay.assert_any_call('sync_job_step', kwargs={
            'step_id': step.id.hex,
            'task_id': step.id.hex,
            'parent_task_id': job.id.hex,
        }, countdown=5)

    @mock.patch('changes.config.queue.delay')
    @mock.patch.object(HistoricalImmutableStep, 'get_implementation')
    def test_finished(self, get_implementation, queue_delay):
        implementation = mock.Mock()
        get_implementation.return_value = implementation

        def mark_finished(step):
            step.status = Status.finished
            step.result = Result.failed

        implementation.update_step.side_effect = mark_finished

        project = self.create_project()
        build = self.create_build(project=project)
        job = self.create_job(build=build)

        plan = self.create_plan()
        self.create_step(plan, implementation='test', order=0)
        self.create_job_plan(job, plan)

        phase = self.create_jobphase(job)
        step = self.create_jobstep(phase)
        task = self.create_task(
            parent_id=job.id,
            task_id=step.id,
            task_name='sync_job_step',
            status=Status.finished,
        )

        db.session.add(TestCase(
            name='test',
            step_id=step.id,
            job_id=job.id,
            project_id=project.id,
            result=Result.failed,
        ))

        db.session.add(FileCoverage(
            job=job, step=step, project=job.project,
            filename='foo.py', data='CCCUUUCCCUUNNN',
            lines_covered=6,
            lines_uncovered=5,
            diff_lines_covered=3,
            diff_lines_uncovered=2,
        ))
        db.session.commit()

        sync_job_step(
            step_id=step.id.hex,
            task_id=step.id.hex,
            parent_task_id=job.id.hex,
        )

        get_implementation.assert_called_once_with()

        implementation.update_step.assert_called_once_with(
            step=step
        )

        db.session.expire(step)
        db.session.expire(task)

        step = JobStep.query.get(step.id)

        assert step.status == Status.finished

        task = Task.query.get(task.id)

        assert task.status == Status.finished

        assert len(queue_delay.mock_calls) == 0

        stat = ItemStat.query.filter(
            ItemStat.item_id == step.id,
            ItemStat.name == 'tests_missing',
        ).first()
        assert stat.value == 0

        stat = ItemStat.query.filter(
            ItemStat.item_id == step.id,
            ItemStat.name == 'lines_covered',
        ).first()
        assert stat.value == 6

        stat = ItemStat.query.filter(
            ItemStat.item_id == step.id,
            ItemStat.name == 'lines_uncovered',
        ).first()
        assert stat.value == 5

        stat = ItemStat.query.filter(
            ItemStat.item_id == step.id,
            ItemStat.name == 'diff_lines_covered',
        ).first()
        assert stat.value == 3

        stat = ItemStat.query.filter(
            ItemStat.item_id == step.id,
            ItemStat.name == 'diff_lines_uncovered',
        ).first()
        assert stat.value == 2

        assert FailureReason.query.filter(
            FailureReason.step_id == step.id,
            FailureReason.reason == 'test_failures',
        )

    @mock.patch('changes.config.queue.delay')
    @mock.patch.object(HistoricalImmutableStep, 'get_implementation')
    def test_missing_test_results_and_expected(self, get_implementation, queue_delay):
        implementation = mock.Mock()
        get_implementation.return_value = implementation

        def mark_finished(step):
            step.status = Status.finished
            step.result = Result.passed

        implementation.update_step.side_effect = mark_finished

        project = self.create_project()
        build = self.create_build(project=project)
        job = self.create_job(build=build)

        plan = self.create_plan()
        self.create_step(plan, implementation='test', order=0)

        db.session.add(ItemOption(
            item_id=plan.id,
            name='build.expect-tests',
            value='1'
        ))
        db.session.commit()

        self.create_job_plan(job, plan)

        phase = self.create_jobphase(
            job=job,
            date_started=datetime(2013, 9, 19, 22, 15, 24),
        )
        step = self.create_jobstep(phase)

        sync_job_step(
            step_id=step.id.hex,
            task_id=step.id.hex,
            parent_task_id=job.id.hex,
        )

        db.session.expire(step)

        step = JobStep.query.get(step.id)

        assert step.status == Status.finished
        assert step.result == Result.failed

        stat = ItemStat.query.filter(
            ItemStat.item_id == step.id,
            ItemStat.name == 'tests_missing',
        ).first()
        assert stat.value == 1

        assert FailureReason.query.filter(
            FailureReason.step_id == step.id,
            FailureReason.reason == 'missing_tests',
        )

    @mock.patch('changes.jobs.sync_job_step.has_timed_out')
    @mock.patch.object(HistoricalImmutableStep, 'get_implementation')
    def test_timed_out(self, get_implementation, mock_has_timed_out):
        implementation = mock.Mock()
        get_implementation.return_value = implementation

        project = self.create_project()
        build = self.create_build(project=project)
        job = self.create_job(build=build)

        plan = self.create_plan()
        self.create_step(plan, implementation='test', order=0)
        jobplan = self.create_job_plan(job, plan)

        phase = self.create_jobphase(job)
        step = self.create_jobstep(phase, status=Status.in_progress)

        mock_has_timed_out.return_value = True

        sync_job_step(
            step_id=step.id.hex,
            task_id=step.id.hex,
            parent_task_id=job.id.hex
        )

        mock_has_timed_out.assert_called_once_with(step, jobplan)

        get_implementation.assert_called_once_with()
        implementation.cancel_step.assert_called_once_with(
            step=step,
        )

        assert step.result == Result.failed

        assert FailureReason.query.filter(
            FailureReason.step_id == step.id,
            FailureReason.reason == 'timeout',
        )
