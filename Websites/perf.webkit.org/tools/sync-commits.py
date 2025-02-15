#!/usr/bin/python

import argparse
import json
import os.path
import re
import subprocess
import sys
import time
import urllib2

from datetime import datetime
from abc import ABCMeta, abstractmethod
from xml.dom.minidom import parseString as parseXmlString
from util import load_server_config
from util import submit_commits
from util import text_content

# There are some buggy commit messages:
# Canonical link: https://commits.webkit.org/https://commits.webkit.org/232477@main
REVISION_IDENTIFIER_IN_MSG_RE = re.compile(r'^Canonical link: (https\://commits\.webkit\.org/)+(?P<revision_identifier>\d+\.?\d*@(?P<branch_name>[\w\.\-]+))\n', flags=re.MULTILINE)
REVISION_IN_MSG_RE = re.compile(r'^git-svn-id: https://svn\.webkit\.org/repository/webkit/[\w\W]+@(?P<revision>\d+) [\w\d\-]+\n', flags=re.MULTILINE)
HASH_RE = re.compile(r'^[a-f0-9A-F]+$')
REVISION_RE = re.compile(r'^[Rr]?(?P<revision>\d+)$')

def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository-config-json', required=True, help='The path to a JSON file that specifies subversion syncing options')
    parser.add_argument('--server-config-json', required=True, help='The path to a JSON file that specifies the perf dashboard')
    parser.add_argument('--seconds-to-sleep', type=float, default=900, help='The seconds to sleep between iterations')
    parser.add_argument('--max-fetch-count', type=int, default=10, help='The number of commits to fetch at once')
    parser.add_argument('--max-ancestor-fetch-count', type=int, default=100, help='The number of commits to fetch at once if some commits are missing previous commits')
    args = parser.parse_args()

    with open(args.repository_config_json) as repository_config_json:
        repositories = [load_repository(repository_info) for repository_info in json.load(repository_config_json)]

    while True:
        server_config = load_server_config(args.server_config_json)
        for repository in repositories:
            try:
                repository.fetch_commits_and_submit(server_config, args.max_fetch_count, args.max_ancestor_fetch_count)
            except Exception as error:
                print "Failed to fetch and sync:", error

        print "Sleeping for %d seconds..." % args.seconds_to_sleep
        time.sleep(args.seconds_to_sleep)


def load_repository(repository):
    if 'gitCheckout' in repository:
        return GitRepository(
            name=repository['name'], git_url=repository['url'], git_checkout=repository['gitCheckout'],
            git_branch=repository.get('branch'), report_revision_identifier_in_commit_msg=repository.get('reportRevisionIdentifier'),
            report_svn_revision=repository.get('reportSVNRevision'),
            max_revision=repository.get('maxRevision'))
    return SVNRepository(name=repository['name'], svn_url=repository['url'], should_trust_certificate=repository.get('trustCertificate', False),
        use_server_auth=repository.get('useServerAuth', False), account_name_script_path=repository.get('accountNameFinderScript'))


class Repository(object):
    ___metaclass___ = ABCMeta

    _name_account_compound_regex = re.compile(r'^\s*(?P<name>(\".+\"|[^<]+?))\s*\<(?P<account>.+)\>\s*$')

    def __init__(self, name):
        self._name = name
        self._last_fetched = None

    def fetch_commits_and_submit(self, server_config, max_fetch_count, max_ancestor_fetch_count):
        if not self._last_fetched:
            print "Determining the starting revision for %s" % self._name
            self._last_fetched = self.determine_last_reported_revision(server_config)

        pending_commits = []
        for unused in range(max_fetch_count):
            commit = self.fetch_next_commit(server_config, self._last_fetched)
            if not commit:
                break
            pending_commits += [commit]
            self._last_fetched = commit['revision']

        if not pending_commits:
            print "No new revision found for %s (last fetched: %s)" % (self._name, self.format_revision(self._last_fetched))
            return

        for unused in range(max_ancestor_fetch_count):
            revision_list = ', '.join([self.format_revision(commit['revision']) for commit in pending_commits])
            print "Submitting revisions %s for %s to %s" % (revision_list, self._name, server_config['server']['url'])

            result = submit_commits(pending_commits, server_config['server']['url'],
                server_config['worker']['name'], server_config['worker']['password'], ['OK', 'FailedToFindPreviousCommit'])

            if result.get('status') == 'OK':
                break

            if result.get('status') == 'FailedToFindPreviousCommit':
                print result
                previous_commit = self.fetch_commit(server_config, result['commit']['previousCommit'])
                if not previous_commit:
                    raise Exception('Could not find the previous commit %s of %s' % (result['commit']['previousCommit'], result['commit']['revision']))
                pending_commits = [previous_commit] + pending_commits

        if result.get('status') != 'OK':
            raise Exception(result)

        print "Successfully submitted."
        print

    @abstractmethod
    def fetch_next_commit(self, server_config, last_fetched):
        pass

    @abstractmethod
    def fetch_commit(self, server_config, last_fetched):
        pass

    @abstractmethod
    def format_revision(self, revision):
        pass

    def determine_last_reported_revision(self, server_config):
        last_reported_revision = self.fetch_revision_from_dasbhoard(server_config, 'last-reported')
        if last_reported_revision:
            return last_reported_revision

    def fetch_revision_from_dasbhoard(self, server_config, filter):
        result = urllib2.urlopen(server_config['server']['url'] + '/api/commits/' + self._name + '/' + filter).read()
        parsed_result = json.loads(result)
        if parsed_result['status'] != 'OK' and parsed_result['status'] != 'RepositoryNotFound':
            raise Exception(result)
        commits = parsed_result.get('commits')
        return commits[0]['revision'] if commits else None


class SVNRepository(Repository):

    def __init__(self, name, svn_url, should_trust_certificate, use_server_auth, account_name_script_path):
        assert not account_name_script_path or isinstance(account_name_script_path, list)
        super(SVNRepository, self).__init__(name)
        self._svn_url = svn_url
        self._should_trust_certificate = should_trust_certificate
        self._use_server_auth = use_server_auth
        self._account_name_script_path = account_name_script_path

    def fetch_next_commit(self, server_config, last_fetched):
        if not last_fetched:
            # FIXME: This is a problematic if dashboard can get results for revisions older than oldest_revision
            # in the future because we never refetch older revisions.
            last_fetched = self.fetch_revision_from_dasbhoard(server_config, 'oldest')

        revision_to_fetch = int(last_fetched) + 1

        args = ['svn', 'log', '--revision', str(revision_to_fetch), '--xml', self._svn_url, '--non-interactive']
        if self._use_server_auth and 'auth' in server_config['server']:
            server_auth = server_config['server']['auth']
            args += ['--no-auth-cache', '--username', server_auth['username'], '--password', server_auth['password']]
        if self._should_trust_certificate:
            args += ['--trust-server-cert']

        try:
            output = subprocess.check_output(args, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as error:
            if (': No such revision ' + str(revision_to_fetch)) in error.output:
                return None
            raise error

        xml = parseXmlString(output)
        time = text_content(xml.getElementsByTagName("date")[0])
        author_elements = xml.getElementsByTagName("author")
        author_account = text_content(author_elements[0]) if author_elements.length else None
        message = text_content(xml.getElementsByTagName("msg")[0])

        name = self._resolve_author_name(author_account) if author_account and self._account_name_script_path else None

        result = {
            'repository': self._name,
            'revision': revision_to_fetch,
            'time': time,
            'message': message,
        }

        if author_account:
            result['author'] = {'account': author_account, 'name': name}

        return result

    def _resolve_author_name(self, account):
        try:
            output = subprocess.check_output(self._account_name_script_path + [account])
        except subprocess.CalledProcessError:
            print 'Failed to resolve the name for account:', account
            return None

        match = Repository._name_account_compound_regex.match(output)
        if match:
            return match.group('name').strip('"')
        return output.strip()

    def format_revision(self, revision):
        return 'r' + str(revision)


class GitRepository(Repository):

    def __init__(self, name, git_checkout, git_url, git_branch=None, report_revision_identifier_in_commit_msg=False, report_svn_revision=False, max_revision=None):
        assert(os.path.isdir(git_checkout))
        super(GitRepository, self).__init__(name)
        self._git_checkout = git_checkout
        self._git_url = git_url
        self._git_branch = git_branch
        self._tokenized_hashes = []
        self._report_revision_identifier_in_commit_msg = report_revision_identifier_in_commit_msg
        self._report_svn_revision = report_svn_revision
        self._max_revision = max_revision

    def fetch_next_commit(self, server_config, last_fetched):
        if not last_fetched:
            self._fetch_all_hashes()
            tokens = self._tokenized_hashes[0]
        else:
            if self._report_svn_revision:
                last_fetched_git_hash = self._git_hash_from_svn_revision_hash_mixed(last_fetched)
                if not last_fetched_git_hash:
                    self._fetch_remote()
                    last_fetched_git_hash = self._git_hash_from_svn_revision_hash_mixed(last_fetched)
                    if not last_fetched_git_hash:
                        raise ValueError('Cannot find the git hash for the last fetched svn revision')
                last_fetched = last_fetched_git_hash
            tokens = self._find_next_hash(last_fetched)
            if not tokens:
                self._fetch_all_hashes()
                tokens = self._find_next_hash(last_fetched)
                if not tokens:
                    return None
        return self._revision_from_tokens(tokens)

    def fetch_commit(self, server_config, hash_to_find):
        assert(self._tokenized_hashes)
        for i, tokens in enumerate(self._tokenized_hashes):
            if tokens and tokens[0] == hash_to_find:
                return self._revision_from_tokens(tokens)
        return None

    def _svn_revision_from_git_hash(self, git_hash):
        message = self._run_git_command(['log', git_hash, '-1', '--pretty=%B'])
        revision_match = REVISION_IN_MSG_RE.search(message)
        if revision_match:
            return revision_match.group('revision')
        return self._run_git_command(['svn', 'find-rev', git_hash]).strip()

    def _git_hash_from_svn_revision_hash_mixed(self, something):
        if REVISION_RE.match(str(something)):
            return self._run_git_command(['svn', 'find-rev', 'r{}'.format(something)]).strip()
        elif HASH_RE.match(str(something)):
            return something

    def _revision_from_tokens(self, tokens):
        current_hash = tokens[0]
        commit_time = int(tokens[1])
        author_email = tokens[2]
        previous_hash = tokens[3] if len(tokens) >= 4 else None

        author_name = self._run_git_command(['log', current_hash, '-1', '--pretty=%cn'])
        message = self._run_git_command(['log', current_hash, '-1', '--pretty=%B'])

        revision_identifier = None
        if self._report_revision_identifier_in_commit_msg:
            for revision_identifier_match in REVISION_IDENTIFIER_IN_MSG_RE.finditer(message):
                if self._git_branch and revision_identifier_match.group('branch_name') != self._git_branch:
                    continue
                revision_identifier = revision_identifier_match.group('revision_identifier')

            if not revision_identifier:
                raise ValueError('Expected commit message to include revision identifier, but cannot find it, will need a history rewrite to fix it')

        current_revision = current_hash
        previous_revision = previous_hash
        if self._report_svn_revision:
            current_revision = int(self._svn_revision_from_git_hash(current_hash))
            if not current_revision:
                raise ValueError('Cannot find SVN revison for {}'.format(current_hash))
            if previous_hash:
                if current_revision <= self._max_revision + 1:
                    previous_revision = self._svn_revision_from_git_hash(previous_hash)
                if not previous_revision:
                    raise ValueError('Cannot find SVN revison for {}'.format(previous_hash))
            if current_revision > self._max_revision:
                current_revision = current_hash

        return {
            'repository': self._name,
            'revision': current_revision,
            'revisionIdentifier': revision_identifier,
            'previousCommit': previous_revision,
            'time': datetime.utcfromtimestamp(commit_time).strftime(r'%Y-%m-%dT%H:%M:%S.%f'),
            'author': {'account': author_email, 'name': author_name},
            'message': message,
        }

    def _find_next_hash(self, hash_to_find):
        for i, tokens in enumerate(self._tokenized_hashes):
            if tokens and tokens[0] == hash_to_find:
                return self._tokenized_hashes[i + 1] if i + 1 < len(self._tokenized_hashes) else None
        return None

    def _fetch_remote(self):
        if self._report_svn_revision:
            self._run_git_command(['fetch', 'origin', self._git_branch])
            self._run_git_command(['reset', '--hard', 'FETCH_HEAD'])
            subprocess.check_call(['rm', '-rf', os.path.join(self._git_checkout, '.git/svn')])
            self._run_git_command(['svn', 'fetch'])
        else:
            self._run_git_command(['pull', self._git_url])

    def _fetch_all_hashes(self):
        self._fetch_remote()
        scope = self._git_branch or '--all'
        lines = self._run_git_command(['log', scope, '--date-order', '--reverse', '--pretty=%H %ct <%ce> %P']).split('\n')
        self._tokenized_hashes = []
        GIT_LOG_LINE_RE = re.compile(r'(?P<H>\w+) (?P<ct>\d+) \<(?P<ce>.*)\>\s*(?P<P>\w+)?')
        for line in lines:
            if not line:
                continue
            match = GIT_LOG_LINE_RE.match(line)
            if not match:
                raise Exception('Failed to tokenize hash "{}"'.format(line))
            self._tokenized_hashes.append(filter(None, list(match.groups())))

    def _run_git_command(self, args):
        return subprocess.check_output(['git', '-C', self._git_checkout] + args, stderr=subprocess.STDOUT)

    def format_revision(self, revision):
        return str(revision)[0:8]


if __name__ == "__main__":
    main(sys.argv)
