"""
This module defines functions related to spinning a devnet L1 node, and deploying L1 contracts
on an L1 blockchain (for now only devnet, but in the future, any kind of L1).
"""
import os
import pathlib
import shutil
import subprocess
import sys
import datetime
import time

import l2
import libroll as lib
from config import Config
from processes import PROCESS_MGR


####################################################################################################

def deploy_devnet_l1(config: Config):
    """
    Spin the devnet L1 node, doing whatever tasks are necessary, including installing geth,
    generating the genesis file and config files, and deploying the L1 contracts.
    """
    os.makedirs(config.paths.gen_dir, exist_ok=True)

    pre_devnet(config)
    # in new version there should generate deploy_config json file first
    generate_devnet_l1_allocs(config)
    generate_devnet_l1_genesis(config)
    start_devnet_l1_node(config)
    print("Devnet L1 deployment is complete! L1 node is running.")


####################################################################################################

def pre_devnet(config: Config):
    """
    Generate the L1 genesis file (simply copies the template).
    """
    try:
        print("Pre devnet")
        lib.run(
            "pre-devnet",
            ["bash", "-c", "'make pre-devnet'"],
            cwd=config.paths.monorepo_dir)
    except Exception as err:
        raise lib.extend_exception(err, prefix="Failed to pre devnet: ")


####################################################################################################

def init_devnet_l1_deploy_config(config: Config, update_timestamp=False):
    """
    Init devnet L1 deploy config.
    """
    print("Init devnet l1 deploy config")
    deploy_config = lib.read_json_file(config.paths.deploy_config_template_path)
    if update_timestamp:
        deploy_config['l1GenesisBlockTimestamp'] = '{:#x}'.format(int(time.time()))
    lib.write_json_file(config.paths.deploy_config_template_path_source, deploy_config)


####################################################################################################

def generate_devnet_l1_allocs(config: Config):
    """
    Generate the L1 allocs file.
    """
    if os.path.exists(config.paths.l1_allocs_path):
        print("L1 allocs already generated.")
    else:
        print("Generating L1 allocs.")
        geth = start_devnet_l1_temp_node(config)
        try:
            l2.generate_deploy_config(config)
            l2.deploy_l1_contracts(config)
            data = lib.debug_dumpBlock(f"127.0.0.1:{config.l1_rpc_listen_port}")
            lib.write_json_file(config.paths.l1_allocs_path, lib.read_json(data)['result'])
            pass
        except Exception as err:
            raise lib.extend_exception(err, prefix="Failed to generate L1 allocs: ")
        finally:
            geth.terminate()


####################################################################################################

def generate_devnet_l1_genesis(config: Config):
    """
    Generate the L1 genesis file (simply copies the template).
    """
    if os.path.exists(config.paths.l1_genesis_path):
        print("L1 genesis already generated.")
    else:
        try:
            print("Generating L1 genesis.")
            lib.run(
                "generating L1 genesis",
                ["go", "run", "cmd/main.go", "genesis", "l1",
                 f"--deploy-config={config.deploy_config_path}",
                 f"--l1-allocs={config.paths.l1_allocs_path}"
                 f"--l1-deployments={config.paths.addresses_json_path}",
                 f"--outfile.l1={config.paths.l1_genesis_path}"],
                cwd=config.paths.op_node_dir)
            l2.generate_deploy_config(config)
            pass
        except Exception as err:
            raise lib.extend_exception(err, prefix="Failed to generate L1 genesis: ")


####################################################################################################

def start_devnet_l1_temp_node(config: Config):
    print("start a ")
    geth = subprocess.Popen([
        'geth', '--dev', '--chain-id', f"{config.l1_chain_id}", '--http', '--http.api', 'eth,debug',
        '--verbosity', '4', '--gcmode', 'archive', '--dev.gaslimit', '30000000',
        '--rpc.allow-unprotected-txs'
    ])
    lib.wait_for_rpc_server("127.0.0.1", config.l1_rpc_listen_port)
    return geth


####################################################################################################

def start_devnet_l1_node(config: Config):
    """
    Spin the devnet L1 node (currently: via `docker compose`), then wait for it to be ready.
    """

    lib.ensure_port_unoccupied(
        "L1 node", config.l1_rpc_listen_addr, config.l1_rpc_listen_port)

    # Create geth db if it doesn't exist.
    os.makedirs(config.l1_data_dir, exist_ok=True)

    if not os.path.exists(config.l1_keystore_dir):
        # Initial account setup
        print(f"Directory '{config.l1_keystore_dir}' missing, running account import.")
        with open(config.l1_password_path, "w") as f:
            f.write(config.l1_password)
        with open(config.l1_tmp_signer_key_path, "w") as f:
            f.write(config.l1_signer_private_key.replace("0x", ""))
        lib.run(
            "importing signing keys",
            ["geth", "account", "import",
             f"--datadir={config.l1_data_dir}",
             f"--password={config.l1_password_path}",
             config.l1_tmp_signer_key_path])
        os.remove(f"{config.l1_data_dir}/block-signer-key")

    if not os.path.exists(config.l1_chaindata_dir):
        log_file = "logs/init_l1_genesis.log"
        print(f"Directory {config.l1_chaindata_dir} missing, importing genesis in L1 node."
              f"Logging to {log_file}")
        lib.run(
            "initializing genesis",
            ["geth",
             f"--verbosity={config.l1_verbosity}",
             "init",
             f"--datadir={config.l1_data_dir}",
             config.paths.l1_genesis_path])

    log_file_path = "logs/l1_node.log"
    print(f"Starting L1 node. Logging to {log_file_path}")
    sys.stdout.flush()
    log_file = open(log_file_path, "w")

    # NOTE: The devnet L1 node must be an archive node, otherwise pruning happens within minutes of
    # starting the node. This could be an issue if the op-node is brought down or restarted later,
    # or if the sequencing window is larger than the time-to-prune.

    PROCESS_MGR.start(
        "starting geth",
        [
            "geth",

            f"--datadir={config.l1_data_dir}",
            f"--verbosity={config.l1_verbosity}",

            f"--networkid={config.l1_chain_id}",
            "--syncmode=full",  # doesn't matter, it's only us
            "--gcmode=archive",
            "--rpc.allow-unprotected-txs",  # allow legacy transactions for deterministic deployment

            # p2p network config
            f"--port={config.l1_p2p_port}",

            # No peers: the blockchain is only this node
            "--nodiscover",
            "--maxpeers=1",

            # HTTP JSON-RPC server config
            "--http",
            "--http.corsdomain=*",
            "--http.vhosts=*",
            f"--http.addr={config.l1_rpc_listen_addr}",
            f"--http.port={config.l1_rpc_listen_port}",
            "--http.api=web3,debug,eth,txpool,net,engine",

            # WebSocket JSON-RPC server config
            "--ws",
            f"--ws.addr={config.l1_rpc_ws_listen_addr}",
            f"--ws.port={config.l1_rpc_ws_listen_port}",
            "--ws.origins=*",
            "--ws.api=debug,eth,txpool,net,engine",

            # Configuration for clique signing, clique itself is enabled via the genesis file
            f"--unlock={config.l1_signer_account}",
            "--mine",
            f"--miner.etherbase={config.l1_signer_account}",
            f"--password={config.l1_password_path}",
            "--allow-insecure-unlock",

            # Authenticated RPC config
            f"--authrpc.addr={config.l1_authrpc_listen_addr}",
            f"--authrpc.port={config.l1_authrpc_listen_port}",
            # NOTE: The Optimism monorepo accepts connections from any host, and specifies the JWT
            # secret to be the same used on L2. We don't see any reason to do that (we never use
            # authenticated RPC), so we restrict access to localhost only (authrpc can't be turned
            # off), and don't specify the JWT secret (`--authrpc.jwtsecret=jwt_secret_path`) which
            # causes a random secret to be created in `config.l1_data_dir/geth/jwtsecret`.
            "--authrpc.vhosts=*",
            f"--authrpc.jwtsecret={config.jwt_secret_path}",

            # Metrics options
            *([] if not config.l1_metrics else [
                "--metrics",
                f"--metrics.port={config.l1_metrics_listen_port}",
                f"--metrics.addr={config.l1_metrics_listen_addr}"]),
        ], forward="fd", stdout=log_file)

    lib.wait_for_rpc_server("127.0.0.1", config.l1_rpc_listen_port)


####################################################################################################

def clean(config: Config):
    """
    Cleans up deployment files and databases, such that trying to start the devnet L1 node will
    proceed as though it had never been started before.
    """
    if os.path.exists(config.paths.gen_dir):
        path = os.path.join(config.paths.gen_dir, "genesis-l1.json")
        pathlib.Path(path).unlink(missing_ok=True)

    if os.path.exists(config.l1_data_dir):
        print(f"Cleaning up {config.l1_data_dir}")
        shutil.rmtree(config.l1_data_dir, ignore_errors=True)

####################################################################################################
