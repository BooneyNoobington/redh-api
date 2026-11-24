"""Shutting the server down: whether it can, and doing it once the answer is out."""

from __future__ import annotations

import grp
import logging
import os
import pwd
import stat
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

log = logging.getLogger("redh_api")

SUDO = ("sudo", "-n")  # -n: fail rather than ask for a password
# sudo logs every 'sudo -l', so its answer is kept: a yes for 10 minutes, a no, which
# usually means the rule is about to be added, for half a minute
SUDO_RECHECK = 600
SUDO_RECHECK_NO = 30


def current_user() -> str:
    uid = os.geteuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def last_line(text: str, limit: int = 200) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:limit] if lines else ""


def unsafe_path(path: str, owners: Sequence[int] = (0,)) -> Optional[str]:
    """Why someone but root could change what sudo is to run as root, or None.

    The file and every directory above it, before and after resolving symbolic
    links, must belong to root and be writable by nobody else.
    """
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return f"{path} does not exist"
    except OSError as e:
        return f"{path}: {e.strerror}"
    if not stat.S_ISREG(info.st_mode):
        return f"{path} is not a file"
    if not info.st_mode & stat.S_IXUSR:
        return f"{path} is not executable"
    for start in (os.path.abspath(path), os.path.realpath(path)):
        current = start
        while True:
            problem = _writable_by_others(current, owners)
            if problem:
                return problem
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return None


def sudo_refusal(sudo: Sequence[str], user: str, argv: Sequence[str]) -> Optional[str]:
    """Ask sudo whether the user may run argv as root without a password: None if so, else why not."""
    asked = list(sudo)
    if user != current_user():
        asked += ["-U", user]
    asked += ["-l", *argv]
    try:
        result = subprocess.run(asked, stdin=subprocess.DEVNULL, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=10)
    except FileNotFoundError:
        return "sudo is not installed"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"could not ask sudo: {e}"
    if result.returncode == 0:
        return None
    reason = last_line(result.stderr)
    return (f"sudo does not let {user} run {' '.join(argv)} without a password"
            + (f" ({reason})" if reason else ""))


def _writable_by_others(path: str, owners: Sequence[int]) -> Optional[str]:
    try:
        info = os.stat(path)
    except OSError as e:
        return f"{path}: {e.strerror}"
    if info.st_uid not in owners:
        return f"{path} belongs to {_user_name(info.st_uid)}, not root"
    if info.st_mode & stat.S_IWOTH:
        return f"{path} is writable by everyone"
    if info.st_mode & stat.S_IWGRP and info.st_gid != 0:
        return f"{path} is writable by the group {_group_name(info.st_gid)}"
    return None


def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return f"gid {gid}"


class Shutdown:
    """Shuts the server down by running the command as root, through sudo.

    A request is accepted first and carried out later: the caller sends its
    answer, then calls start(), and the command runs after the delay. That way
    the answer is out before the server goes down.
    """

    def __init__(self, command: str, delay: float, *, user: Optional[str] = None,
                 sudo: Sequence[str] = SUDO, owners: Sequence[int] = (0,),
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.command = command
        self.delay = delay
        self.user = user or current_user()
        self.sudo = list(sudo)
        self.owners = tuple(owners)
        self.clock = clock
        self._lock = threading.Lock()
        self._pending = False
        self._started = False
        self._last_error: Optional[str] = None
        self._sudo_answer: Optional[Tuple[float, Optional[str]]] = None

    def problem(self, fresh: bool = False) -> Optional[str]:
        """Why a shutdown can't run now, or None if it can.

        The file is checked every time, sudo now and then (see SUDO_RECHECK),
        or every time when fresh is set.
        """
        unsafe = unsafe_path(self.command, self.owners)
        if unsafe:
            return unsafe
        now = self.clock()
        answer = self._sudo_answer
        if (fresh or answer is None
                or now - answer[0] >= (SUDO_RECHECK if answer[1] is None else SUDO_RECHECK_NO)):
            answer = (now, self.sudo_problem())
            self._sudo_answer = answer
        return answer[1]

    def sudo_problem(self) -> Optional[str]:
        """Ask sudo whether the user may run the command without a password."""
        return sudo_refusal(self.sudo, self.user, [self.command])

    def status(self) -> Dict[str, Any]:
        problem = self.problem()
        with self._lock:
            return {"ready": problem is None, "problem": problem,
                    "pending": self._pending, "last_error": self._last_error}

    def accept(self) -> Optional[str]:
        """Accept a request: None if accepted, or already pending; otherwise why not."""
        with self._lock:
            if self._pending:
                return None
            problem = self.problem(fresh=True)
            if problem is None:
                self._pending, self._started, self._last_error = True, False, None
            return problem

    def start(self) -> None:
        """Carry out the accepted request: the command runs after the delay."""
        with self._lock:
            if not self._pending or self._started:
                return
            self._started = True
        threading.Thread(target=self._run, name="shutdown", daemon=True).start()

    def _run(self) -> None:
        time.sleep(self.delay)
        argv = self.sudo + [self.command]
        log.warning("shutting down: %s", " ".join(argv))
        error: Optional[str] = None
        try:
            # stdout goes to the journal as it comes; stderr also into last_error
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    encoding="utf-8", errors="replace")
        except OSError as e:
            error = f"could not run sudo: {e.strerror}"
        else:
            for line in result.stderr.splitlines():
                log.warning("%s: %s", os.path.basename(self.command), line)
            if result.returncode != 0:
                reason = last_line(result.stderr)
                error = (f"{self.command} failed with exit status {result.returncode}"
                         + (f": {reason}" if reason else ""))
        if error:
            log.error("shutdown failed: %s", error)
        else:
            log.warning("%s is done; the server should be going down", self.command)
        with self._lock:
            self._pending = self._started = False
            self._last_error = error
