#!/usr/bin/env python3
"""Independent lifecycle, shared-daemon and cooperation setup choices."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import install_client_hooks as hooks
from src import env_file
from src.daemon import registrations
from src.daemon.bootstrap import assert_service_safe
from src.daemon.control import Controller
from src.daemon.state import read_json, state_dir
from src.file_lock import file_lock


def controller_for_installation():
    marker = read_json(ROOT / 'data/.shared-service.json')
    if marker is not None:
        if not isinstance(marker, dict) or not isinstance(marker.get('directory'), str):
            raise ValueError('Invalid shared-service marker')
        directory = Path(marker['directory'])
        if not directory.is_absolute():
            raise ValueError('Shared-service directory must be absolute')
    else:
        directory = state_dir(ROOT)
    controller = Controller(directory)
    if not isinstance(controller.config, dict):
        raise ValueError('Invalid service configuration')
    if marker is not None and not controller.config:
        raise ValueError('Shared-service marker has no installed service')
    if controller.config:
        installation = controller.config.get('installation')
        if not isinstance(installation, str) or not Path(installation).is_absolute():
            raise ValueError('Invalid service installation identity')
        if Path(installation).resolve() != ROOT.resolve():
            raise ValueError('Service belongs to another installation')
    return controller


def enable_cooperation(controller):
    """Change only the accepted setting, under service admission and an env writer lock."""
    target = ROOT / '.env'
    with file_lock(controller.directory / 'control.lock', blocking=False):
        assert_service_safe(controller.directory)
        with file_lock(ROOT / 'data/.env.lock', blocking=False):
            original = env_file.read_text(target) if target.exists() else ''
            updated = env_file.rewrite_text(original, {'AGENTS_COOPERATION_ENABLED': '1'})
            if updated == original:
                return False
            env_file.write_atomic(target, updated)
    return True


def refresh_registrations(controller, *, force_claude_code=False):
    # Startup upkeep may briefly own control.lock. Retry only an explicit busy result.
    for attempt in range(3):
        try:
            report = registrations.run(controller.directory, force=False,
                                       force_claude_code=force_claude_code)
            break
        except BlockingIOError:
            if attempt == 2:
                raise
            time.sleep(.2)
    if report.get('auto') is False:
        print('Managed registration upkeep is disabled; existing registrations preserved.')
        return False
    failed = False
    for client, result in report.get('clients', {}).items():
        status = result.get('status')
        print(f'Shared daemon registration: {client} {status}.')
        failed |= status == 'failed'
    if failed:
        raise RuntimeError('Some managed client registrations could not be migrated')
    return True


def install_daemon(controller):
    # Select the daemon's model settings without exporting unrelated .env
    # bindings (notably sync credentials) to service-management children.
    settings = dotenv_values(ROOT / '.env')
    model = os.environ.get('EMBEDDING_MODEL', settings.get('EMBEDDING_MODEL')) or None
    cache = settings.get('FASTEMBED_CACHE_DIR')
    temporary_cache = 'FASTEMBED_CACHE_DIR' not in os.environ and bool(cache)
    if temporary_cache:
        os.environ['FASTEMBED_CACHE_DIR'] = cache
    try:
        controller.install(python=sys.executable, model=model)
    finally:
        if temporary_cache:
            os.environ.pop('FASTEMBED_CACHE_DIR', None)


def setup_shared(args):
    if args.skip_daemon and args.skip_cooperation:
        return
    if sys.platform not in ('darwin', 'win32'):
        print('Shared daemon and cooperation setup skipped: OS service installation is supported on macOS and Windows.')
        return
    selected = hooks.choose('shared daemon and managed client migration',
                            assume_yes=args.yes, skip=args.skip_daemon)
    controller = controller_for_installation()
    if not selected and not controller.config:
        print('Cooperation skipped: it requires an installed shared daemon.')
        return
    cooperation = hooks.choose('cross-client agent cooperation',
                               assume_yes=args.yes, skip=args.skip_cooperation)
    if not selected and not cooperation:
        return
    if not controller.config:
        install_daemon(controller)
    with file_lock(controller.directory / 'control.lock', blocking=False):
        assert_service_safe(controller.directory)
        status = controller.status()
    if status.get('state') == 'ready' and status.get('install_root') != str(ROOT.resolve()):
        raise RuntimeError('Service endpoint belongs to another installation')
    changed = enable_cooperation(controller) if cooperation else False
    if status.get('state') != 'ready':
        status = controller.start()
    elif cooperation and (changed or status.get('cooperation') != 'ready'):
        status = controller.restart()
    if status.get('state') != 'ready' or status.get('install_root') != str(ROOT.resolve()):
        raise RuntimeError('Shared daemon readiness could not be verified')
    # ClientMigration's managed-entry discovery preserves absent, custom, pinned
    # and foreign registrations; it never creates unrelated client profiles.
    print('Shared daemon ready.')
    cooperation_ready = status.get('cooperation') == 'ready'
    if cooperation:
        if cooperation_ready:
            print('Cross-client agent cooperation ready.')
        else:
            print('WARNING: cooperation is configured but its coordinator is unavailable; inspect service.log.',
                  file=sys.stderr)
    if selected and refresh_registrations(controller, force_claude_code=args.force_claude_code_mcp):
        print('Reconnect clients to use the shared runtime.')
    if cooperation and not cooperation_ready:
        raise RuntimeError('Cooperation is not ready')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yes', '-y', action='store_true', help='accept all supported setup options')
    for name in ('hooks', 'readiness', 'thread-status', 'daemon', 'cooperation'):
        parser.add_argument('--skip-' + name, action='store_true')
    parser.add_argument('--force-claude-code-mcp', action='store_true')
    args = parser.parse_args(argv)
    failed = False
    if not args.skip_hooks:
        options = ['--yes'] if args.yes else []
        if args.skip_readiness:
            options.append('--skip-readiness')
        if args.skip_thread_status:
            options.append('--skip-thread-status')
        failed = bool(hooks.main(options))
    try:
        setup_shared(args)
    except BlockingIOError:
        print('ERROR: a service/setup lock is busy. Retry after the other operation ends. '
              'For first daemon installation, close standalone Agents-Core sessions, then retry; '
              'or use --skip-daemon --skip-cooperation to keep standalone setup.', file=sys.stderr)
        failed = True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'ERROR: shared daemon/cooperation setup failed ({type(error).__name__}). '
              'Earlier completed steps are retained. Check the cached model, service.log and '
              'service status, then rerun setup; use --skip-daemon --skip-cooperation to skip these options.',
              file=sys.stderr)
        failed = True
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
