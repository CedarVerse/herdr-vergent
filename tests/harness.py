"""Shared fake-server test environment.

Four case files (test_socket, test_snapshot, test_create, test_reconcile)
need the same arrangement: a loaded seeder module, a FakeHerdr on a
throwaway socket path, and a client pointed at it. make_env() wires all
three and registers the LIFO cleanups (client.close, fake.stop, rmtree):
fake.stop() closes the listener so the accept thread exits deterministically,
and the directory removal runs last, after the socket file is gone with its
directory. client.close stays registered for API symmetry even though
HerdrSocket.close is a documented no-op -- per-call sockets already close in
call()'s finally, so there is nothing left for it to release.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_herdr
from test_loader import load_module


def make_env(testcase, timeout=None, fake=None, module=None):
    """Return (module, fake, client) for one test; cleanups self-register.

    `timeout` is forwarded to HerdrSocket only when given (None must NOT
    reach the constructor: the deadline arithmetic needs a real number).
    `fake` accepts a FakeHerdr subclass instance (a test pinning altered
    server semantics); default is the plain base fake.
    `module` pins the seeder module instead of loading a fresh one: module
    -level state (HerdrError, FACT_A_CONFIRMED) is PER-LOAD, and
    assertRaises matches exception classes by identity -- a test that flips
    a flag on its own load must have the client built from that same load,
    or it would assert against one module while exercising another.
    """
    module = module if module is not None else load_module()
    tmp = tempfile.mkdtemp()
    # rmtree, not rmdir: the fake's socket file lives in here, so the dir
    # is never empty at teardown. Cleanups run LIFO: client.close() and
    # fake.stop() run before the tree is removed.
    testcase.addCleanup(shutil.rmtree, tmp)
    fake = fake if fake is not None else fake_herdr.FakeHerdr()
    fake.tmpdir = tmp
    kwargs = {"timeout": timeout} if timeout is not None else {}
    client = module.HerdrSocket(path=fake.start(), **kwargs)
    testcase.addCleanup(client.close)  # no-op today; kept for API symmetry
    testcase.addCleanup(fake.stop)
    return module, fake, client
