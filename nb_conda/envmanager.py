# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

import json
import os
import re
from tempfile import NamedTemporaryFile
import typing

from packaging.version import parse
from subprocess import Popen, PIPE

from tornado import gen, ioloop
from traitlets.config.configurable import LoggingConfigurable
from traitlets import Dict, List, Bool, Unicode


def pkg_info(s):
    return {
        "build_number": s.get("build_number"),
        "build_string": s.get("build_string", s.get("build")),
        "channel": s.get("channel"),
        "name": s.get("name"),
        "platform": s.get("platform"),
        "version": s.get("version"),
    }


MAX_LOG_OUTPUT = 6000

CONDA_EXE = os.environ.get("CONDA_EXE", "conda")

# try to match lines of json
JSONISH_RE = r'(^\s*["\{\}\[\],\d])|(["\}\}\[\],\d]\s*$)'

# these are the types of environments that can be created
package_map = {
    "python2": "python=2 ipykernel",
    "python3": "python=3 ipykernel",
    "r": "r-base r-essentials",
}


class EnvManager(LoggingConfigurable):
    envs = Dict()
    options = Dict(
        traits={
            "channels": List(
                trait=Unicode(), help="Additional channel to include in the export."
            ),
            "override-channels": Bool(False, help="Do not include .condarc channels."),
            "offline": Bool(False, help="Offline mode, don't connect to the Internet."),
        },
        help="Conda command lines options.",
        config=True,
    )

    def get_cmd_options(
        self, options: typing.Optional[typing.List[str]] = None
    ) -> typing.List[str]:
        cmd_options = []

        if options is None:
            options = []

        if "channels" in options:
            for channel in self.options["channels"]:
                cmd_options.extend(["-c", channel])
        for option in ("override-channels", "offline"):
            if option in options:
                cmd_options.append("--" + option)

        return cmd_options

    def _call_subprocess(self, cmdline: typing.List[str]):
        process = Popen(cmdline, stdout=PIPE, stderr=PIPE)
        output, error = process.communicate()
        return (process.returncode, output, error)

    @gen.coroutine
    def _execute(
        self, cmd: str, *args, options: typing.Optional[typing.List[str]] = None
    ) -> str:
        cmdline = cmd.split()
        cmdline.extend(self.get_cmd_options(options))
        cmdline.extend(args)

        self.log.debug("[nb_conda] command: %s", cmdline)

        # process = Popen(cmdline, stdout=PIPE, stderr=PIPE)
        # output, error = process.communicate()
        current_loop = ioloop.IOLoop.current()
        returncode, output, error = yield current_loop.run_in_executor(
            None, self._call_subprocess, cmdline
        )

        if returncode == 0:
            output = output.decode("utf-8")
        else:
            self.log.debug("[nb_conda] exit code: %s", returncode)
            output = error.decode("utf-8")

        self.log.debug("[nb_conda] output: %s", output[:MAX_LOG_OUTPUT])

        if len(output) > MAX_LOG_OUTPUT:
            self.log.debug("[nb_conda] ...")

        return output

    @gen.coroutine
    def list_envs(self) -> typing.Dict[str, str]:
        """List all environments that conda knows about"""
        output = yield self._execute(CONDA_EXE + " info --json")
        info = self.clean_conda_json(output)
        default_env = info["default_prefix"]

        root_env = {
            "name": "base",
            "dir": info["root_prefix"],
            "is_default": info["root_prefix"] == default_env,
        }

        def get_info(env):
            return {
                "name": os.path.basename(env),
                "dir": env,
                "is_default": env == default_env,
            }

        envs_folder = os.path.join(info["root_prefix"], "envs")

        return {
            "environments": [root_env]
            + [get_info(env) for env in info["envs"] if env.startswith(envs_folder)]
        }

    @gen.coroutine
    def delete_env(self, env: str) -> dict:
        output = yield self._execute(CONDA_EXE + " env remove -y -q --json -n " + env)
        return self.clean_conda_json(output)

    def clean_conda_json(self, output: str) -> dict:
        lines = output.splitlines()

        try:
            return json.loads("\n".join(lines))
        except (ValueError, json.JSONDecodeError) as err:
            self.log.warn("[nb_conda] JSON parse fail:\n%s", err)

        # try to remove bad lines
        lines = [line for line in lines if re.match(JSONISH_RE, line)]

        try:
            return json.loads("\n".join(lines))
        except (ValueError, json.JSONDecodeError) as err:
            self.log.error("[nb_conda] JSON clean/parse fail:\n%s", err)

        return {"error": True}

    @gen.coroutine
    def export_env(self, env: str) -> str:
        output = yield self._execute(CONDA_EXE + " list -e -n " + env)
        return output

    @gen.coroutine
    def clone_env(self, env: str, name: str) -> dict:
        output = yield self._execute(
            CONDA_EXE + " create -y -q --json -n " + name + " --clone " + env,
            options=list(self.options),
        )
        return self.clean_conda_json(output)

    @gen.coroutine
    def create_env(self, env: str, type: str) -> dict:
        packages = package_map[type]
        output = yield self._execute(
            CONDA_EXE + " create -y -q --json -n " + env,
            *packages.split(),
            options=list(self.options)
        )
        return self.clean_conda_json(output)

    @gen.coroutine
    def import_env(self, env: str, file_content: str) -> dict:
        with NamedTemporaryFile(mode="w", delete=False) as f:
            name = f.name
            f.write(file_content)
        output = yield self._execute(
            CONDA_EXE + " create -y -q --json -n " + env + " --file " + name,
            options=list(self.options),
        )
        os.unlink(name)
        return self.clean_conda_json(output)

    @gen.coroutine
    def env_channels(self, env: str) -> typing.List[str]:
        output = yield self._execute(CONDA_EXE + " config --show --json")
        info = self.clean_conda_json(output)
        if env != "base":
            old_prefix = os.environ["CONDA_PREFIX"]
            envs = yield self.list_envs()
            envs = envs["environments"]
            env_dir = None
            for env in envs:
                if env["name"] == env:
                    env_dir = env["dir"]
                    break

            os.environ["CONDA_PREFIX"] = env_dir
            output = yield self._execute(CONDA_EXE + " config --show --json")
            info = self.clean_conda_json(output)
            os.environ["CONDA_PREFIX"] = old_prefix

        deployed_channels = {}

        def get_uri(spec):
            location = "/".join((spec["location"], spec["name"]))
            if location[0] != "/":
                location = "/" + location
            return spec["scheme"] + "://" + location

        for channel in info["channels"]:
            if channel in info["custom_multichannels"]:
                deployed_channels[channel] = [
                    get_uri(entry) for entry in info["custom_multichannels"][channel]
                ]
            elif os.path.sep not in channel:
                spec = info["channel_alias"]
                spec["name"] = channel
                deployed_channels[channel] = [get_uri(spec)]
            else:
                deployed_channels[channel] = ["file:///" + channel]

        self.log.debug("[nb_conda] {} channels: {}".format(env, deployed_channels))

        if "error" in info:
            return info
        return {"channels": deployed_channels}

    @gen.coroutine
    def env_packages(self, env: str) -> dict:
        output = yield self._execute(CONDA_EXE + " list --no-pip --json -n " + env)
        data = self.clean_conda_json(output)

        # Data structure
        #   List of dictionary. Example:
        # {
        #     "base_url": null,
        #     "build_number": 0,
        #     "build_string": "py36_0",
        #     "channel": "defaults",
        #     "dist_name": "anaconda-client-1.6.14-py36_0",
        #     "name": "anaconda-client",
        #     "platform": null,
        #     "version": "1.6.14"
        # }

        if "error" in data:
            # we didn't get back a list of packages, we got a dictionary with
            # error info
            return data

        return {"packages": [pkg_info(package) for package in data]}

    @gen.coroutine
    def list_available(self, env: str):
        output = yield self._execute(
            CONDA_EXE + " search --json -n " + env, options=list(self.options)
        )

        data = self.clean_conda_json(output)

        if "error" in data:
            # we didn't get back a list of packages, we got a
            # dictionary with error info
            return data

        packages = []

        # Data structure
        #  Dictionary with package name key and value is a list of dictionary. Example:
        #  {
        #   "arch": "x86_64",
        #   "build": "np17py33_0",
        #   "build_number": 0,
        #   "channel": "https://repo.anaconda.com/pkgs/free/win-64",
        #   "constrains": [],
        #   "date": "2013-02-20",
        #   "depends": [
        #     "numpy 1.7*",
        #     "python 3.3*"
        #   ],
        #   "fn": "astropy-0.2-np17py33_0.tar.bz2",
        #   "license": "BSD",
        #   "md5": "3522090a8922faebac78558fbde9b492",
        #   "name": "astropy",
        #   "platform": "win32",
        #   "size": 3352442,
        #   "subdir": "win-64",
        #   "url": "https://repo.anaconda.com/pkgs/free/win-64/astropy-0.2-np17py33_0.tar.bz2",
        #   "version": "0.2"
        # }

        # List all available version for packages
        for entries in data.values():
            pkg_entry = None
            versions = list()
            max_build_numbers = list()
            max_build_strings = list()

            for entry in entries:
                entry = pkg_info(entry)
                if pkg_entry is None:
                    pkg_entry = entry

                version = parse(entry.get("version", ""))

                if version not in versions:
                    versions.append(version)
                    max_build_numbers.append(entry.get("build_number", 0))
                    max_build_strings.append(entry.get("build_string", ""))
                else:
                    version_idx = versions.index(version)
                    build_number = entry.get("build_number", 0)
                    if build_number > max_build_numbers[version_idx]:
                        max_build_numbers[version_idx] = build_number
                        max_build_strings[version_idx] = entry.get("build_string", "")

            sorted_versions_idx = sorted(range(len(versions)), key=versions.__getitem__)

            pkg_entry["version"] = [str(versions[i]) for i in sorted_versions_idx]
            pkg_entry["build_number"] = [max_build_numbers[i] for i in sorted_versions_idx]
            pkg_entry["build_string"] = [max_build_strings[i] for i in sorted_versions_idx]

            packages.append(pkg_entry)

        # Get channel short names
        channels = yield self.env_channels(env)

        return sorted(packages, key=lambda entry: entry.get("name"))

    @gen.coroutine
    def check_update(self, env: str, packages: typing.List[str]) -> dict:
        output = yield self._execute(
            CONDA_EXE + " update --dry-run -q --json -n " + env,
            *packages,
            options=list(self.options)
        )
        data = self.clean_conda_json(output)

        # Data structure in LINK
        #   List of dictionary. Example:
        # {
        #     "base_url": null,
        #     "build_number": 0,
        #     "build_string": "mkl",
        #     "channel": "defaults",
        #     "dist_name": "blas-1.0-mkl",
        #     "name": "blas",
        #     "platform": null,
        #     "version": "1.0"
        # }

        if "error" in data:
            # we didn't get back a list of packages, we got a dictionary with
            # error info
            return data
        elif "actions" in data:
            links = data["actions"].get("LINK", [])
            package_versions = [link for link in links]
            return {
                "updates": [pkg_info(pkg_version) for pkg_version in package_versions]
            }
        else:
            # no action plan returned means everything is already up to date
            return {"updates": []}

    @gen.coroutine
    def install_packages(self, env: str, packages: typing.List[str]) -> dict:
        output = yield self._execute(
            CONDA_EXE + " install -y -q --json -n " + env,
            *packages,
            options=list(self.options)
        )
        return self.clean_conda_json(output)

    @gen.coroutine
    def update_packages(self, env: str, packages: typing.List[str]) -> dict:
        output = yield self._execute(
            CONDA_EXE + " update -y -q --json -n " + env,
            *packages,
            options=list(self.options)
        )
        return self.clean_conda_json(output)

    @gen.coroutine
    def remove_packages(self, env: str, packages: typing.List[str]) -> dict:
        output = yield self._execute(
            CONDA_EXE + " remove -y -q --json -n " + env,
            *packages,
            options=list(self.options)
        )
        return self.clean_conda_json(output)

    @gen.coroutine
    def package_search(self, env: str, q: str) -> dict:
        # this method is slow
        output = yield self._execute(
            CONDA_EXE + " search --json -n " + env, q, options=list(self.options)
        )
        data = self.clean_conda_json(output)

        if "error" in data:
            # we didn't get back a list of packages, we got a dictionary with
            # error info
            return data

        packages = []

        for entries in data.values():
            max_version = None
            max_version_entry = None

            for entry in entries:
                version = parse(entry.get("version", ""))

                if max_version is None or version > max_version:
                    max_version = version
                    max_version_entry = entry

            packages.append(max_version_entry)
        return {"packages": sorted(packages, key=lambda entry: entry.get("name"))}
