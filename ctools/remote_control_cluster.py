#!/usr/bin/env python3
#
help_string = '''
Tool to create and manipulate a MongoDB sharded cluster given SSH and MongoDB port access to a set
of hosts. When launching the EC2 hosts, please ensure that the machine from which they are being
connected to has access in the inbound rules.

The intended usage is:

1. Use the `launch_ec2_cluster_hosts.py Cluster launch` script in order to spawn a set of hosts in
   EC2 on which the MongoDB processes will run.
2. This script will produce a cluster description Cluster.json file which contains the specific
   hosts and will serve as an input for the `remote_control_cluster.py` utilities.
3. Update the 'MongoBinPath' parameter in Cluster.json and run the
   `remote_control_cluster.py create Cluster.json` command in order to launch the processes.

Use --help for more information on the supported commands.
'''

import argparse
import asyncio
import copy
import json
import logging
import motor.motor_asyncio
import sys

from common.common import yes_no
from common.remote_common import RemoteSSHHost
from common.version import CTOOLS_VERSION
from signal import Signals

# Ensure that the caller is using python 3
if (sys.version_info[0] < 3):
    raise Exception("Must be using Python 3")


class RemoteMongoHost(RemoteSSHHost):
    '''
    Specialisation of 'RemoteSSHHost' which will be running MongoDB services (mongod/mongos, etc)
    '''

    def __init__(self, host_desc):
        '''
        Constructs a cluster host object from host description, which is a superset of the
        description required by 'RemoteSSHHost' above. The additional fields are:
          RemoteMongoDPath: Path on the host to serve as a root for the MongoD service's data and
            logs. Defaulted to $HOME/mongod_data.
          RemoteMongoSPath: Path on the host to serve as a root for the MongoS service's data and
            logs. Defaulted to $HOME/mongos_data.
        '''

        RemoteSSHHost.__init__(self, host_desc)

        default_mongod_data_path = '$HOME/mongod_data'
        default_mongos_data_path = '$HOME/mongos_data'

        # Populate parameter defaults
        if 'RemoteMongoDPath' not in self.host_desc:
            self.host_desc['RemoteMongoDPath'] = default_mongod_data_path
        if 'RemoteMongoSPath' not in self.host_desc:
            self.host_desc['RemoteMongoSPath'] = default_mongos_data_path

    async def start_mongod_instance(self, port, repl_set_name, extra_args=[]):
        '''
        Starts a single MongoD instance on this host, as part of a replica set called 'repl_set_name',
        listening on 'port'. The 'extra_args' is a list of additional command line arguments to
        specify to the mongod command line.
        '''

        await self.exec_remote_ssh_command(
            (f'mkdir -p {self.host_desc["RemoteMongoDPath"]} && '
             f'$HOME/binaries/mongod --replSet {repl_set_name} '
             f'--dbpath {self.host_desc["RemoteMongoDPath"]} '
             f'--logpath {self.host_desc["RemoteMongoDPath"]}/mongod.log '
             f'--port {port} '
             f'--bind_ip_all '
             f'{" ".join(extra_args)} '
             f'--fork '))

    async def start_mongos_instance(self, port, config_server, extra_args=[]):
        '''
        Starts a single MongoS instance on this host, listening on 'port', pointing to
        'config_server'. The 'extra_args' is a list of additional command line arguments to specify
        to the mongos command line.
        '''

        await self.exec_remote_ssh_command(
            (f'mkdir -p {self.host_desc["RemoteMongoSPath"]} && '
             f'$HOME/binaries/mongos --configdb config/{config_server.host}:27019 '
             f'--logpath {self.host_desc["RemoteMongoSPath"]}/mongos.log '
             f'--port {port} '
             f'--bind_ip_all '
             f'{" ".join(extra_args)} '
             f'--fork '))


class ClusterBuilder:
    '''
    Wraps information and common management tasks for the whole cluster
    '''

    def __init__(self, cluster_config):
        '''
        Constructs a cluster object from a JSON object with the following fields:
         Name: Name for the cluster (used only for logging purposes)
         Hosts: Array of JSON objects, each of which must follow the format for RemoteMongoHost
          above
        '''

        self.config = cluster_config

        self.name = self.config['Name']
        self.feature_flags = [f'--setParameter {f}=true' for f in self.config["FeatureFlags"]
                              ] if "FeatureFlags" in self.config else []
        self.mongod_parameters = self.config['MongoDParameters'] + self.feature_flags
        self.mongos_parameters = self.config['MongoSParameters'] + self.feature_flags

        def make_remote_mongo_host_with_global_config(host_idx_and_info):
            '''
            Constructs a RemoteMongoHost and populates default configuration
            '''

            host_idx = host_idx_and_info[0]
            if (host_idx < 3):
                shard = 'config'
            elif (host_idx < 6):
                shard = 'shard0'
            elif (host_idx < 9):
                shard = 'shard1'
            else:
                raise Exception('Too many hosts')

            host_info = host_idx_and_info[1]

            if (isinstance(host_info, str)):
                host_info = {'host': host_info}
            else:
                host_info = copy.deepcopy(host_info)

            if 'RemoteMongoDPath' in self.config and not 'RemoteMongoDPath' in host_info:
                host_info['RemoteMongoDPath'] = self.config['RemoteMongoDPath']
            if 'RemoteMongoSPath' in self.config and not 'RemoteMongoSPath' in host_info:
                host_info['RemoteMongoSPath'] = self.config['RemoteMongoSPath']

            host_info['shard'] = shard

            return RemoteMongoHost(host_info)

        self.available_hosts = list(
            map(make_remote_mongo_host_with_global_config,
                zip(range(0, len(self.config['Hosts'])), self.config['Hosts'])))

        logging.info(
            f"Cluster will consist of: {list(map(lambda h: (h.host_desc['host'], h.host_desc['shard']), self.available_hosts))}"
        )

        # Split the cluster hosts into 3 replica sets
        self.config_hosts = self.available_hosts[0:3]
        self.shard0_hosts = self.available_hosts[3:6]
        self.shard1_hosts = self.available_hosts[6:9]

    @property
    def connection_string(self):
        '''
        Chooses the connection string for the cluster. It effectively selects the config server
        hosts because they contain no load.
        '''

        return f'mongodb://{self.config_hosts[1].host}'

    async def get_description(self):
        return f'''
Cluster {cluster.name} started with:
  MongoS: {cluster.connection_string}
  ConfigServer: {cluster.config_hosts}
  Shard0: {cluster.shard0_hosts}
  Shard1: {cluster.shard1_hosts}
'''

    async def start_mongod_as_replica_set(self, hosts, port, repl_set_name, extra_args):
        '''
        Starts the mongod processes on the specified 'hosts', where each host will be listening on
        'port' and will be initiated as part of 'repl_set_name'. The 'extra_args' is a list of
        additional command line arguments to specify to the mongod command line.
        '''

        # Start the Replica Set hosts
        tasks = []
        for host in hosts:
            tasks.append(
                asyncio.ensure_future(host.start_mongod_instance(port, repl_set_name, extra_args)))
        await asyncio.gather(*tasks)

    async def rsync(self, local_pattern, remote_path, single_shard=None):
        '''
        Rsyncs all the files that match 'local_pattern' to the 'remote_path' on all nodes or on a
        single shard only
        '''

        tasks = []

        for host in self.available_hosts:
            if single_shard and host.host_desc['shard'] != single_shard:
                continue

            tasks.append(
                asyncio.ensure_future(host.rsync_files_to_remote(local_pattern, remote_path)))

        await asyncio.gather(*tasks)


async def stop_mongo_processes(cluster, signal, shard=None):
    '''
    Stops processes that might have been left over from a previous run, on all nodes
    '''

    logging.info(f"Stopping mongo processes for {shard if shard else 'all shards'}")
    tasks = []
    for host in cluster.available_hosts:
        if shard and host.host_desc['shard'] != shard:
            continue

        tasks.append(
            asyncio.ensure_future(
                host.exec_remote_ssh_command(
                    (f'killall --wait -s {signal.name} mongo mongod mongos || true'))))
    await asyncio.gather(*tasks)


async def cleanup_mongo_directories(cluster, shard=None):
    '''
    Cleanup directories that might have been left over from a previous run, on all nodes
    '''

    logging.info(f"Cleaning leftover directories for {shard if shard else 'all shards'}")
    tasks = []
    for host in cluster.available_hosts:
        if shard and host.host_desc['shard'] != shard:
            continue

        tasks.append(
            asyncio.ensure_future(
                host.exec_remote_ssh_command((f'rm -rf {host.host_desc["RemoteMongoDPath"]} ;'
                                              f'rm -rf {host.host_desc["RemoteMongoSPath"]}'))))
    await asyncio.gather(*tasks)


async def install_prerequisite_packages(cluster):
    '''
    Install (using apt) all the prerequisite libraries that the mongodb binaries require, on all nodes
    '''

    logging.info('Installing prerequisite packages')
    tasks = []
    for host in cluster.available_hosts:
        tasks.append(
            asyncio.ensure_future(
                host.exec_remote_ssh_command(
                    'sudo apt -y update && sudo apt -y install libsnmp-dev')))
    await asyncio.gather(*tasks)


async def start_config_replica_set(cluster):
    logging.info('Starting config processes')

    await cluster.start_mongod_as_replica_set(
        cluster.config_hosts,
        27019,
        'config',
        [
            '--configsvr',
        ] + cluster.mongod_parameters,
    )


async def start_shard_replica_set(cluster, shard_hosts, shard_name):
    logging.info(f'Starting shard processes for {shard_name}')

    await cluster.start_mongod_as_replica_set(
        shard_hosts,
        27018,
        shard_name,
        [
            '--shardsvr',
        ] + cluster.mongod_parameters,
    )


async def initiate_replica_set(hosts, port, repl_set_name):
    '''
    Initiates 'hosts' as a replica set with a name of 'repl_set_name' and 'hosts':'port' as members
    '''

    connection_string = f'mongodb://{hosts[0].host}:{port}'
    replica_set_members = list(
        map(
            lambda id_and_host: {
                '_id': id_and_host[0],
                'host': f'{id_and_host[1].host}:{port}',
                'priority': 2 if id_and_host[0] == 0 else 1,
            }, zip(range(0, len(hosts)), hosts)))

    logging.info(
        f'Connecting to {connection_string} in order to initiate it as {repl_set_name} with members of {replica_set_members}'
    )

    mongo_client = motor.motor_asyncio.AsyncIOMotorClient(connection_string, directConnection=True)
    logging.info(await mongo_client.admin.command({
        'replSetInitiate': {
            '_id': repl_set_name,
            'members': replica_set_members,
        },
    }))


async def start_mongos_processes(cluster):
    logging.info('Starting mongos processes ...')

    tasks = []
    for host in cluster.available_hosts:
        tasks.append(
            asyncio.ensure_future(
                host.start_mongos_instance(
                    27017,
                    cluster.config_hosts[0],
                    cluster.mongos_parameters,
                )))
    await asyncio.gather(*tasks)

    logging.info('Mongos processes started')


##################################################################################################
#
# Main methods implementations for the various sub-commands
#
##################################################################################################


async def main_create(args, cluster):
    '''Implements the create command'''

    yes_no(('Start creating the cluster from scratch.'
            'WARNING: The next steps will erase all existing data on the specified hosts.'))

    await stop_mongo_processes(cluster, Signals.SIGKILL)
    await cleanup_mongo_directories(cluster)
    await install_prerequisite_packages(cluster)
    await cluster.rsync(f'{cluster.config["MongoBinPath"]}/mongo*', '$HOME/binaries')
    await start_config_replica_set(cluster)
    await start_shard_replica_set(cluster, cluster.shard0_hosts, 'shard0')
    await start_shard_replica_set(cluster, cluster.shard1_hosts, 'shard1')

    await initiate_replica_set(cluster.config_hosts, 27019, 'config')
    await initiate_replica_set(cluster.shard0_hosts, 27018, 'shard0')
    await initiate_replica_set(cluster.shard1_hosts, 27018, 'shard1')

    # MongoS instances
    await start_mongos_processes(cluster)

    logging.info(f'Connecting to {cluster.connection_string}')

    mongo_client = motor.motor_asyncio.AsyncIOMotorClient(cluster.connection_string)
    logging.info(await mongo_client.admin.command({
        'addShard': f'shard0/{cluster.shard0_hosts[0].host}:27018',
        'name': 'shard0',
    }))
    logging.info(await mongo_client.admin.command({
        'addShard': f'shard1/{cluster.shard1_hosts[0].host}:27018',
        'name': 'shard1',
    }))

    logging.info(await cluster.get_description())


async def main_describe(args, cluster):
    logging.info(await cluster.get_description())


async def main_start(args, cluster):
    '''Implements the start command'''

    if not args.shard or args.shard == 'config':
        await start_config_replica_set(cluster)
    if not args.shard or args.shard == 'shard0':
        await start_shard_replica_set(cluster, cluster.shard0_hosts, 'shard0')
    if not args.shard or args.shard == 'shard1':
        await start_shard_replica_set(cluster, cluster.shard1_hosts, 'shard1')

    await start_mongos_processes(cluster)


async def main_stop(args, cluster):
    '''Implements the stop command'''

    await stop_mongo_processes(cluster, args.signal, args.shard)


async def main_run(args, cluster):
    '''Implements the run command'''

    tasks = []
    for host in cluster.available_hosts:
        if args.shard and host.host_desc['shard'] != args.shard:
            continue

        tasks.append(asyncio.ensure_future(host.exec_remote_ssh_command(args.command)))
    await asyncio.gather(*tasks)


async def main_rsync(args, cluster):
    '''Implements the rsync command'''

    await cluster.rsync(args.local_pattern, args.remote_path, args.shard)


async def main_deploy_binaries(args, cluster):
    '''Implements the deploy-binaries command'''

    await cluster.rsync(f'{cluster.config["MongoBinPath"]}/mongo*', '$HOME/binaries', args.shard)


async def main_gather_logs(args, cluster):
    '''Implements the gather-logs command'''

    def make_host_suffix(host, process_name):
        if host in cluster.config_hosts:
            return f'{process_name}-CSRS-{host.host}'
        elif host in cluster.shard0_hosts:
            return f'{process_name}-Shard0-{host.host}'
        elif host in cluster.shard1_hosts:
            return f'{process_name}-Shard1-{host.host}'

    # Compress MongoD logs and FTDC
    tasks = []
    for host in cluster.available_hosts:
        if args.shard and host.host_desc['shard'] != args.shard:
            continue

        tasks.append(
            asyncio.ensure_future(
                host.exec_remote_ssh_command((
                    f'tar zcvf {host.host_desc["RemoteMongoDPath"]}/{make_host_suffix(host, "mongod")}.tar.gz '
                    f'{host.host_desc["RemoteMongoDPath"]}/mongod.log* '
                    f'{host.host_desc["RemoteMongoDPath"]}/diagnostic.data'))))
    await asyncio.gather(*tasks)

    # Compress MongoS logs and FTDC
    tasks = []
    for host in cluster.available_hosts:
        if args.shard and host.host_desc['shard'] != args.shard:
            continue

        tasks.append(
            asyncio.ensure_future(
                host.exec_remote_ssh_command((
                    f'tar zcvf {host.host_desc["RemoteMongoSPath"]}/{make_host_suffix(host, "mongos")}.tar.gz '
                    f'{host.host_desc["RemoteMongoSPath"]}/mongos.log* '
                    f'{host.host_desc["RemoteMongoSPath"]}/mongos.diagnostic.data'))))
    await asyncio.gather(*tasks)

    # Rsync files locally (do nor rsync more than 3 at a time)
    sem_max_concurrent_rsync = asyncio.Semaphore(3)

    async def rsync_with_semaphore(host):
        async with sem_max_concurrent_rsync:
            await host.rsync_files_to_local(
                f'{host.host_desc["RemoteMongoDPath"]}/{make_host_suffix(host, "mongod")}.tar.gz',
                f'{args.local_path}/{cluster.name}/')
            await host.rsync_files_to_local(
                f'{host.host_desc["RemoteMongoSPath"]}/{make_host_suffix(host, "mongos")}.tar.gz',
                f'{args.local_path}/{cluster.name}/')

    tasks = []
    for host in cluster.available_hosts:
        if args.shard and host.host_desc['shard'] != args.shard:
            continue

        tasks.append(asyncio.ensure_future(rsync_with_semaphore(host)))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    argsParser = argparse.ArgumentParser(description=help_string)
    logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)

    argsParser.add_argument(
        'clusterconfigfile',
        help='JSON-formatted text file which contains the configuration of the cluster', type=str)
    subparsers = argsParser.add_subparsers(title='subcommands')

    ###############################################################################################
    # Arguments for the 'create' command
    parser_create = subparsers.add_parser('create',
                                          help='Creates (or overwrites) a brand new cluster')
    parser_create.set_defaults(func=main_create)

    ###############################################################################################
    # Arguments for the 'describe' command
    parser_create = subparsers.add_parser('describe', help='Describes the nodes of a cluster')
    parser_create.set_defaults(func=main_describe)

    ###############################################################################################
    # Arguments for the 'start' command
    parser_start = subparsers.add_parser(
        'start', help='Starts all the processes of an already created cluster')
    parser_start.add_argument('--shard', nargs='?', type=str,
                              help='Limit the command to just one shard')
    parser_start.set_defaults(func=main_start)

    # Arguments for the 'stop' command
    parser_stop = subparsers.add_parser(
        'stop',
        help='Stops all the processes of an already created cluster using a specified signal')
    parser_stop.add_argument(
        '--signal', type=lambda x: Signals[x],
        help=f'The signal to use for terminating the processes. One of {[e.name for e in Signals]}',
        default=Signals.SIGTERM)
    parser_stop.add_argument('--shard', nargs='?', type=str,
                             help='Limit the command to just one shard')
    parser_stop.set_defaults(func=main_stop)

    ###############################################################################################
    # Arguments for the 'run' command
    parser_run = subparsers.add_parser('run', help='Runs a command')
    parser_run.add_argument('command', help='The command to run')
    parser_run.add_argument('--shard', nargs='?', type=str,
                            help='Limit the command to just one shard')
    parser_run.set_defaults(func=main_run)

    ###############################################################################################
    # Arguments for the 'rsync' command
    parser_rsync = subparsers.add_parser('rsync',
                                         help='Rsyncs a set of file from a local to remote path')
    parser_rsync.add_argument('local_pattern', help='The local pattern from which to rsync')
    parser_rsync.add_argument('remote_path', help='The remote path to which to rsync')
    parser_rsync.add_argument('--shard', nargs='?', type=str,
                              help='Limit the command to just one shard')
    parser_rsync.set_defaults(func=main_rsync)

    ###############################################################################################
    # Arguments for the 'deploy-binaries' command
    parser_deploy_binaries = subparsers.add_parser(
        'deploy-binaries',
        help='Specialisation of the rsync command which only deploys binaries from MongoBinPath')
    parser_deploy_binaries.add_argument('--shard', nargs='?', type=str,
                                        help='Limit the command to just one shard')
    parser_deploy_binaries.set_defaults(func=main_deploy_binaries)

    ###############################################################################################
    # Arguments for the 'gather-logs' command
    parser_gather_logs = subparsers.add_parser(
        'gather-logs',
        help='Compresses and rsyncs the set of logs from the cluster to a local directory')
    parser_gather_logs.add_argument('local_path', help='The local to which to rsync')
    parser_gather_logs.add_argument('--shard', nargs='?', type=str,
                                    help='Limit the command to just one shard')
    parser_gather_logs.set_defaults(func=main_gather_logs)

    ###############################################################################################

    args = argsParser.parse_args()
    logging.info(f"CTools version {CTOOLS_VERSION} starting with arguments: '{args}'")

    with open(args.clusterconfigfile) as f:
        cluster_config = json.load(f)

    logging.info(f'Configuration: {json.dumps(cluster_config, indent=2)}')
    cluster = ClusterBuilder(cluster_config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(args.func(args, cluster))
