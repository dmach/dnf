# Copyright (C) 2017  Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.


from __future__ import print_function

import errno
import fnmatch
import gzip
import os
from collections import OrderedDict

import hawkey
import modulemd
import smartcols

from dnf.callback import TransactionProgress, TRANS_POST, PKG_VERIFY
from dnf.conf import ModuleConf
from dnf.conf.read import ModuleReader, ModuleDefaultsReader
from dnf.exceptions import Error
from dnf.i18n import _
from dnf.pycomp import ConfigParser
from dnf.subject import Subject
from dnf.util import logger


LOAD_CACHE_ERR = 1
MISSING_YAML_ERR = 2
NO_METADATA_ERR = 3
NO_MODULE_OR_STREAM_ERR = 4
NO_MODULE_ERR = 5
NO_PROFILE_ERR = 6
NO_STREAM_ERR = 7
NO_ACTIVE_STREAM_ERR = 8
STREAM_NOT_ENABLED_ERR = 9
DIFFERENT_STREAM_INFO = 10
INVALID_MODULE_ERR = 11
LOWER_VERSION_INFO = 12
NOTHING_TO_SHOW = 13
PARSING_ERR = 14
PROFILE_NOT_INSTALLED = 15


module_errors = {
    LOAD_CACHE_ERR: "Cannot load from cache dir: {}",
    MISSING_YAML_ERR: "Missing file *modules.yaml in metadata cache dir: {}",
    NO_METADATA_ERR: "No available metadata for module: {}",
    NO_MODULE_OR_STREAM_ERR: "No such module: {} or active stream (enable a stream first)",
    NO_MODULE_ERR: "No such module: {}",
    NO_PROFILE_ERR: "No such profile: {}. Possible profiles: {}",
    NO_STREAM_ERR: "No such stream {} in {}",
    NO_ACTIVE_STREAM_ERR: "No active stream for module: {}",
    STREAM_NOT_ENABLED_ERR: "Stream not enabled. Skipping {}",
    DIFFERENT_STREAM_INFO: "Enabling different stream for {}",
    INVALID_MODULE_ERR: "Not a valid module: {}",
    LOWER_VERSION_INFO: "Using lower version due to missing profile in latest version",
    NOTHING_TO_SHOW: "Nothing to show",
    PARSING_ERR: "Probable parsing problem of {}, try specifying MODULE-STREAM-VERSION",
    PROFILE_NOT_INSTALLED: "Profile not installed: {}",
}


class RepoModuleVersion(object):
    def __init__(self, module_metadata, base, repo):
        self.module_metadata = module_metadata
        self.repo = repo
        self.base = base
        self.parent = None

    def __lt__(self, other):
        # for finding latest
        assert self.full_stream == other.full_stream
        return self.module_metadata.version < other.module_metadata.version

    def install(self, profile):
        if profile not in self.profiles:
            raise Error(module_errors[NO_PROFILE_ERR].format(profile, self.profiles))

        self.parent.parent.installed_repo_module_version = self
        self.parent.parent.installed_profiles.append(profile)

        for single_nevra in self.profile_nevra(profile):
            self.base.install(single_nevra, reponame=self.repo.id, forms=hawkey.FORM_NEVR)

        repo_module = self.parent.parent
        repo_module.parent.transaction_callback.repo_modules.append(repo_module)

    def upgrade(self, profiles):
        self.parent.parent.installed_repo_module_version = self
        for profile in profiles:
            if profile not in self.profiles:
                raise Error(module_errors[NO_PROFILE_ERR].format(profile, self.profiles))

            for single_nevra in self.profile_nevra(profile):
                self.base.upgrade(single_nevra, reponame=self.repo.id)

        repo_module = self.parent.parent
        repo_module.parent.transaction_callback.repo_modules.append(repo_module)

    def nevra(self):
        result = self.module_metadata.artifacts.rpms
        # HACK: remove epoch to make filter(nevra=...) work
        result = [i.replace("0:", "") for i in result]
        return result

    def rpms(self, profile):
        return self.module_metadata.profiles[profile].rpms

    def profile_nevra(self, profile):
        result = set()
        rpms = set(self.rpms(profile))
        for nevra in self.nevra():
            subj = Subject(nevra)
            nevra_obj = list(subj.get_nevra_possibilities(hawkey.FORM_NEVRA))[0]
            if nevra_obj.name not in rpms:
                continue
            result.add("{}-{}".format(nevra_obj.name, nevra_obj.evr()))
        return result

    @property
    def version(self):
        return self.module_metadata.version

    @property
    def full_version(self):
        return "%s-%s-%s" % (
            self.module_metadata.name, self.module_metadata.stream, self.module_metadata.version)

    @property
    def stream(self):
        return self.module_metadata.stream

    @property
    def full_stream(self):
        return "%s-%s" % (self.module_metadata.name, self.module_metadata.stream)

    @property
    def name(self):
        return self.module_metadata.name

    @property
    def profiles(self):
        return sorted(self.module_metadata.profiles)


class RepoModuleStream(OrderedDict):
    def __init__(self):
        super(RepoModuleStream, self).__init__()

        self.stream = None
        self.parent = None

    def add(self, repo_module_version):
        self[repo_module_version.version] = repo_module_version
        repo_module_version.parent = self

        self.stream = repo_module_version.stream

    def latest(self):
        return max(self.values())


class RepoModule(OrderedDict):
    def __init__(self):
        super(RepoModule, self).__init__()

        self.conf = None
        self.defaults = None
        self.name = None
        self.parent = None
        self.installed_profiles = []
        self.installed_version = None

    def add(self, repo_module_version):
        module_stream = self.setdefault(repo_module_version.stream, RepoModuleStream())
        module_stream.add(repo_module_version)
        module_stream.parent = self

        self.name = repo_module_version.name

    def enable(self, stream, assumeyes, assumeno):
        if stream not in self:
            raise Error(module_errors[NO_STREAM_ERR].format(stream, self.name))

        if self.conf is None:
            self.conf = ModuleConf(section=self.name, parser=ConfigParser())
            self.conf.name = self.name

        if self.conf.stream is not None and str(self.conf.stream) != str(stream) and not assumeyes:
            logger.info(module_errors[DIFFERENT_STREAM_INFO].format(self.name))
            if not assumeno and self.parent.base.output.userconfirm():
                self.enable(stream, True, assumeno)
            else:
                logger.info(module_errors[STREAM_NOT_ENABLED_ERR].format(stream))

        self.conf.stream = stream
        self.conf.enabled = True
        self.write_conf_to_file()

    def disable(self):
        if self.conf is None:
            self.conf = ModuleConf(section=self.name, parser=ConfigParser())
            self.conf.name = self.name

        self.conf.enabled = False
        self.write_conf_to_file()

    def write_conf_to_file(self):
        output_file = os.path.join(self.parent.get_modules_dir(), "%s.module" % self.conf.name)

        with open(output_file, "w") as config_file:
            self.conf._write(config_file)


class RepoModuleDict(OrderedDict):
    def __init__(self, base):
        super(RepoModuleDict, self).__init__()

        self.base = base
        self.transaction_callback = ModuleTransactionProgress()

    def add(self, repo_module_version):
        module = self.setdefault(repo_module_version.name, RepoModule())
        module.add(repo_module_version)
        module.parent = self

    def find_module_version(self, name, stream=None, version=None, arch=None):
        try:
            repo_module = self[name]

            # if stream is not specified:
            # - use the enabled stream
            # - pick the default from system profile
            # - return None if no suitable stream is found
            if not stream:
                if repo_module.conf and repo_module.conf.enabled:
                    stream = repo_module.conf.stream

            if not stream:
                # TODO: read default stream from system profile
                stream = "f26"
                if name == "httpd":
                    stream = "2.4"

            if not stream:
                return None

            repo_module_stream = repo_module[stream]

            if repo_module.conf and repo_module.conf.locked and repo_module.conf.version is not None:
                # if module version is locked, ignore user input
                # TODO: print warning if locked version != latest or provided
                repo_module_version = repo_module_stream[repo_module.conf.version]
            elif version:
                repo_module_version = repo_module_stream[version]
            else:
                # if version is not specified, pick the latest
                repo_module_version = repo_module_stream.latest()

            # TODO: arch
            # TODO: platform module

            return repo_module_version
        except KeyError:
            pass
        return None

    def enable(self, pkg_spec, assumeyes, assumeno=False):
        subj = ModuleSubject(pkg_spec)
        module_version, nsvap = subj.find_module_version(self)

        if not module_version:
            logger.error(module_errors[NO_MODULE_ERR].format(pkg_spec))
            return

        self[module_version.name].enable(module_version.stream, assumeyes, assumeno)

    def disable(self, pkg_spec):
        subj = ModuleSubject(pkg_spec)
        module_version, nsvap = subj.find_module_version(self)

        if module_version:
            repo_module = module_version.parent.parent
            repo_module.disable()
            return

        # if lookup by pkg_spec failed, try disabling module by name
        try:
            self[pkg_spec].disable()
        except KeyError:
            logger.warning(module_errors[NO_MODULE_ERR].format(pkg_spec))

    def install(self, pkg_specs, autoenable=False):
        for pkg_spec in pkg_specs:
            subj = ModuleSubject(pkg_spec)
            module_version, nsvap = subj.find_module_version(self)

            if not module_version:
                logger.error(module_errors[NO_MODULE_ERR].format(pkg_spec))
                continue

            if autoenable:
                # TODO: nsvap depends on enabled module :(
                self.enable(nsvap.name, True)

            module_version.install(nsvap.profile)

    def upgrade(self, pkg_specs):
        for pkg_spec in pkg_specs:
            subj = ModuleSubject(pkg_spec)
            module_version, nsvap = subj.find_module_version(self)

            if not module_version:
                logger.error(module_errors[NO_MODULE_ERR].format(pkg_spec))
                continue

            conf = self[nsvap.name].conf
            if conf:
                installed_profiles = conf.profiles
            else:
                installed_profiles = []
            if nsvap.profile:
                if nsvap.profile not in installed_profiles:
                    logger.error(module_errors[PROFILE_NOT_INSTALLED].format(pkg_spec))
                    continue
                profiles = [nsvap.profile]
            else:
                profiles = installed_profiles

            module_version.upgrade(profiles)

    def upgrade_all(self):
        modules = []
        for module_name, repo_module in self.items():
            if not repo_module.conf:
                continue
            if not repo_module.conf.enabled:
                continue
            modules.append(module_name)
        modules.sort()
        self.upgrade(modules)

    def load_modules(self, repo):
        loader = ModuleMetadataLoader(repo)
        for module_metadata in loader.load():
            module_version = RepoModuleVersion(module_metadata, base=self.base, repo=repo)

            self.add(module_version)

    def read_all_modules(self):
        module_reader = ModuleReader(self.get_modules_dir())
        for conf in module_reader:
            repo_module = self.setdefault(conf.name, RepoModule())
            repo_module.conf = conf
            repo_module.name = conf.name
            repo_module.parent = self

    def read_all_module_defaults(self):
        defaults_reader = ModuleDefaultsReader(self.base.conf.moduledefaultsdir)
        for conf in defaults_reader:
            try:
                self[conf.name].defaults = conf
            except KeyError:
                logger.debug("No module named {}, skipping.".format(conf.name))

    def get_modules_dir(self):
        modules_dir = os.path.join(self.base.conf.installroot, self.base.conf.modulesdir.lstrip("/"))

        if not os.path.exists(modules_dir):
            self.create_dir(modules_dir)

        return modules_dir

    def get_module_defaults_dir(self):
        return self.base.conf.moduledefaultsdir

    @staticmethod
    def create_dir(output_file):
        oumask = os.umask(0o22)
        try:
            os.makedirs(output_file)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
        finally:
            os.umask(oumask)

    def get_full_description(self, pkg_spec):
        subj = ModuleSubject(pkg_spec)
        module_version, nsvap = subj.find_module_version(self)
        return module_version.module_metadata.dumps().rstrip("\n")

    def get_brief_description_all(self, module_n):
        return self.get_brief_description_by_name(module_n, [stream for module in self.values()
                                                             for stream in module.values()])

    def get_brief_description_enabled(self, module_n):
        return self.get_brief_description_by_name(module_n,
                                                  [stream for module in self.values()
                                                   for stream in module.values()
                                                   if module.conf is not None and
                                                   module.conf.enabled and
                                                   module.conf.stream == stream.stream])

    def get_brief_description_disabled(self, module_n):
        return self.get_brief_description_by_name(module_n,
                                                  [stream for module in self.values()
                                                   for stream in module.values()
                                                   if module.conf is None or
                                                   not module.conf.enabled])

    def get_brief_description_installed(self, module_n):
        return self.get_brief_description_by_name(module_n,
                                                  [stream for module in self.values()
                                                   for stream in module.values()
                                                   if module.conf is not None and
                                                   module.conf.enabled and
                                                   module.conf.version],
                                                  True)

    def get_brief_description_by_name(self, module_n, repo_module_streams, only_installed=False):
        if module_n is None or not module_n:
            return self._get_brief_description(repo_module_streams, only_installed)
        else:
            return self._get_brief_description([stream for stream in repo_module_streams
                                                if fnmatch.fnmatch(stream.parent.name,
                                                                   module_n[0])],
                                               only_installed)

    @staticmethod
    def _get_brief_description(repo_module_streams, only_installed=False):
        repo_module_versions = [repo_module_version
                                for repo_module_stream in repo_module_streams
                                for repo_module_version in repo_module_stream.values()]

        if only_installed:
            only_installed_versions = []
            for i in repo_module_versions:
                conf = i.parent.parent.conf
                if int(conf.version) == int(i.version) and conf.stream == i.stream:
                    only_installed_versions.append(i)
            repo_module_versions = only_installed_versions

        if not repo_module_versions:
            return module_errors[NOTHING_TO_SHOW]

        table = smartcols.Table()
        table.maxout = True

        column_name = table.new_column("Name")
        column_stream = table.new_column("Stream")
        column_version = table.new_column("Version")
        column_repo = table.new_column("Repo")
        column_installed = table.new_column("Installed")
        column_info = table.new_column("Info")
        column_info.right = True

        for i in sorted(repo_module_versions, key=lambda data: data.name):
            line = table.new_line()
            conf = i.parent.parent.conf
            data = i.module_metadata
            line[column_name] = data.name
            line[column_stream] = data.stream
            line[column_version] = str(data.version)
            line[column_repo] = i.repo.id
            if conf and conf.version == i.version and conf.stream == i.stream:
                line[column_installed] = ", ".join(conf.profiles)
            line[column_info] = data.summary

        return table


class ModuleMetadataLoader(object):
    def __init__(self, repo=None):
        self.repo = repo

    def load(self):
        if self.repo is None:
            raise Error(module_errors[LOAD_CACHE_ERR].format(self.repo))

        content_of_cachedir = os.listdir(self.repo._cachedir + "/repodata")
        modules_yaml_gz = list(filter(lambda repodata_file: 'modules' in repodata_file,
                                      content_of_cachedir))

        if not modules_yaml_gz:
            raise Error(module_errors[MISSING_YAML_ERR].format(self.repo._cachedir))
        modules_yaml_gz = "{}/repodata/{}".format(self.repo._cachedir, modules_yaml_gz[0])

        with gzip.open(modules_yaml_gz, "r") as extracted_modules_yaml_gz:
            modules_yaml = extracted_modules_yaml_gz.read()

        return modulemd.loads_all(modules_yaml)


class ModuleTransactionProgress(TransactionProgress):
    def __init__(self):
        super(ModuleTransactionProgress, self).__init__()
        self.repo_modules = []
        self.saved = False

    def progress(self, package, action, ti_done, ti_total, ts_done, ts_total):
        if not self.saved and (action == TRANS_POST or action == PKG_VERIFY):
            self.saved = True
            for repo_module in self.repo_modules:
                conf = repo_module.conf
                conf.enabled = True
                conf.version = repo_module.installed_repo_module_version.version

                profiles = repo_module.installed_profiles
                profiles.extend(conf.profiles)
                conf.profiles = sorted(set(profiles))

                repo_module.write_conf_to_file()


NSVAP_FIELDS = ["name", "stream", "version", "arch", "profile"]


class NSVAP(object):
    """
    Represents module name, stream, version, arch, profile.
    Returned by ModuleSubject.
    """

    def __init__(self, name, stream, version, arch, profile):
        self.name = name
        self.stream = stream
        self.version = version is not None and int(version) or None
        self.arch = arch
        self.profile = profile

    def __repr__(self):
        values = [getattr(self, i) for i in NSVAP_FIELDS]
        items = [(field, value) for field, value in zip(NSVAP_FIELDS, values) if value is not None]
        items_str = ", ".join(["{}={}".format(field, value) for field, value in items])
        return "<NSVAP: {}>".format(items_str)

    def __eq__(self, other):
        result = True
        for field in NSVAP_FIELDS:
            value_self = getattr(self, field)
            value_other = getattr(other, field)
            result &= value_self == value_other
        return result


class ModuleSubject(object):
    """
    Find matching modules for given user input (pkg_spec).
    """

    def __init__(self, pkg_spec):
        self.pkg_spec = pkg_spec

    def get_nsvap_possibilities(self, forms=None):
        # split profile and then parse module NSVA as it was rpm NVRA

        if "/" in self.pkg_spec:
            nsva, profile = self.pkg_spec.rsplit("/", 1)
            if not profile.strip():
                profile = None
        else:
            nsva, profile = self.pkg_spec, None

        subj = hawkey.Subject(nsva)
        kwargs = {}
        if forms:
            kwargs["form"] = forms
        possibilities = subj.nevra_possibilities(**kwargs)

        result = []
        for i in possibilities:
            try:
                if i.release is not None:
                    i.release = str(int(i.release))
            except ValueError:
                # module version has to be integer
                # if it is not -> invalid possibility -> skip
                continue
            args = {
                "name": i.name,
                "stream": i.version,
                "version": i.release and int(i.release) or None,
                "arch": i.arch,
                "profile": profile
            }
            result.append(NSVAP(**args))
        return result

    def find_module_version(self, repo_module_dict):
        """
        Find module that matches self.pkg_spec in given repo_module_dict.
        Return (RepoModuleVersion, NSVAP).
        """

        result = (None, None)
        for nsvap in self.get_nsvap_possibilities():
            module_version = repo_module_dict.find_module_version(nsvap.name, nsvap.stream, nsvap.version, nsvap.arch)
            if module_version:
                result = (module_version, nsvap)
                break
        return result
