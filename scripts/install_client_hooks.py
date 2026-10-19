#!/usr/bin/env python3
"""Offer the supported lifecycle hooks during normal client setup."""
import argparse
from importlib import metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts/_helpers'))

from install_codex_instructions import detect_codex_home
from src.client_paths import client_config_path, client_home
from scripts import install_session_readiness as readiness
from scripts import install_thread_status as thread_status


def detected_clients() -> tuple[str, ...]:
    """Match setup's profile signals without creating absent client profiles."""
    clients = []
    desktop = client_config_path('desktop')
    if os.environ.get('AGENTS_CLAUDE_DESKTOP_CONFIG') or desktop.parent.is_dir():
        clients.append('desktop')
    if (os.environ.get('CLAUDE_CONFIG_DIR') or client_home('claude').is_dir()
            or client_config_path('claude').is_file() or shutil.which('claude') or 'desktop' in clients):
        clients.append('claude')
    if detect_codex_home() is not None:
        clients.append('codex')
    for client, override, executable in (
        ('cursor', 'AGENTS_CURSOR_MCP_CONFIG', None),
        ('antigravity', 'AGENTS_ANTIGRAVITY_MCP_CONFIG', 'agy'),
    ):
        native_home = Path.home() / ('.cursor' if client == 'cursor' else '.gemini/config')
        if os.environ.get(override) and client_config_path(client).parent != native_home:
            print(f'{client} lifecycle hooks skipped: the alternate MCP file does not select a supported native hook profile.')
            continue
        if (os.environ.get(override) or client_config_path(client).parent.is_dir()
                or (executable and shutil.which(executable))):
            clients.append(client)
    return tuple(clients)


def ensure_claude_title_sdk() -> None:
    """Use the existing pinned extra in the interpreter stored in hook commands."""
    requirements = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['optional-dependencies']['thread-status']
    for requirement in requirements:
        package, separator, version = requirement.partition('==')
        if not separator:
            raise ValueError('The thread-status extra must pin its dependencies')
        try:
            installed = metadata.version(package) == version
        except metadata.PackageNotFoundError:
            installed = False
        if not installed:
            print('Installing the Claude title metadata dependency in the hook interpreter...')
            subprocess.run([sys.executable, '-m', 'pip', 'install', requirement], check=True, timeout=120)


def choose(label: str, *, assume_yes: bool, skip: bool) -> bool:
    if skip:
        print(f'{label}: skipped by option.')
        return False
    if assume_yes:
        return True
    try:
        answer = input(f'Install {label}? [Y/n]: ').strip().lower()
    except EOFError:
        print(f'{label}: no answer; skipped. Use --yes to accept all hook options.')
        return False
    return answer not in ('n', 'no')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yes', '-y', action='store_true', help='accept both hook options')
    parser.add_argument('--skip-readiness', action='store_true')
    parser.add_argument('--skip-thread-status', action='store_true')
    args = parser.parse_args(argv)
    clients = detected_clients()
    if not clients:
        print('No supported clients detected; lifecycle hooks skipped.')
        return 0
    failed = False
    codex_changed = False
    readiness_clients = set()
    for label, skipped, supported, installer in (
        ('session readiness and persona restore hooks', args.skip_readiness, readiness.CLIENTS, readiness.install),
        ('emoji thread-status hooks', args.skip_thread_status, thread_status.CLIENTS, thread_status.install),
    ):
        selected = [client for client in clients if client in supported]
        if not selected or not choose(label, assume_yes=args.yes, skip=skipped):
            continue
        if installer is thread_status.install and 'claude' in selected:
            try:
                ensure_claude_title_sdk()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f'ERROR: Claude title dependency setup failed ({type(error).__name__}). '
                      'Earlier setup steps may already be configured. Restore package access and rerun, '
                      'or use --skip-thread-status for an offline setup.', file=sys.stderr)
                failed = True
                # Other clients do not depend on this optional SDK.
                selected.remove('claude')
        for client in selected:
            try:
                result = installer(client)
                state = 'configured' if result['changed'] else 'already current'
                print(f'{label}: {client} {state}: {result["config"]}')
                codex_changed |= client == 'codex'
                if installer is readiness.install:
                    readiness_clients.add(client)
            except (OSError, ValueError, TypeError) as error:
                print(f'ERROR: {label} for {client} failed ({type(error).__name__}); inspect its hook configuration.', file=sys.stderr)
                failed = True
    if codex_changed:
        print('Codex: start a fresh session and review/trust the new definitions in /hooks. '
              'Installation does not grant hook trust. Interrupt title persistence also requires the codex CLI on PATH.')
    if 'desktop' in readiness_clients:
        print('Claude Desktop Chat: restart and enable Agents-Readiness if prompted. '
              'Its banner is a model-controlled first-response fallback.')
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
