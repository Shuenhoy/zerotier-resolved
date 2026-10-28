#! /usr/bin/env python

import argparse
import json
import logging
import signal
import subprocess
import threading
import urllib.error
import urllib.request


logger = logging.getLogger('zt_resolved')

RESOLVECTL = '/usr/bin/resolvectl'
DEFAULT_INTERVAL = 10.0

# device name -> (domain, dns servers)
NetworkDns = dict[str, tuple[str, tuple[str, ...]]]


def read_authtoken() -> str:
    with open('/var/lib/zerotier-one/authtoken.secret', 'rt') as f:
        return f.read()


def get_network_info(authtoken: str) -> list[dict]:
    with urllib.request.urlopen(urllib.request.Request(
        'http://localhost:19193/network',
        headers={
            'X-ZT1-Auth': authtoken,
        },
    ), timeout=10) as resp:
        if resp.status != 200:
            logger.error('Response status: %d %s', resp.status, resp.reason)
            raise RuntimeError('Response status not 200')
        resp_data = resp.read()
        resp_json = json.loads(resp_data)
        logger.debug('Network info response: %r', resp_json)
        return resp_json


def parse_desired_dns(
    network_info: list[dict],
    restrict_interfaces: list[str] | None = None,
) -> NetworkDns:
    """Return the DNS configuration wanted for each ZeroTier interface."""
    desired = {}
    for network in network_info:
        network_id = network.get('id')
        device = network.get('portDeviceName')
        if not device:
            logger.debug(
                'Network %s has no interface (not connected), skipping',
                network_id,
            )
            continue
        if restrict_interfaces is not None and device not in restrict_interfaces:
            logger.debug(
                'Network %s %s not in interface list, skipping',
                network_id,
                device,
            )
            continue
        dns = network.get('dns') or {}
        dns_servers = tuple(dns.get('servers') or ())
        if not dns_servers:
            logger.debug(
                'Network %s %s has no DNS servers configured, skipping',
                network_id,
                device,
            )
            continue
        dns_domain = dns.get('domain') or ''
        logger.info(
            'Network %s %s domain: %s, DNS servers: %r',
            network_id,
            device,
            dns_domain,
            dns_servers,
        )
        desired[device] = (dns_domain, dns_servers)
    return desired


def generate_apply_commands(
    device: str,
    dns_domain: str,
    dns_servers: tuple[str, ...],
) -> list[list[str]]:
    commands = []
    if dns_domain:
        commands.append([RESOLVECTL, 'domain', device, dns_domain])
    commands.extend([
        [RESOLVECTL, 'dns', device, *dns_servers],
        [RESOLVECTL, 'default-route', device, 'no'],
    ])
    return commands


def generate_resolvectl_commands(
    desired: NetworkDns,
) -> list[list[str]]:
    commands = []
    for device, (dns_domain, dns_servers) in desired.items():
        commands.extend(
            generate_apply_commands(device, dns_domain, dns_servers)
        )
    return commands


def run_commands(
    commands: list[list[str]],
    failure_level: int = logging.ERROR,
) -> bool:
    """Run commands, returning False if any of them failed."""
    success = True
    for command in commands:
        logger.debug('Executing command %r', command)
        try:
            p = subprocess.run(command)
        except OSError as e:
            logger.log(failure_level, 'Failed to execute %s: %s', command, e)
            success = False
            continue
        if p.returncode != 0:
            logger.log(
                failure_level,
                'Command %s returned non-zero: %d',
                command,
                p.returncode,
            )
            success = False
    return success


def revert_interface(device: str) -> bool:
    """Remove all per-link DNS settings previously applied to an interface."""
    logger.info('Reverting DNS settings for %s', device)
    # A failure here is not worth an error log: the interface may already be
    # gone, in which case resolved has dropped the settings by itself.
    return run_commands(
        [[RESOLVECTL, 'revert', device]],
        failure_level=logging.WARNING,
    )


def revert_all(applied: NetworkDns) -> None:
    for device in applied:
        revert_interface(device)
    applied.clear()


def apply_desired(desired: NetworkDns, applied: NetworkDns) -> None:
    """Apply desired state, reverting interfaces that are no longer wanted."""
    for device in list(applied):
        if device not in desired:
            logger.info(
                'Network interface %s no longer configured, '
                'reverting DNS settings', device,
            )
            revert_interface(device)
            del applied[device]

    for device, (dns_domain, dns_servers) in desired.items():
        if applied.get(device) == (dns_domain, dns_servers):
            continue
        logger.info(
            'Configuring %s: domain %r, DNS servers %r',
            device, dns_domain, dns_servers,
        )
        if run_commands(generate_apply_commands(
            device, dns_domain, dns_servers,
        )):
            applied[device] = (dns_domain, dns_servers)
        else:
            applied.pop(device, None)


def poll_once(
    restrict_interfaces: list[str] | None,
    applied: NetworkDns,
) -> None:
    authtoken = read_authtoken()
    network_info = get_network_info(authtoken)
    desired = parse_desired_dns(network_info, restrict_interfaces)
    apply_desired(desired, applied)


def run_daemon(
    restrict_interfaces: list[str] | None,
    interval: float,
) -> None:
    stop_event = threading.Event()

    def handle_signal(_signum: int, _frame) -> None:
        # Signal handler: only touch the event, as logging and friends are
        # not async-signal-safe and could deadlock.
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    applied: NetworkDns = {}
    logger.info('Starting in daemon mode, polling every %g seconds', interval)
    try:
        while not stop_event.is_set():
            try:
                poll_once(restrict_interfaces, applied)
            except (
                OSError, urllib.error.URLError, RuntimeError, ValueError,
            ) as e:
                # ZeroTier may not be ready yet, or may be restarting.
                # Keep the current config and retry on the next poll.
                logger.warning('Failed to update DNS settings: %s', e)
            stop_event.wait(interval)
    finally:
        logger.info('Stopping, reverting DNS settings')
        revert_all(applied)


def main():
    parser = argparse.ArgumentParser(
        description='Set ZeroTier per-network DNS servers for systemd-resolved',
    )

    parser.add_argument(
        '--interface',
        '-I',
        action='append',
        help="""Only configure DNS servers for specified interface(s).
        Repeat this argument for each interface."""
    )
    parser.add_argument(
        '--daemon',
        '-d',
        action='store_true',
        help="""Keep running and periodically check ZeroTier for network
        changes. DNS settings are reverted when the process is stopped."""
    )
    parser.add_argument(
        '--interval',
        type=float,
        default=DEFAULT_INTERVAL,
        metavar='SECONDS',
        help='Polling interval in daemon mode (default: %g).'
             % DEFAULT_INTERVAL,
    )
    parser.add_argument(
        '--verbose',
        '-v',
        action='count',
        help='Increase verbosity. Specify once for INFO, twice for DEBUG.'
    )

    args = parser.parse_args()

    if not args.verbose:
        loglevel = logging.WARNING
    elif args.verbose >= 2:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO
    logging.basicConfig(
        level=loglevel,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    logger.debug('Args: %r', args)

    if args.interval <= 0:
        parser.error('--interval must be greater than zero')

    if not args.daemon:
        authtoken = read_authtoken()
        network_info = get_network_info(authtoken)
        desired = parse_desired_dns(network_info, args.interface)
        run_commands(generate_resolvectl_commands(desired))
        return

    run_daemon(args.interface, args.interval)


if __name__ == '__main__':
    main()
