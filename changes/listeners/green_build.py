import calendar
import logging
import requests

from requests.exceptions import HTTPError

from datetime import datetime
from flask import current_app
from time import time
from sqlalchemy.orm import joinedload

from changes.config import db
from changes.constants import Result
from changes.db.utils import create_or_update
from changes.lib import build_type
from changes.models.build import Build
from changes.models.event import Event, EventType
from changes.models.project import ProjectOption
from changes.models.repository import RepositoryBackend
from changes.models.latest_green_build import LatestGreenBuild
from changes.utils.http import build_web_uri
from changes.utils.locking import lock
from changes.vcs.base import (
    ConcurrentUpdateError, UnknownChildRevision, UnknownParentRevision
)

logger = logging.getLogger('green_build')


def get_options(project_id):
    return dict(
        db.session.query(
            ProjectOption.name, ProjectOption.value
        ).filter(
            ProjectOption.project_id == project_id,
            ProjectOption.name.in_([
                'green-build.notify', 'green-build.project', 'build.branch-names'
            ])
        )
    )


def get_release_id(source, vcs):
    """Return an ID of the form counter:hash"""
    # green_build requires an identifier that is <integer:revision_sha>
    # the integer must also be sequential and unique
    # TODO(dcramer): it's a terrible API and realistically we should just be
    # sending a sha, as the sequential counter is hg-only, invalid, and really
    # isn't used
    if source.repository.backend == RepositoryBackend.hg:
        return vcs.run(
            ['log', '-r %s' % (source.revision_sha,), '--limit=1', '--template={rev}:{node|short}'])
    elif source.repository.backend == RepositoryBackend.git:
        counter = vcs.run(['rev-list', source.revision_sha, '--count']).strip()
        return '%s:%s' % (counter, source.revision_sha)
    else:
        return '%d:%s' % (time(), source.revision_sha)


@lock
def build_finished_handler(build_id, **kwargs):
    """Update the latest green build if this is a green build for a commit.

    Also, send a green build notification if the project is configured to do so.
    """
    build = Build.query.get(build_id)
    if build is None:
        return

    if build.result != Result.passed:
        return

    source = build.source

    if not build_type.is_any_commit_build(build):
        logger.debug('Ignoring build due to non-commit: %s', build.id)
        return

    vcs = source.repository.get_vcs()
    if vcs is None:
        logger.info('Repository has no VCS set: %s', source.repository.id)
        return

    # ensure we have the latest changes
    if vcs.exists():
        try:
            vcs.update()
        except ConcurrentUpdateError:
            # Retry once if it was already updating.
            vcs.update()
    else:
        vcs.clone()

    # set latest_green_build if latest for each branch:
    _set_latest_green_build_for_each_branch(build, source, vcs)

    # Send green build notifications if configured to do so.
    url = current_app.config.get('GREEN_BUILD_URL')
    if not url:
        logger.info('GREEN_BUILD_URL not set')
        return

    auth = current_app.config['GREEN_BUILD_AUTH']
    if not auth:
        logger.info('GREEN_BUILD_AUTH not set')
        return

    options = get_options(build.project_id)

    if options.get('green-build.notify', '1') != '1':
        logger.info('green-build.notify disabled for project: %s', build.project_id)
        return

    branch_names = filter(bool, options.get('build.branch-names', '*').split(' '))
    if not source.revision.should_build_branch(branch_names):
        return

    release_id = get_release_id(source, vcs)

    project = options.get('green-build.project') or build.project.slug
    committed_timestamp_sec = calendar.timegm(source.revision.date_committed.utctimetuple())

    logging.info('Making green_build request to %s', url)
    try:
        requests.post(url, auth=auth, timeout=10, data={
            'project': project,
            'id': release_id,
            'build_url': build_web_uri('/projects/{0}/builds/{1}/'.format(
                build.project.slug, build.id.hex)),
            'build_server': 'changes',
            'author_name': source.revision.author.name,
            'author_email': source.revision.author.email,
            'commit_timestamp': committed_timestamp_sec,
            'revision_message': source.revision.message,
        }).raise_for_status()
    except HTTPError as ex:
        # Conflicts aren't necessarily failures; some green build receivers
        # report conflict if they see out-of-order results (not uncommon in Changes).
        # We want to track those situations independently of other non-success responses.
        # NOTE: We compare `ex.response` to None explicitly because any non-200 response
        # evaluates to `False`.
        if ex.response is not None and ex.response.status_code == 409:
            logger.warning("Conflict when reporting green build", extra={
                'data': {
                    'project': project,
                    'release_id': release_id,
                    'build_id': build.id.hex,
                }
            })
        else:
            logger.exception('Failed to report green build')
        status = 'fail'
    except Exception:
        logger.exception('Failed to report green build')
        status = 'fail'
    else:
        status = 'success'

    create_or_update(Event, where={
        'type': EventType.green_build,
        'item_id': build.id,
    }, values={
        'data': {
            'status': status,
        },
        'date_modified': datetime.utcnow(),
    })


def _set_latest_green_build_for_each_branch(build, source, vcs):
    project = build.project
    for branch in source.revision.branches:
        current_latest_green_build = LatestGreenBuild.query.options(
            joinedload('build').joinedload('source')
        ).filter(
            LatestGreenBuild.project_id == project.id,
            LatestGreenBuild.branch == branch).first()

        if current_latest_green_build:
            child_in_question = source.revision_sha
            parent_in_question = current_latest_green_build.build.source.revision_sha
            try:
                if not vcs.is_child_parent(
                     child_in_question=child_in_question,
                     parent_in_question=parent_in_question):
                    return
            except UnknownChildRevision:
                # The child_in_question is an unknown SHA. This shouldn't happen.
                logging.exception(
                    "Child SHA is missing from the VCS. This is bad news and "
                    "shouldn't happen. (parent=%s, child=%s)",
                    parent_in_question, child_in_question)
                return
            except UnknownParentRevision:
                # The parent_in_question is an unknown SHA. Assume it was
                # deleted and forgotten VCS, and set the new green build to
                # the child_in_question anyway.
                logging.warning(
                    "Parent SHA is missing from the VCS. Assume it was deleted "
                    "and the new build is legit.",
                    extra={
                        'data': {
                            'repository_url': source.revision.repository.url,
                            'branch': branch,
                            'parent': parent_in_question,
                            'child': child_in_question,
                        },
                    }
                )

        # switch latest_green_build to this sha
        green_build, _ = create_or_update(LatestGreenBuild, where={
            'project_id': project.id,
            'branch': branch,
        }, values={
            'build': build,
        })
