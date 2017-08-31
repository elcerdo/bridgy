#!/usr/bin/env python
"""bridgy
SSH + TMUX + SSHFS + CLOUD INVENTORY SEARCH.
Fuzzy search for one or more systems then ssh into all matches, organized
by tmux.

Usage:
  bridgy ssh [-adsuvw] [-l LAYOUT] <host>...
  bridgy ssh [-dsuv] --no-tmux <host>
  bridgy list-inventory
  bridgy list-mounts
  bridgy mount [-duv] <host>:<remotedir>
  bridgy unmount [-dv] (-a | <host>...)
  bridgy run <task>
  bridgy update [-v]
  bridgy (-h | --help)
  bridgy --version

Sub-commands:
  ssh           ssh into the selected host(s)
  mount         use sshfs to mount a remote directory to an empty local directory
  unmount       unmount one or more host sshfs mounts
  list-mounts   show all sshfs mounts
  run           execute the given ansible task defined as playbook yml in ~/.bridgy/config.yml
  update        pull the latest inventory from your cloud provider

Options:
  -a        --all            Automatically use all matched hosts.
  -d        --dry-run        Show all commands that you would have run, but don't run them (implies --verbose).
  -l LAYOUT --layout LAYOUT  Use a configured lmux layout for each host.
  -n        --no-tmux        Ssh into a single server without tmux.
  -s        --sync-panes     Synchronize input on all visible panes (tmux :setw synchronize-panes on).
  -u        --update         pull the latest instance inventory from aws then run the specified command.
  -w        --windows        Use tmux windows instead of panes for each matched host.
  -h        --help           Show this screen.
  -v        --verbose        Show debug information.
  --version                  Show version.

Configuration Options are in ~/.bridgy/config.yml
"""
import sys
import os
import logging
import inquirer
import coloredlogs
import collections
from tabulate import tabulate
from docopt import docopt

from bridgy.version import __version__
from bridgy.command import Ssh, Sshfs, RunAnsibleTask
import bridgy.inventory as inventory
import bridgy.config as cfg
import bridgy.tmux as tmux
import bridgy.utils as utils

logger = logging.getLogger()


def prompt_targets(question, targets=None, instances=None, multiple=True, config=None):
    if targets == None and instances == None or targets != None and instances != None:
        raise RuntimeError("Provide exactly one of either 'targets' or 'instances'")

    if targets:
        instances = inventory.search(config, targets)

    if len(instances) == 0:
        return []

    if len(instances) == 1:
        return instances

    display_instances = collections.OrderedDict()
    for instance in sorted(instances):
        display = "%-55s (%s)" % (instance.name, instance.address)
        display_instances[display] = instance

    questions = []

    if multiple:
        q = inquirer.Checkbox('instance',
                              message="%s%s%s (space to multi-select, enter to finish)" % (utils.color.BOLD, question, utils.color.NORMAL),
                              choices=list(display_instances.keys()) + ['all'],
                              # default='all'
                              )
    else:
        q = inquirer.List('instance',
                           message="%s%s%s (enter to select)" % (utils.color.BOLD, question, utils.color.NORMAL),
                           choices=list(display_instances.keys()),
                           )
    questions.append(q)

    answers = None
    try:
        answers = inquirer.prompt(questions, raise_keyboard_interrupt=True)
    except KeyboardInterrupt:
        logger.error("Cancelled by user")
        sys.exit(1)

    if 'all' in answers["instance"]:
        selected_hosts = instances
    else:
        selected_hosts = []
        if not multiple:
            answers["instance"] = [answers["instance"]]
        for answer in answers["instance"]:
            selected_hosts.append(display_instances[answer])

    return selected_hosts


@utils.SupportedPlatforms('linux', 'windows', 'osx')
def ssh_handler(args, config):
    if config.dig('inventory', 'update_at_start') or args['-u']:
        update_handler(args, config)

    if args ['--no-tmux'] or config.dig('ssh', 'no-tmux'):
        question = "What instance would you like to ssh into?"
        targets = prompt_targets(question, targets=args['<host>'], config=config, multiple=False)
    else:
        question = "What instances would you like to ssh into?"
        targets = prompt_targets(question, targets=args['<host>'], config=config)

    if len(targets) == 0:
        logger.info("No matching instances found")
        sys.exit(1)

    commands = collections.OrderedDict()
    for idx, instance in enumerate(targets):
        name = '{}-{}'.format(instance.name, idx)
        commands[name] = Ssh(config, instance).command

    layout = None
    if args['--layout']:
        layout = args['--layout']

    if args['--no-tmux'] or config.dig('ssh', 'no-tmux'):
        cmd = list(commands.values())[0]
        if args['-d']:
            logger.debug(cmd)
        else:
            os.system(cmd)
    else:
        tmux.run(config, commands, args['-w'], layout, args['-d'], args['-s'])


@utils.SupportedPlatforms('linux', 'osx')
def mount_handler(args, config):
    Sshfs.ensure_sshfs_installed()

    if config.dig('inventory', 'update_at_start') or args['-u']:
        update_handler(args, config)

    fields = args['<host>:<remotedir>'].split(':')

    if len(fields) != 2:
        logger.error("Requires exactly 2 arguments: host:remotedir")
        sys.exit(1)

    desired_target, remotedir = fields
    instances = inventory.search(config, [desired_target])
    sshfs_objs = [Sshfs(config, instance, remotedir, dry_run=args['-d']) for instance in instances]
    unmounted_targets = [obj.instance for obj in sshfs_objs if not obj.is_mounted]

    question = "What instances would you like to have mounted?"
    target_instances = prompt_targets(question, instances=unmounted_targets, multiple=False, config=config)

    if len(target_instances) == 0:
        logger.info("No matching instances found")
        sys.exit(1)

    for sshfsObj in sshfs_objs:
        if sshfsObj.instance in target_instances:
            if sshfsObj.mount():
                logger.info("Mounted %s at %s" % (sshfsObj.instance.name, sshfsObj.mountpoint))
            else:
                logger.error("Unable to mount %s" % sshfsObj.instance.name)


@utils.SupportedPlatforms('linux', 'osx')
def list_mounts_handler(args, config):
    Sshfs.ensure_sshfs_installed()

    if args['-d']:
        return

    for mountpoint in Sshfs.mounts(config.mount_root_dir):
        logger.info(mountpoint)


@utils.SupportedPlatforms('linux', 'osx')
def unmount_handler(args, config):
    Sshfs.ensure_sshfs_installed()

    question = "What instances would you like to have unmounted?"

    if args['-a']:
        instances = inventory.instances(config)
        sshfs_objs = [Sshfs(config, instance, dry_run=args['-d']) for instance in instances]
        mounted_targets = [obj.instance for obj in sshfs_objs if obj.is_mounted]
        target_instances = mounted_targets
    else:
        desired_targets = args['<host>']
        instances = inventory.search(config, desired_targets)
        sshfs_objs = [Sshfs(config, instance, dry_run=args['-d']) for instance in instances]
        mounted_targets = [obj.instance for obj in sshfs_objs if obj.is_mounted]
        target_instances = prompt_targets(question, instances=mounted_targets, multiple=False, config=config)

    if len(target_instances) == 0:
        logger.error("No matching mounts found")
        if args['-a']:
            logger.warn("Did you select targets with <space> and confirm with <enter>?")
        sys.exit(1)

    for sshfsObj in sshfs_objs:
        if sshfsObj.instance in target_instances:
            if sshfsObj.unmount():
                logger.info("Unmounted %s" % sshfsObj.instance.name)
            else:
                logger.error("Unable to unmount %s" % sshfsObj.instance.name)


@utils.SupportedPlatforms('linux', 'windows', 'osx')
def list_inventory_handler(args, config):
    instances = []
    for ip, name, aliases in inventory.instances(config):
        if aliases:
            instances.append( (ip, name, ', '.join(aliases)) )
        else:
            instances.append( (ip, name, '--- None ---') )
    logger.info(tabulate(instances, headers=['Name', 'Address/Dns', 'Aliases']))


@utils.SupportedPlatforms('linux', 'windows', 'osx')
def update_handler(args, config):
    if args['-d']:
        return

    logger.warn("Updating inventory...")
    inventory_obj = inventory.inventory(config)
    inventory_obj.update()


@utils.SupportedPlatforms('linux', 'windows', 'osx')
def run_handler(args, config):
    # TODO: implement -d -a and -v

    task_name = args['<task>']
    task_playbook = config.dig('run', task_name)

    if not task_playbook:
        logger.error("Playbook %s not configured." % repr(task_name))
        sys.exit(1)

    inventory_obj = inventory.inventory(config)
    task = RunAnsibleTask(task_name, task_playbook[0], config, inventory_obj.instances())
    task.run()


def main():
    coloredlogs.install(fmt='%(message)s')

    if os.geteuid() == 0:
        logger.error("Do not run this as root")
        sys.exit(1)

    config = cfg.Config()
    config.create()
    config.read()
    config.verify()

    version = 'bridgy %s' % __version__
    args = docopt(__doc__, version=version)

    if not tmux.is_installed():
        if not args ['--no-tmux'] and not config.dig('ssh', 'no-tmux'):
            logger.warn("Tmux not installed. Cannot support split screen.")
        args['--no-tmux'] = True

    if args['-v']:
        coloredlogs.install(fmt='%(message)s', level='DEBUG')

    if args['-d']:
        args['-v'] = True
        coloredlogs.install(fmt='%(message)s', level='DEBUG')
        logger.warn("Performing dry run, no actions will be taken.")

    if args['--version']:
        logger.info(version)
        sys.exit(0)

    opts = {
        'ssh': ssh_handler,
        'mount': mount_handler,
        'list-mounts': list_mounts_handler,
        'list-inventory': list_inventory_handler,
        'unmount': unmount_handler,
        'update': update_handler,
        'run': run_handler,
    }

    for opt, handler in list(opts.items()):
        if args[opt]:
            try:
                handler(args, config)
            except utils.UnsupportedPlatform as ex:
                logger.error(ex.message)
                sys.exit(1)

if __name__ == '__main__':
    main()