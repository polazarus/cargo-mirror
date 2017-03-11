#!/usr/bin/env python3

"""Create, update and setup a mirror for Rust crates (Cargo local-registry)"""

from argparse import ArgumentParser
from pathlib import Path
import multiprocessing as mp
from urllib.request import Request, urlopen, URLError
import urllib.request
import hashlib
import contextlib
import json
import shlex
import subprocess
import logging
import itertools


LOGGER = logging.getLogger("cargo-mirror")


FORMAT = "%(levelname)s: %(message)s"


HEADERS = {
    'User-Agent': 'cargo',
    'Accept': 'aplication/vnd.github.3.sha', # WTF!!!
    # 'If-None-Match' : '72ea7fde8af9d73ee1b97efe8177b936f2f11380',
}


CONFIGURATION = """
[source.crates-io]
registry = 'https://github.com/rust-lang/crates.io-index'
replace-with = 'local-mirror'

[source.local-mirror]
local-registry = '%s'
"""


def update_index_repository(index):
    """Update index"""
    escaped = shlex.quote(str(index))
    cmd = "cd %s; git fetch origin master; git reset --hard origin/master"
    cmd = cmd % escaped

    LOGGER.info("update index")
    LOGGER.debug("command: %s", cmd)
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=True)
        LOGGER.debug("output:\n%s", output.decode())
    except CallProcessError as e:
        LOGGER.error("cannot update the index at %s, return code %d", index, e.returncode)
        LOGGER.info("index update's output\n%s", e.output.decode())


class CrateVersion(object):
    """A version of a crate"""
    __slots__ = ('name', 'vers', 'yanked', 'cksum')

    def __init__(self, name, vers, yanked, cksum):
        self.name = name
        self.vers = vers
        self.yanked = yanked
        self.cksum = cksum

    @classmethod
    def fromjson(cls, line):
        """Parse a JSON version"""
        obj = json.loads(line)
        return cls(obj['name'],
                   obj['vers'],
                   obj['yanked'],
                   bytes.fromhex(obj['cksum']))

    def __repr__(self):
        return "<%s:%s>" % (self.name, self.vers)


def get_crate_versions(packfile):
    """Get versions of a particular crate given the pack file"""

    with packfile.open() as pack:
        try:
            for line in pack:
                yield CrateVersion.fromjson(line)
        except json.JSONDecodeError:
            LOGGER.exception("cannot parse %s in index", packfile)


def get_crates(indexdir):
    """Extract all information in the index"""

    for a in sorted(indexdir.iterdir()):
        if a.name[0] == '.' or a.name == 'config.json':
            continue
        if a.is_dir():
            yield from get_crates(a)
        else:
            yield from get_crate_versions(a)


def retrieve_and_hash(url, dst, hkind, raw=True, blocksize=8192, headers=None):
    """Retrieve some distant file and compute its hash"""

    hasher = hashlib.new(hkind)
    req = Request(url, headers=headers)

    with contextlib.closing(urlopen(req)) as resp:
        with dst.open('wb') as fdst:
            while True:
                block = resp.read(blocksize)
                if not block:
                    break
                fdst.write(block)
                hasher.update(block)

    if raw:
        return hasher.digest()
    else:
        return hasher.hexdigest()


def get_hash(fpath, hkind='sha256', raw=True, blocksize=8192):
    hasher = hashlib.new(hkind)
    with fpath.open('rb') as finput:
        while True:
            block = finput.read(blocksize)
            if not block:
                break
            hasher.update(block)
    if raw:
        return hasher.digest()
    else:
        return hasher.hexdigest()


def download_crate(cache, crate):
    """Download a crate version"""
    name = crate.name
    vers = crate.vers
    cksum = crate.cksum

    filepath = cache / ('%s-%s.crate' % (name, vers))

    if filepath.exists():
        if get_hash(filepath, hkind='sha256') == cksum:
            LOGGER.debug("%s:%s skip", name, vers)
            return
        else:
            LOGGER.warning("%s:%s invalid checksum retry", name, vers)
            filepath.unlink()

    LOGGER.info("%s:%s downloading", name, vers)
    filepath_tmp = filepath.with_suffix('.crate~')

    url = 'https://crates.io/api/v1/crates/%s/%s/download' % (name, vers)
    try:
        hashv = retrieve_and_hash(url, filepath_tmp, hkind="sha256", headers=HEADERS)
        if hashv == cksum:
            LOGGER.info("%s:%s downloaded", name, vers)
            filepath_tmp.rename(filepath)
        else:
            LOGGER.warning("%s:%s downloaded with invalid checksum (%s != %s)", name, vers, hashv.hex(), cksum.hex())
            filepath_tmp.rename(filepath.with_suffix('.crate~corrupted'))
    except URLError as e:
        LOGGER.error("%s: %s cannot download crate", name, vers)
        LOGGER.info("%s, reason: %s", url, str(e))


def dl(pair):
    """Download a crate"""
    try:
        download_crate(*pair)
    except KeyboardInterrupt:
        import sys
        sys.exit(1)
    except Exception as exc:
        LOGGER.exception("unexpected error")


def cleanup(directory):
    """Cleanup cache crates"""
    directory = Path(directory).resolve()
    index = directory / 'index'
    crates = set('%s-%s.crate' % (k.name, k.vers) for k in get_crates(index))

    for path in directory.glob('*.crate'):
        if path.name not in crates:
            LOGGER.info("remove %s", path)
            path.unlink()

    for path in directory.glob('*.crate~'):
        LOGGER.info("remove partial download %s", path)
        path.unlink()

    for path in directory.glob('*.crate~corrupted'):
        LOGGER.info("remove corrupted download %s", path)
        path.unlink()


def update(directory, parallel=0):
    """ Update index and cache crates"""
    directory = Path(directory).resolve()
    index = directory / 'index'

    if not index.exists():
        LOGGER.error("invalid local registry: no index")
        return

    update_index_repository(index)
    crates = list(get_crates(index))
    LOGGER.info("%d crates to synchronize", len(crates))

    if parallel is 1:
        for crate in crates:
            try:
                download_crate(directory, crate)
            except KeyboardError:
                LOGGER.error("update aborted by user")
                return
    else:
        if parallel <= 0:
            parallel = mp.cpu_count()
        LOGGER.info("start downloading with %d jobs", parallel)

        def _dl(crate):
            download_crate(directory, crate)

        dir_crates = zip(itertools.repeat(directory), crates)

        with mp.Pool(parallel) as pool:
            try:
                # for crate in crates:
                #     pool.apply_async(download_crate, [directory, crate])
                for _ in pool.imap_unordered(dl, dir_crates):
                    pass
            except KeyboardInterrupt:
                LOGGER.error("update aborted by user")
                return
            pool.close()
            pool.join()


def install(index, cache, config_file):
    """Setup the mirror as a local registry or show how to do it"""
    index = Path(index).expanduser().resolve()
    cache = Path(cache).expanduser().resolve()

    if config_file is None:
        print("To finish, manually add this few lines to your cargo configuration (.cargo/config):")
        print()
        print(CONFIGURATION % index)
        return
    try:
        import toml
    except ImportError:
        LOGGER.error("require toml python package (pip install toml)")
        return

    config_file = Path(config_file).expanduser().resolve()

    LOGGER.info("configuring mirror in %s", config_file)

    if config_file.exists():
        with config_file.open("rt") as fobj:
            config = toml.load(fobj)
    else:
        config = dict()

    registry = config.setdefault("registry", dict())
    registry["index"] = "file://%s" % index

    config_file.parent.mkdir(exist_ok=True, parents=True)
    with config_file.open("wt") as fobj:
        toml.dump(config, fobj)
    print("%s configured." % config_file)


def initialize(directory, new=False):
    """Initialize a cargo mirror"""
    directory = Path(directory).expanduser()
    index = directory / 'index'

    if new:
        if directory.exists():
            LOGGER.error("cannot create new mirror in %s", directory)
            LOGGER.info("%s already exists", directory)
            return

        directory.mkdir(parents=True)

    else:
        if not directory.exists():
            LOGGER.error("cannot initialize mirror in %s", directory)
            LOGGER.info("%s does not exists", directory)
            return
        if not directory.is_dir():
            LOGGER.error("cannot initialize mirror in %s", directory)
            LOGGER.info("%s is not a directory", directory)
            return
        if index.exists():
            LOGGER.error("cannot initialize mirror in %s", directory)
            LOGGER.info("%s already exists", index)
            return

    escaped = shlex.quote(str(index))
    cmd = 'git clone --depth 1 https://github.com/rust-lang/crates.io-index %s' % escaped

    LOGGER.info("clone crates.io index")
    LOGGER.debug("command: %s", cmd)
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=True)
        LOGGER.debug("output:\n%s", output.decode())
    except CallProcessError as e:
        LOGGER.error("cannot clone index to %s (return code: %d)", index, e.returncode)
        LOGGER.info("index update's output\n%s", e.output.decode())


def main():
    """Main"""
    parser = ArgumentParser(description="A tool to make and keep up-to-date a crates.io mirror")

    parser.add_argument('-v', '--verbose', action="store_const", dest="loglevel",
                        const=logging.INFO,
                        help="display more information")
    parser.add_argument('-d', '--debug', action="store_const", dest="loglevel",
                        const=logging.DEBUG,
                        help="display debug information")

    subparsers = parser.add_subparsers(title="commands",
                                       description="valid commands",
                                       dest="action")

    parser_new = subparsers.add_parser("new", help="create a new mirror")
    parser_new.add_argument("dir", help="output directory for mirror")

    parser_init = subparsers.add_parser("init", help="initialize a mirror")
    parser_init.add_argument("dir", help="output directory for mirror")

    parser_update = subparsers.add_parser("update", help="update a mirror")
    parser_update.add_argument("dir", nargs="?", default=".",
                               help="mirror directory (default: .)")
    parser_update.add_argument("-j","--parallel", nargs='?', metavar="N",
                               default=0, type=int,
                               help="parallelize downloads up on N jobs (default: cpu count)")

    parser_cleanup = subparsers.add_parser("cleanup", help="cleanup a mirror")
    parser_cleanup.add_argument("dir", nargs="?", default=".",
                               help="mirror directory (default: .)")

    parser_install = subparsers.add_parser("install", help="install a mirror")
    parser_install.add_argument("dir", nargs="?", default=".",
                                help="mirror directory (default: .)")

    parser_install.add_argument("--global",
                                dest="config_file",
                                action="store_const",
                                const="~/.cargo/config",
                                help="set Cargo config globally (default)")
    parser_install.add_argument("--local",
                                dest="config_file",
                                action="store_const",
                                const=".cargo/config",
                                help="set Cargo config locally (current directory)")
    parser_install.add_argument("--config-file",
                                help="setup mirror in a custom config file")

    #parser_install.set_defaults(config_file="~/.cargo/config")
    parser_install.set_defaults(config_file=None, loglevel=logging.WARNING)

    options = parser.parse_args()
    logging.basicConfig(level=options.loglevel, format=FORMAT)

    if options.action == "new":
        initialize(options.dir, new=True)
    elif options.action == "init":
        initialize(options.dir)
    elif options.action == "update":
        update(options.dir, parallel=options.parallel)
    elif options.action == "cleanup":
        cleanup(options.dir)
    elif options.action == "install":
        install("index", "cache", options.config_file)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
