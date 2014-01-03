from datetime import datetime
from flask import current_app
from sqlalchemy.orm import subqueryload_all
import sys

from changes.backends.base import UnrecoverableException
from changes.config import db, queue
from changes.constants import Status, Result
from changes.events import publish_build_update, publish_job_update
from changes.models import Build, Job, JobPlan, Plan
from changes.utils.locking import lock


def _sync_job(job_id):
    job = Job.query.get(job_id)
    if not job:
        return

    if job.status == Status.finished:
        return

    prev_status = job.status

    # TODO(dcramer): we make an assumption that there is a single step
    job_plan = JobPlan.query.options(
        subqueryload_all('plan.steps')
    ).filter(
        JobPlan.job_id == job.id,
    ).join(Plan).first()
    try:
        if not job_plan:
            raise UnrecoverableException('Got sync_job task without job plan: %s' % (job_id,))

        try:
            step = job_plan.plan.steps[0]
        except IndexError:
            raise UnrecoverableException('Missing steps for plan')

        implementation = step.get_implementation()
        implementation.execute(job=job)

    except UnrecoverableException:
        job.status = Status.finished
        job.result = Result.aborted
        current_app.logger.exception('Unrecoverable exception syncing %s', job_id)

    current_datetime = datetime.utcnow()

    job.date_modified = current_datetime
    db.session.add(job)

    db.session.commit()

    # this might be the first job firing for the build, so ensure we update the
    # build if its applicable
    if job.build_id and job.status != prev_status:
        Build.query.filter(
            Build.id == job.build_id,
            Build.status.in_([Status.queued, Status.unknown]),
        ).update({
            Build.status: job.status,
            Build.date_started: job.date_started,
            Build.date_modified: current_datetime,
        }, synchronize_session=False)

        db.session.commit()

        build = Build.query.get(job.build_id)

        publish_build_update(build)

    # if this job isnt finished, we assume that there's still data to sync
    if job.status != Status.finished:
        queue.delay('sync_job', kwargs={
            'job_id': job.id.hex
        }, countdown=5)
    else:
        if job.build_id:
            queue.delay('update_build_result', kwargs={
                'build_id': job.build_id.hex,
                'job_id': job.id.hex,
            })

        queue.delay('update_project_stats', kwargs={
            'project_id': job.project_id.hex,
        }, countdown=1)

        queue.delay('notify_listeners', kwargs={
            'job_id': job.id.hex,
            'signal_name': 'job.finished',
        })

        if job_plan:
            queue.delay('update_project_plan_stats', kwargs={
                'project_id': job.project_id.hex,
                'plan_id': job_plan.plan_id.hex,
            }, countdown=1)

    publish_job_update(job)


@lock
def sync_job(job_id):
    try:
        _sync_job(job_id)

    except Exception:
        # Ensure we continue to synchronize this job as this could be a
        # temporary failure
        current_app.logger.exception('Failed to sync job %s', job_id)
        raise queue.retry('sync_job', kwargs={
            'job_id': job_id,
        }, exc=sys.exc_info(), countdown=60)
