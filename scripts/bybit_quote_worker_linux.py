"""Linux read-only parser entrypoint; no DPAPI or raw secret environment values."""
import argparse
import asyncio
import json
import os
import stat


class CredentialFileError(ValueError):
    pass


def load_credentials(directory):
    """Read owner-only regular files through a checked directory descriptor."""
    try:
        if not os.path.isabs(directory) or os.name != 'posix':
            raise ValueError
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        directory_fd = os.open(directory, flags | os.O_DIRECTORY)
        try:
            directory_stat = os.fstat(directory_fd)
            if directory_stat.st_uid != os.geteuid() or directory_stat.st_mode & 0o077:
                raise ValueError
            result = {}
            for field in ('api_key', 'api_secret'):
                fd = os.open(field, flags | os.O_NONBLOCK, dir_fd=directory_fd)
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                            or info.st_mode & 0o077 or not 1 <= info.st_size <= 4096):
                        raise ValueError
                    raw = os.read(fd, 4097)
                    value = raw.decode('utf-8').strip()
                    if not 1 <= len(value) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
                        raise ValueError
                    result[field] = value
                finally:
                    os.close(fd)
            return result
        finally:
            os.close(directory_fd)
    except (OSError, ValueError, UnicodeError):
        # Do not disclose paths, file contents or exception text in logs.
        raise CredentialFileError('bybit_linux_credentials_invalid') from None


def main():
    parser = argparse.ArgumentParser(description='Read-only Bybit worker with Linux file secrets')
    parser.add_argument('--credentials-dir', default='/run/ozark-bybit')
    args = parser.parse_args()
    try:
        pair = load_credentials(args.credentials_dir)
        from scripts.bybit_quote_worker import run
        asyncio.run(run(activate=False, credentials=pair))
    except Exception as exc:
        print(json.dumps({'status': 'BLOCKED', 'error': type(exc).__name__}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
