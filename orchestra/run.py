from cStringIO import StringIO

import gevent
import pipes
import logging
import shutil

log = logging.getLogger(__name__)

class RemoteProcess(object):
    __slots__ = ['command', 'stdin', 'stdout', 'stderr', '_get_exitstatus']
    def __init__(self, command, stdin, stdout, stderr, get_exitstatus):
        self.command = command
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._get_exitstatus = get_exitstatus

    @property
    def exitstatus(self):
        """
        Wait for exit and return exit status.

        Will return None on signals and connection loss.

        This will likely block until you've closed stdin and consumed
        stdout and stderr.
        """
        status = self._get_exitstatus()
        # -1 on connection loss *and* signals; map to more pythonic None
        if status == -1:
            status = None
        return status

def execute(client, args):
    """
    Execute a command remotely.

    Caller needs to handle stdin etc.

    :param client: SSHConnection to run the command with
    :param args: command to run
    :type args: list of string
    """
    cmd = ' '.join(pipes.quote(a) for a in args)
    (in_, out, err) = client.exec_command(cmd)
    r = RemoteProcess(
        command=cmd,
        stdin=in_,
        stdout=out,
        stderr=err,
        get_exitstatus=out.channel.recv_exit_status,
        )
    return r

def copy_to_log(f, logger, loglevel=logging.INFO):
    # i can't seem to get fudge to fake an iterable, so using this old
    # api for now
    for line in f.xreadlines():
        line = line.rstrip()
        logger.log(loglevel, line)

def copy_and_close(src, fdst):
    if src is not None:
        if isinstance(src, basestring):
            src = StringIO(src)
        shutil.copyfileobj(src, fdst)
    fdst.close()

def copy_file_to(f, dst):
    if hasattr(dst, 'log'):
        # looks like a Logger to me; not using isinstance to make life
        # easier for unit tests
        handler = copy_to_log
    else:
        handler = shutil.copyfileobj
    return handler(f, dst)


class CommandFailedError(Exception):
    def __init__(self, command, exitstatus):
        self.command = command
        self.exitstatus = exitstatus

    def __str__(self):
        return "Command failed with status {status}: {command!r}".format(
            status=self.exitstatus,
            command=self.command,
            )


class CommandCrashedError(Exception):
    def __init__(self, command):
        self.command = command

    def __str__(self):
        return "Command crashed: {command!r}".format(
            command=self.command,
            )


class ConnectionLostError(Exception):
    def __init__(self, command):
        self.command = command

    def __str__(self):
        return "SSH connection was lost: {command!r}".format(
            command=self.command,
            )

class CommandResult(object):
    __slots__ = ['command', 'stdout', 'stderr', 'exitstatus']
    def __init__(self, command, stdout=None, stderr=None, exitstatus=None):
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.exitstatus = exitstatus

def run(
    client, args,
    stdin=None, stdout=None, stderr=None,
    logger=None,
    check_status=True,
    ):
    """
    Run a command remotely.

    :param client: SSHConnection to run the command with
    :param args: command to run
    :type args: list of string
    :param stdin: Standard input to send; either a string, a file-like object, or None.
    :param stdout: What to do with standard output. Either a file-like object, a `logging.Logger`, or `None` for copying to default log.
    :param stderr: What to do with standard error. See `stdout`.
    :param logger: If logging, write stdout/stderr to "out" and "err" children of this logger. Defaults to logger named after this module.
    :param check_status: Whether to raise CalledProcessError on non-zero exit status, and . Defaults to True. All signals and connection loss are made to look like SIGHUP.
    """
    r = execute(client, args)

    g_in = gevent.spawn(copy_and_close, stdin, r.stdin)

    if logger is None:
        logger = log

    if stderr is None:
        stderr = logger.getChild('err')
    g_err = gevent.spawn(copy_file_to, r.stderr, stderr)

    if stdout is None:
        stdout = logger.getChild('out')
    copy_file_to(r.stdout, stdout)

    g_err.get()
    g_in.get()

    status = r.exitstatus
    if check_status:
        if status is None:
            # command either died due to a signal, or the connection
            # was lost
            transport = client.get_transport()
            if not transport.is_active():
                # look like we lost the connection
                raise ConnectionLostError(command=r.command)

            # connection seems healthy still, assuming it was a
            # signal; sadly SSH does not tell us which signal
            raise CommandCrashedError(command=r.command)
        if status != 0:
            raise CommandFailedError(command=r.command, exitstatus=status)

    return CommandResult(
        command=r.command,
        stdout=stdout,
        stderr=stderr,
        exitstatus=status,
        )
