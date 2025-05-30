#!/usr/bin/env python3
# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
################################################################################
"""
This is copied into the OSS-Fuzz container image and run there as part of the
instrumentation process.
"""

from collections.abc import MutableSequence, Sequence
import hashlib
import json
import os
import random
import subprocess
import sys
from typing import Any

_LLVM_READELF_PATH = "/usr/local/bin/llvm-readelf"


def execute(argv: Sequence[str]) -> None:
  argv[0] = os.path.join("/usr/local/bin/", os.path.basename(argv[0]))
  print("About to execute...", argv)
  os.execv(argv[0], argv)


def run(argv: Sequence[str]) -> None:
  argv[0] = os.path.join("/usr/local/bin/", os.path.basename(argv[0]))
  print("About to run...", argv)
  ret = subprocess.run(argv, check=False)
  if ret.returncode != 0:
    sys.exit(ret.returncode)


def sha256(file: str) -> str:
  hash_value = hashlib.sha256()
  with open(file, "rb") as f:
    # python 3.11 is too new, this doesn't work on the oss-fuzz image.
    # hashlib.file_digest(f, lambda: hash_value)
    for chunk in iter(lambda: f.read(4096), b""):
      hash_value.update(chunk)
  return hash_value.hexdigest()


def _get_build_id_from_elf_notes(contents: bytes) -> str | None:
  """Extracts the build id from the ELF notes of a binary.

  The ELF notes are obtained with
    `llvm-readelf --notes --elf-output-style=JSON`.

  Args:
    contents: The contents of the ELF notes, as a JSON string.

  Returns:
    The build id, or None if it could not be found.
  """

  elf_data = json.loads(contents)
  assert elf_data

  for file_info in elf_data:
    for note_entry in file_info["Notes"]:
      note_section = note_entry["NoteSection"]
      if note_section["Name"] == ".note.gnu.build-id":
        note_details = note_section["Note"]
        if "Build ID" in note_details:
          return note_details["Build ID"]
  return None


def get_build_id(elf_file: str) -> str:
  """This invokes llvm-readelf to get the build ID of the given ELF file."""

  # Example output of llvm-readelf JSON output:
  # [
  #   {
  #     "FileSummary": {
  #       "File": "/out/iccprofile_info",
  #       "Format": "elf64-x86-64",
  #       "Arch": "x86_64",
  #       "AddressSize": "64bit",
  #       "LoadName": "<Not found>",
  #     },
  #     "Notes": [
  #       {
  #         "NoteSection": {
  #           "Name": ".note.ABI-tag",
  #           "Offset": 764,
  #           "Size": 32,
  #           "Note": {
  #             "Owner": "GNU",
  #             "Data size": 16,
  #             "Type": "NT_GNU_ABI_TAG (ABI version tag)",
  #             "OS": "Linux",
  #             "ABI": "3.2.0",
  #           },
  #         }
  #       },
  #       {
  #         "NoteSection": {
  #           "Name": ".note.gnu.build-id",
  #           "Offset": 796,
  #           "Size": 24,
  #           "Note": {
  #             "Owner": "GNU",
  #             "Data size": 8,
  #             "Type": "NT_GNU_BUILD_ID (unique build ID bitstring)",
  #             "Build ID": "a03df61c5b0c26f3",
  #           },
  #         }
  #       },
  #     ],
  #   }
  # ]

  ret = subprocess.run(
      [
          _LLVM_READELF_PATH,
          "--notes",
          "--elf-output-style=JSON",
          elf_file,
      ],
      capture_output=True,
      check=True,
  )
  if ret.returncode != 0:
    sys.exit(ret.returncode)

  return _get_build_id_from_elf_notes(ret.stdout)


def get_flag_value(argv: Sequence[str], flag: str) -> str:
  for i in range(len(argv) - 1):
    if argv[i] == flag:
      return argv[i + 1]
    elif flag == "-o" and argv[i].startswith(flag):
      return argv[i][2:]
  return None


def remove_flag_and_value(
    argv: Sequence[str], flag: str
) -> MutableSequence[str]:
  for i in range(len(argv) - 1):
    if argv[i] == flag:
      return argv[:i] + argv[i + 2 :]
    elif flag == "-o" and argv[i].startswith(flag):
      return argv[:i] + argv[i + 2 :]

  return None


def parse_dependency_file(
    file_path: str, output_file: str, ignored_deps: frozenset[str]
) -> Sequence[str]:
  """Parses the dependency file generated by the linker."""
  output_file = os.path.realpath(output_file)
  output_file_line = f"{output_file}: \\"
  with open(file_path, "r") as f:
    lines = [line.strip() for line in f]
  assert output_file_line.endswith(
      lines[0]
  ), f"{lines[0]} is not a suffix of {output_file_line}"

  deps = []
  ignored_dep_paths = ["/usr", "/clang", "/lib"]
  for line in lines[1:]:
    if not line:
      break
    if line.endswith(" \\"):
      line = line[:-2]
    dep = os.path.realpath(line)
    # We don"t care about system-wide dependencies.
    if any([True for p in ignored_dep_paths if dep.startswith(p)]):
      continue
    if dep in ignored_deps:
      continue
    deps.append(dep)
  return deps


def files_by_creation_time(folder_path: str) -> Sequence[str]:
  files = [
      os.path.join(folder_path, file)
      for file in os.listdir(folder_path)
      if os.path.isfile(os.path.join(folder_path, file))
  ]
  files.sort(key=os.path.getctime)
  return files


def read_cdb_fragments(cdb_path: str) -> Any:
  """Iterates through the CDB fragments to reconstruct the compile commands."""
  files = files_by_creation_time(cdb_path)
  contents = []
  for file in files:
    # Don't read previously generated linker commands files.
    if file.endswith("_linker_commands.json"):
      continue
    if not file.endswith(".json"):
      continue
    with open(file, "rt") as f:
      data = f.read()
      assert data.endswith(
          ",\n"
      ), f"Invalid compile commands file {file}: {data}"
      contents.append(data[:-2])
  contents = ",\n".join(contents)
  contents = "[" + contents + "]"
  return json.loads(contents)


def main(argv: Sequence[str]) -> None:
  fuzzer_engine = os.getenv("LIB_FUZZING_ENGINE")

  # If we are not linking the fuzzing engine, execute normally.
  if not fuzzer_engine or (
      fuzzer_engine not in argv and "-lFuzzingEngine" not in argv
  ):
    execute(argv)

  # We are linking, collect the relevant flags and dependencies.
  output_file = get_flag_value(argv, "-o")
  assert output_file, f"Missing output file: {argv}"

  cdb_path = get_flag_value(argv, "-gen-cdb-fragment-path")
  assert cdb_path, f"Missing Compile Directory Path: {argv}"

  argv = remove_flag_and_value(argv, "-gen-cdb-fragment-path")

  # We can now run the linker and look at the output of some files.
  dependency_file = os.path.join(
      cdb_path, os.path.basename(output_file) + ".deps"
  )
  why_extract_file = os.path.join(
      cdb_path, os.path.basename(output_file) + ".why_extract"
  )
  argv.append("-fuse-ld=lld")
  argv.append(f"-Wl,--dependency-file={dependency_file}")
  argv.append(f"-Wl,--why-extract={why_extract_file}")
  argv.append("-Wl,--build-id")
  run(argv)

  build_id = get_build_id(output_file)
  assert build_id is not None

  output_hash = sha256(output_file)

  with open("/opt/indexer/ignored_deps.json") as f:
    ignored_deps = frozenset(json.load(f)["deps"])

  deps = parse_dependency_file(dependency_file, output_file, ignored_deps)
  obj_deps = [dep for dep in deps if dep.endswith(".o")]
  ar_deps = [dep for dep in deps if dep.endswith(".a") and dep != fuzzer_engine]
  archive_deps = []
  for archive in ar_deps:
    res = subprocess.run(["ar", "-t", archive], capture_output=True, check=True)
    archive_deps += [dep.decode() for dep in res.stdout.splitlines()]

  cdb = read_cdb_fragments(cdb_path)
  commands = {}
  for dep in obj_deps:
    print(f"Looking for dep {dep}")
    if dep == fuzzer_engine:
      continue
    dep = os.path.realpath(dep)
    for command in cdb:
      command_path = os.path.realpath(
          os.path.join(command["directory"], command["output"])
      )
      if command_path == dep:
        commands[dep] = command

    if dep not in commands:
      print(f"{dep} NOT FOUND")

  for archive_dep in archive_deps:
    # We don't have the full path of the archive dep, so we will only look at
    # the basename.
    for command in cdb:
      if os.path.basename(command["output"]) == archive_dep:
        commands[archive_dep] = command

    if archive_dep not in commands:
      print(f"{archive_dep} NOT FOUND")

  linker_commands = {
      "output": output_file,
      "directory": os.getcwd(),
      "deps": obj_deps + archive_deps,
      "args": argv,
      "sha256": output_hash,
      "gnu_build_id": build_id,
      "compile_commands": list(commands.values()),
  }
  linker_commands = json.dumps(linker_commands)
  commands_path = os.path.join(cdb_path, build_id + "_linker_commands.json")
  with open(commands_path, "w") as f:
    f.write(linker_commands)


if __name__ == "__main__":
  main(sys.argv)
