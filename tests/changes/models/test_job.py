from __future__ import absolute_import

import mock

from changes.models.jobplan import JobPlan
from changes.testutils import TestCase


class AutogeneratedJobTest(TestCase):
    @mock.patch('changes.models.project.Project.get_config')
    def test_autogenerated_commands(self, get_config):
        get_config.return_value = {
            'bazel.targets': [
                '//aa/bb/cc/...',
                '//aa/abc/...',
            ],
        }

        project = self.create_project()
        build = self.create_build(project)
        job = self.create_job(build, autogenerated=True)

        _, implementation = JobPlan.get_build_step_for_job(job.id)

        assert len(implementation.commands) == 2
        assert implementation.commands[1].script == 'bazel test //aa/bb/cc/... //aa/abc/...'