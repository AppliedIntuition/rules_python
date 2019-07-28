# Copyright 2017 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The whl modules defines classes for interacting with Python packages."""

import argparse
import json
import os
import pkg_resources
import re
import zipfile

def _bfs_walk(dirname): 
    dirs = [dirname] 
    while len(dirs): 
        parent = dirs.pop(0)
        children = [os.path.join(parent, fname) for fname in os.listdir(parent)]
        children = [d for d in children if os.path.isdir(d)]
        dirs.extend(children)
        yield parent

class Wheel(object):

  def __init__(self, path):
    self._path = path

  def path(self):
    return self._path

  def basename(self):
    return os.path.basename(self.path())

  def distribution(self):
    # See https://www.python.org/dev/peps/pep-0427/#file-name-convention
    parts = self.basename().split('-')
    return parts[0]

  def version(self):
    # See https://www.python.org/dev/peps/pep-0427/#file-name-convention
    parts = self.basename().split('-')
    return parts[1]

  def repository_name(self):
    # Returns the canonical name of the Bazel repository for this package.
    canonical = 'pypi__{}_{}'.format(self.distribution(), self.version())
    # Escape any illegal characters with underscore.
    return re.sub('[-.+]', '_', canonical)

  def _dist_info(self):
    # Return the name of the dist-info directory within the .whl file.
    # e.g. google_cloud-0.27.0-py2.py3-none-any.whl ->
    #      google_cloud-0.27.0.dist-info
    return '{}-{}.dist-info'.format(self.distribution(), self.version())

  def metadata(self):
    # Extract the structured data from metadata.json in the WHL's dist-info
    # directory.
    with zipfile.ZipFile(self.path(), 'r') as whl:
      # first check for metadata.json
      try:
        with whl.open(self._dist_info() + '/metadata.json') as f:
          return json.loads(f.read().decode("utf-8"))
      except KeyError:
          pass
      # fall back to METADATA file (https://www.python.org/dev/peps/pep-0427/)
      with whl.open(self._dist_info() + '/METADATA') as f:
        return self._parse_metadata(f.read().decode("utf-8"))

  def name(self):
    return self.metadata().get('name')

  def dependencies(self, extra=None):
    """Access the dependencies of this Wheel.

    Args:
      extra: if specified, include the additional dependencies
            of the named "extra".

    Yields:
      the names of requirements from the metadata.json
    """
    # TODO(mattmoor): Is there a schema to follow for this?
    run_requires = self.metadata().get('run_requires', [])
    for requirement in run_requires:
      if requirement.get('extra') != extra:
        # Match the requirements for the extra we're looking for.
        continue
      marker = requirement.get('environment')
      if marker and not pkg_resources.evaluate_marker(marker):
        # The current environment does not match the provided PEP 508 marker,
        # so ignore this requirement.
        continue
      requires = requirement.get('requires', [])
      for entry in requires:
        # Strip off any trailing versioning data.
        parts = re.split('[ ><=()]', entry)
        yield parts[0]

  def extras(self):
    return self.metadata().get('extras', [])

  def expand(self, directory):
    with zipfile.ZipFile(self.path(), 'r') as whl:
      whl.extractall(directory)
      names = set(whl.namelist())

    # Workaround for https://github.com/bazelbuild/rules_python/issues/14
    for initpy in self.get_init_paths(names):
      with open(os.path.join(directory, initpy), 'w') as f:
        f.write(INITPY_CONTENTS)

  def get_init_paths(self, names):
    # Overwrite __init__.py in these directories.
    # (required as googleapis-common-protos has an empty __init__.py, which
    # blocks google.api.core from google-cloud-core)
    NAMESPACES = ["ruamelj]

    # Find package directories without __init__.py, or where the __init__.py
    # must be overwritten to create a working namespace. This is based on
    # Bazel's PythonUtils.getInitPyFiles().
    init_paths = set()
    for n in names:
      if os.path.splitext(n)[1] not in ['.so', '.py', '.pyc']:
        continue
      while os.path.sep in n:
        n = os.path.dirname(n)
        initpy = os.path.join(n, '__init__.py')
        initpyc = os.path.join(n, '__init__.pyc')
        if (initpy in names or initpyc in names) and n not in NAMESPACES:
          continue
        init_paths.add(initpy)

    return init_paths

  # _parse_metadata parses METADATA files according to https://www.python.org/dev/peps/pep-0314/
  def _parse_metadata(self, content):
    # TODO: handle fields other than just name
    name_pattern = re.compile('Name: (.*)')
    return { 'name': name_pattern.search(content).group(1) }

  def _find_package_path(self, directory): 
    """Finds the path to the package within the extracted .whl. 

    This is a patch to fix this issue: 
        https://github.com/bazelbuild/rules_python/issues/189
        https://github.com/bazelbuild/rules_python/issues/92

    Bazel assumes the package is located in the top-level directory of the 
    extracted whl. For instance, the structure of the matplotlib .whl is: 
        extracted_whl 
            matplotlib
                __init__.py
                <src files...>
            <metadata files...>

    This patch lets Bazel handle packages that do not support this convention, 
    like tensorflow: 
        extracted_whl
            tensorflow-<version_num>.data
                purelib
                    tensorflow
                        __init__.py
                        <src files...>
            <metadata files...>
    
    (added by anelise)
    """
    name = self.name()

    # search the directory structure for the right folder
    for dirname in _bfs_walk(directory): 
        if os.path.exists(os.path.join(dirname, name)): 
            return dirname

    # fall back to top-level
    return "."

parser = argparse.ArgumentParser(
    description='Unpack a WHL file as a py_library.')

parser.add_argument('--whl', action='store',
                    help=('The .whl file we are expanding.'))

parser.add_argument('--requirements', action='store',
                    help='The pip_import from which to draw dependencies.')

parser.add_argument('--directory', action='store', default='.',
                    help='The directory into which to expand things.')

parser.add_argument('--extras', action='append',
                    help='The set of extras for which to generate library targets.')

def main():
  args = parser.parse_args()
  whl = Wheel(args.whl)

  # Extract the files into the current directory
  whl.expand(args.directory)

  import_path=whl._find_package_path(args.directory)

  with open(os.path.join(args.directory, 'BUILD'), 'w') as f:
    f.write("""
package(default_visibility = ["//visibility:public"])

load("{requirements}", "requirement")

py_library(
    name = "pkg",
    srcs = glob(["**/*.py"]),
    data = glob(["**/*"], exclude=["**/*.py", "**/* *", "BUILD", "WORKSPACE"]),
    imports = ["{import_path}"],
    deps = [{dependencies}],
)
{extras}""".format(
  requirements=args.requirements,
  import_path=import_path,
  dependencies=','.join([
    'requirement("%s")' % d
    for d in whl.dependencies()
  ]),
  extras='\n\n'.join([
    """py_library(
    name = "{extra}",
    deps = [
        ":pkg",{deps}
    ],
)""".format(extra=extra,
            deps=','.join([
                'requirement("%s")' % dep
                for dep in whl.dependencies(extra)
            ]))
    for extra in args.extras or []
  ])))

if __name__ == '__main__':
  main()
