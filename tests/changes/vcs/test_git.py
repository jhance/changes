from tests.changes.vcs.asserts import VcsAsserts

class GitVcsTest(TestCase, VcsAsserts):
    def _get_last_two_revisions(self, marker, revisions):
        if marker in revisions[0].branches:
            return revisions[0], revisions[1]
        else:
            return revisions[1], revisions[0]

    def _set_author(self, name, email):
        check_call('cd {0} && git config --replace-all "user.name" "{1}"'
                   .format(self.remote_path, name), shell=True)
        check_call('cd {0} && git config --replace-all "user.email" "{1}"'
                   .format(self.remote_path, email), shell=True)

        self._set_author('Foo Bar', 'foo@example.com')
    def test_log_with_authors(self):
        vcs = self.get_vcs()

        # Create a commit with a new author
        self._set_author('Another Committer', 'ac@d.not.zm.exist')
        check_call('cd %s && touch BAZ && git add BAZ && git commit -m "bazzy"'
                   % self.remote_path, shell=True)
        vcs.clone()
        vcs.update()
        revisions = list(vcs.log())
        assert len(revisions) == 3

        revisions = list(vcs.log(author='Another Committer'))
        assert len(revisions) == 1
        self.assertRevision(revisions[0],
                            author='Another Committer <ac@d.not.zm.exist>',
                            message='bazzy')

        revisions = list(vcs.log(author='ac@d.not.zm.exist'))
        assert len(revisions) == 1
        self.assertRevision(revisions[0],
                            author='Another Committer <ac@d.not.zm.exist>',
                            message='bazzy')

        revisions = list(vcs.log(branch=vcs.get_default_revision(),
                                 author='Foo'))
        assert len(revisions) == 2

    def test_log_throws_errors_when_needed(self):
        vcs = self.get_vcs()

        try:
            vcs.log(parent='HEAD', branch='master').next()
            self.fail('log passed with both branch and master specified')
        except ValueError:
            pass

    def test_log_with_branches(self):
        vcs = self.get_vcs()

        # Create another branch and move it ahead of the master branch
        check_call('cd %s && git checkout -b B2' % self.remote_path, shell=True)
        check_call('cd %s && touch BAZ && git add BAZ && git commit -m "second branch commit"' % (
            self.remote_path,
        ), shell=True)

        # Create a third branch off master with a commit not in B2
        check_call('cd %s && git checkout %s' % (
            self.remote_path, vcs.get_default_revision(),
        ), shell=True)
        check_call('cd %s && git checkout -b B3' % self.remote_path, shell=True)
        check_call('cd %s && touch IPSUM && git add IPSUM && git commit -m "3rd branch"' % (
            self.remote_path,
        ), shell=True)
        vcs.clone()
        vcs.update()

        # Ensure git log normally includes commits from all branches
        revisions = list(vcs.log())
        assert len(revisions) == 4

        # Git timestamps are only accurate to the second. But since this test
        #   creates these commits so close to each other, there's a race
        #   condition here. Ultimately, we only care that both commits appear
        #   last in the log, so allow them to be out of order.
        last_rev, previous_rev = self._get_last_two_revisions('B3', revisions)
        self.assertRevision(last_rev,
                            message='3rd branch',
                            branches=['B3'])
        self.assertRevision(previous_rev,
                            message='second branch commit',
                            branches=['B2'])

        # Note that the list of branches here differs from the hg version
        #   because hg only returns the branch name from the changeset, which
        #   does not include any ancestors.
        self.assertRevision(revisions[3],
                            message='test',
                            branches=[vcs.get_default_revision(), 'B2', 'B3'])

        # Ensure git log with B3 only
        revisions = list(vcs.log(branch='B3'))
        assert len(revisions) == 3
        self.assertRevision(revisions[0],
                            message='3rd branch',
                            branches=['B3'])
        self.assertRevision(revisions[2],
                            message='test',
                            branches=[vcs.get_default_revision(), 'B2', 'B3'])

        # Sanity check master
        check_call('cd %s && git checkout %s' % (
            self.remote_path, vcs.get_default_revision(),
        ), shell=True)
        revisions = list(vcs.log(branch=vcs.get_default_revision()))
        assert len(revisions) == 2

        revision = vcs.log(parent='HEAD', limit=1).next()
        self.assertRevision(revision,
                            author='Foo Bar <foo@example.com>',
                            message='biz\nbaz\n',
                            subject='biz')

    def test_get_known_branches(self):
        vcs = self.get_vcs()
        vcs.clone()
        vcs.update()

        branches = vcs.get_known_branches()
        self.assertEquals(1, len(branches))
        self.assertIn('master', branches)

        check_call('cd %s && git checkout -B test_branch' % self.remote_path,
                   shell=True)
        vcs.update()
        branches = vcs.get_known_branches()
        self.assertEquals(2, len(branches))
        self.assertIn('test_branch', branches)