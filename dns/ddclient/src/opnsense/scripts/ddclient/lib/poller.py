"""
    Copyright (c) 2023 Ad Schellevis <ad@opnsense.org>
    All rights reserved.

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:

    1. Redistributions of source code must retain the above copyright notice,
     this list of conditions and the following disclaimer.

    2. Redistributions in binary form must reproduce the above copyright
     notice, this list of conditions and the following disclaimer in the
     documentation and/or other materials provided with the distribution.

    THIS SOFTWARE IS PROVIDED ``AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES,
    INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
    AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
    AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY,
    OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
    SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
    INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
    CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
    ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
    POSSIBILITY OF SUCH DAMAGE.
"""
import fcntl
import syslog
import glob
import importlib
import sys
import os
import time
import ujson
import ipaddress
import uuid
from .account import BaseAccount


REQUEST_PATH = '/var/run/ddclient_opn.requests'


class AccountFactory:
    def __init__(self):
        self._account_classes = list()
        self._register()

    def _register(self):
        """ Register all account (type) classes.
            These usually describe a protocol (like dyndns2)
        """
        pkg_name = "%s.account" % __name__[:-len(os.path.splitext(os.path.basename(__file__))[0])-1]
        all_account_classes = list()
        for filename in glob.glob("%s/account/*.py" % os.path.dirname(__file__)):
            importlib.import_module(".%s" % os.path.splitext(os.path.basename(filename))[0], pkg_name)

        for module_name in dir(sys.modules[pkg_name]):
            for attribute_name in dir(getattr(sys.modules[pkg_name], module_name)):
                cls = getattr(getattr(sys.modules[pkg_name], module_name), attribute_name)
                if isinstance(cls, type) and issubclass(cls, BaseAccount) and cls != BaseAccount:
                    all_account_classes.append(cls)

        self._account_classes = sorted(all_account_classes, key=lambda k: k._priority)

    def get(self, account: dict):
        for handler in self._account_classes:
            if handler.match(account):
                return handler(account)

    def known_services(self):
        all_services = {}
        for handler in self._account_classes:
            data = handler.known_services()
            if type(data) is dict:
                all_services.update(data)
            else:
                for item in data:
                    all_services[item] = item
        return all_services


class RequestQueue:
    """ Simple file based queue to pass force refresh requests to the running poller.
        A client drops a request (<id>.req) containing the account ids to refresh, the poller claims it,
        executes the requested accounts and writes the outcome to <id>.res, which the client collects.
        All files are written to a temporary name first and renamed into place, so readers never see
        partial content.
    """
    # requests older than this are not executed anymore, nobody is waiting for the result
    request_ttl = 120
    # leftovers (uncollected results, files of an interrupted client or poller) are removed after this
    cleanup_ttl = 3600

    def __init__(self, path=REQUEST_PATH):
        self._path = path

    def _filename(self, request_id, ext):
        return "%s/%s.%s" % (self._path, request_id, ext)

    def _write(self, filename, data):
        tmp_filename = "%s.tmp" % filename
        with open(tmp_filename, 'w') as f:
            f.write(ujson.dumps(data))
        os.rename(tmp_filename, filename)

    def submit(self, account_ids):
        """ queue a request, return its id
        """
        os.makedirs(self._path, mode=0o700, exist_ok=True)
        # a .run file only exists between claiming and reading a request, never while it executes
        for filename in glob.glob("%s/*" % self._path):
            try:
                if time.time() - os.path.getmtime(filename) > self.cleanup_ttl:
                    os.remove(filename)
            except FileNotFoundError:
                pass
        request_id = uuid.uuid4().hex
        self._write(self._filename(request_id, 'req'), list(account_ids))
        return request_id

    def cancel(self, request_id):
        """ withdraw a request, return false when the poller has already claimed it
        """
        try:
            os.remove(self._filename(request_id, 'req'))
            return True
        except FileNotFoundError:
            return False

    def wait(self, request_id, timeout):
        """ wait for the result of a request, return None on timeout
        """
        filename = self._filename(request_id, 'res')
        until = time.time() + timeout
        while time.time() < until:
            if os.path.isfile(filename):
                with open(filename) as f:
                    result = ujson.load(f)
                os.remove(filename)
                return result
            time.sleep(0.5)
        return None

    def pending(self):
        """ claim and yield all queued requests as (request_id, [account_ids])
        """
        for filename in glob.glob("%s/*.req" % self._path):
            request_id = os.path.basename(filename)[:-4]
            claimed_filename = self._filename(request_id, 'run')
            try:
                os.rename(filename, claimed_filename)
            except FileNotFoundError:
                # cancelled by the client
                continue
            try:
                with open(claimed_filename) as f:
                    account_ids = ujson.load(f)
            except ValueError:
                account_ids = None
            expired = time.time() - os.path.getmtime(claimed_filename) > self.request_ttl
            os.remove(claimed_filename)
            if expired:
                syslog.syslog(syslog.LOG_NOTICE, "force refresh request %s expired, ignored" % request_id)
            elif type(account_ids) is list and all(type(x) is str for x in account_ids):
                yield request_id, account_ids

    def respond(self, request_id, result):
        self._write(self._filename(request_id, 'res'), result)


class Poller:
    def __init__(self, config_filename, status_filename, request_path=REQUEST_PATH):
        self._config_filename = config_filename
        self._status_filename = status_filename
        self._requests = RequestQueue(request_path)
        self._accounts = {}
        self._general_settings = {}
        syslog.openlog('ddclient', facility=syslog.LOG_LOCAL4)
        self.startup()
        self.run()

    @property
    def is_verbose(self):
        return self._general_settings.get('verbose') is True

    @property
    def is_enabled(self):
        return self._general_settings.get('enabled') is True

    @property
    def poll_interval(self):
        return self._general_settings.get('daemon_delay', 60)

    def startup(self):
        account_factory = AccountFactory()
        with open(self._config_filename) as f:
            cnf = ujson.load(f)
            if type(cnf.get('general')) is dict:
                self._general_settings = cnf.get('general')
            if type(cnf.get('accounts')) is list:
                for account in cnf.get('accounts'):
                    account['verbose'] = self.is_verbose
                    acc = account_factory.get(account)
                    if acc:
                        self._accounts[acc.id] = acc
                        if self.is_verbose:
                            syslog.syslog(
                                syslog.LOG_NOTICE,
                                "Account %s uses %s for service" % (acc.description, acc.__class__.__name__)
                            )
                    elif self.is_verbose:
                        syslog.syslog(
                            syslog.LOG_NOTICE,
                            "Unable to find a suitable target for account %(id)s [%(description)s]" % account
                        )
        if len(self._accounts) > 0 and os.path.isfile(self._status_filename):
            with open(self._status_filename) as f:
                try:
                    state = ujson.load(f)
                    if type(state) is dict:
                        for sid in state:
                            if sid in self._accounts:
                                self._accounts[sid].state = state[sid]
                except ValueError:
                    syslog.syslog(syslog.LOG_ERR, "Unable to read file %s" % self._status_filename)

    def flush_status(self):
        fhandle = open(self._status_filename, 'a+')
        try:
            fcntl.flock(fhandle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            syslog.syslog(syslog.LOG_ERR, "Unable to flush status, %s already locked" % self._status_filename)
            return
        fhandle.seek(0)
        fhandle.truncate()
        data = {}
        for acc_id in self._accounts:
            data[acc_id] = self._accounts[acc_id].state
        fhandle.write(ujson.dumps(data))
        fhandle.close()

    def execute_account(self, acc, force=False):
        """ execute account check/update sequence
            :param acc: account object
            :param force: push the detected address to the service, even when it equals the cached one
            :return: True when updated, False when not modified or failed, None on fatal error
        """
        if self.is_verbose:
            syslog.syslog(syslog.LOG_NOTICE, "Account %s executing" % acc.description)
        acc.force = force
        try:
            if acc.execute():
                if self.is_verbose:
                    syslog.syslog(syslog.LOG_NOTICE, "Account %s updated" % acc.description)
                return True
            else:
                if self.is_verbose:
                    syslog.syslog(syslog.LOG_NOTICE, "Account %s not modified" % acc.description)
                # update last accessed timestamp
                acc.update_state(None)
                return False
        except Exception as e:
            # fatal exception, update atime so we're not going to retry too soon
            acc.update_state(None)
            syslog.syslog(syslog.LOG_ERR, "Account %s raised fatal error (%s)" % (acc.description, e))
            return None
        finally:
            acc.force = False

    def process_requests(self):
        """ handle queued force refresh requests, status is flushed before responding so the
            result is persisted by the time the client receives it.
        """
        for request_id, account_ids in self._requests.pending():
            result = {}
            needs_flush = False
            for acc_id in dict.fromkeys(account_ids):
                acc = self._accounts.get(acc_id)
                if acc is None:
                    result[acc_id] = {'status': 'failed', 'reason': 'unknown_account'}
                    continue
                syslog.syslog(syslog.LOG_NOTICE, "Account %s force refresh requested" % acc.description)
                updated = self.execute_account(acc, force=True)
                if updated:
                    needs_flush = True
                    syslog.syslog(
                        syslog.LOG_NOTICE,
                        "Account %s force refresh succeeded (%s)" % (acc.description, acc.state.get('ip'))
                    )
                    result[acc_id] = {'status': 'ok', 'ip': acc.state.get('ip')}
                elif updated is None:
                    # unexpected error, already logged by execute_account()
                    result[acc_id] = {'status': 'error'}
                else:
                    reason = 'update_failed' if acc.current_address else 'no_address'
                    syslog.syslog(
                        syslog.LOG_ERR, "Account %s force refresh failed (%s)" % (acc.description, reason)
                    )
                    result[acc_id] = {'status': 'failed', 'reason': reason}
            if needs_flush:
                self.flush_status()
            self._requests.respond(request_id, result)

    def run(self):
        while True:
            try:
                self.process_requests()
            except OSError as e:
                syslog.syslog(syslog.LOG_ERR, "Unable to process force refresh requests (%s)" % e)
            needs_flush = False
            for acc in self._accounts.values():
                if time.time() - acc.atime > self.poll_interval:
                    if self.execute_account(acc):
                        needs_flush = True

            if needs_flush:
                if self.is_verbose:
                    syslog.syslog(syslog.LOG_NOTICE, "Flush dyndns status to disk")
                self.flush_status()

            # XXX: needs better poll interval calculation
            time.sleep(5)
