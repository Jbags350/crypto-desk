"""Container start-up (Linux only): fix folder ownership, then run the real
command as the ordinary 'desk' user instead of root.

Why: data/, logs/ and reports/ are folders on the server shared into the
container. They're often created by root, so a non-root bot couldn't write
its database. This makes them the desk user's first, every start, so it
always works, and the bot itself still never runs as root.
"""

import os
import pwd
import sys

USER = "desk"
FOLDERS = ["/app/data", "/app/logs", "/app/reports"]


def give_to(uid: int, gid: int, folder: str) -> None:
    os.makedirs(folder, exist_ok=True)
    for root, dirs, files in os.walk(folder):
        for path in [root] + [os.path.join(root, n) for n in dirs + files]:
            try:
                os.chown(path, uid, gid, follow_symlinks=False)
            except OSError as exc:
                print(f"entrypoint: couldn't change owner of {path}: {exc}", file=sys.stderr)


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit("entrypoint: no command given")
    if os.getuid() == 0:
        user = pwd.getpwnam(USER)
        for folder in FOLDERS:
            give_to(user.pw_uid, user.pw_gid, folder)
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
        os.environ.update(HOME=user.pw_dir, USER=USER, LOGNAME=USER)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
