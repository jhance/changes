from __future__ import absolute_import

import json

from datetime import datetime
from flask_restful.reqparse import RequestParser

from changes.api.base import APIView, error
from changes.api.validators.datetime import ISODatetime
from changes.config import db, redis, statsreporter
from changes.constants import Result, Status
from changes.expanders.commands import CommandsExpander
from changes.expanders.tests import TestsExpander
from changes.jobs.sync_job_step import sync_job_step
from changes.models.command import Command, CommandType
from changes.models.jobphase import JobPhase
from changes.models.jobplan import JobPlan


STATUS_CHOICES = ('queued', 'in_progress', 'finished')

EXPANDERS = {
    CommandType.collect_steps: CommandsExpander,
    CommandType.collect_tests: TestsExpander,
}


class CommandDetailsAPIView(APIView):
    post_parser = RequestParser()
    post_parser.add_argument('status', choices=STATUS_CHOICES)
    post_parser.add_argument('return_code', type=int)
    post_parser.add_argument('date', type=ISODatetime())
    # output is required for various collectors, and is the buffered response
    # of the command sent
    post_parser.add_argument('output', type=json.loads)

    def get(self, command_id):
        command = Command.query.get(command_id)
        if command is None:
            return '', 404

        return self.respond(command)

    def post(self, command_id):
        args = self.post_parser.parse_args()

        current_datetime = args.date or datetime.utcnow()

        # We need to lock this resource to ensure the command doesn't get expanded
        # twice in the time it's checking the attr + writing the updated value
        if args.output or args.status == 'finished':
            lock = redis.lock('expand:{}'.format(command_id), expire=15, nowait=True)
        else:
            lock = None

        if lock:
            lock.__enter__()

        try:
            command = Command.query.get(command_id)
            if command is None:
                return '', 404

            if command.status == Status.finished:
                return error("Command already marked as finished")

            if args.return_code is not None:
                command.return_code = args.return_code

            if args.status:
                command.status = Status[args.status]

                # if we've finished this job, lets ensure we have set date_finished
                if command.status == Status.finished and command.date_finished is None:
                    command.date_finished = current_datetime
                elif command.status != Status.finished and command.date_finished:
                    command.date_finished = None

                if command.status != Status.queued and command.date_started is None:
                    command.date_started = current_datetime
                elif command.status == Status.queued and command.date_started:
                    command.date_started = None

            db.session.add(command)
            db.session.flush()

            if args.output or args.status == 'finished':
                # don't expand a jobstep that already failed
                if command.jobstep.result in (Result.aborted, Result.failed, Result.infra_failed):
                    statsreporter.stats().incr('command_expansion_aborted')
                    return self.respond(command)
                expander_cls = self.get_expander(command.type)
                if expander_cls is not None:
                    if not args.output:
                        db.session.rollback()
                        return error("Missing output for command of type %s" % command.type)

                    expander = expander_cls(
                        project=command.jobstep.project,
                        data=args.output,
                    )

                    try:
                        expander.validate()
                    except AssertionError as e:
                        db.session.rollback()
                        return error('%s' % e)
                    except Exception:
                        db.session.rollback()
                        return '', 500

                    self.expand_command(command, expander, args.output)

            db.session.commit()

        finally:
            if lock:
                lock.__exit__(None, None, None)

        return self.respond(command)

    def get_expander(self, type):
        return EXPANDERS.get(type)

    def expand_command(self, command, expander, data):
        jobstep = command.jobstep
        phase_name = data.get('phase')
        if not phase_name:
            phase_name = expander.default_phase_name()

        new_jobphase = JobPhase(
            job_id=jobstep.job_id,
            project_id=jobstep.project_id,
            label=phase_name,
            status=Status.queued,
        )
        db.session.add(new_jobphase)

        _, buildstep = JobPlan.get_build_step_for_job(jobstep.job_id)

        results = []
        for future_jobstep in expander.expand(max_executors=jobstep.data['max_executors'],
                                              test_stats_from=buildstep.get_test_stats_from()):
            new_jobstep = buildstep.create_expanded_jobstep(jobstep, new_jobphase, future_jobstep)
            results.append(new_jobstep)

        # If there are no tests to run, the phase is done.
        if len(results) == 0:
            new_jobphase.status = Status.finished
            new_jobphase.result = Result.passed
            db.session.add(new_jobphase)

        db.session.flush()

        for new_jobstep in results:
            sync_job_step.delay_if_needed(
                step_id=new_jobstep.id.hex,
                task_id=new_jobstep.id.hex,
                parent_task_id=new_jobphase.job.id.hex,
            )

        return results
