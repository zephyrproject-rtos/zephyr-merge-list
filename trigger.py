#!/usr/bin/env python3

# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Triggers a GitHub workflow if no run is currently active or recently completed."""

import argparse
import datetime
import github
import pathlib
import sys

DEFAULT_TOKEN_PATH = pathlib.Path.home() / ".github-token-merge-list"
DEFAULT_COOLDOWN_SECONDS = 10 * 60
UTC = datetime.timezone.utc


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("-o", "--org", default="zephyrproject-rtos",
                        help="Target Github organisation")
    parser.add_argument("-r", "--repo", default="zephyr-merge-list",
                        help="Target Github repository")
    parser.add_argument("-w", "--workflow", default="update.yaml",
                        help="Workflow file name")
    parser.add_argument("-t", "--token", default=DEFAULT_TOKEN_PATH,
                        type=pathlib.Path,
                        help="GitHub token file path")
    parser.add_argument("-c", "--cooldown",
                        default=DEFAULT_COOLDOWN_SECONDS,
                        type=int,
                        help="Cooldown time in seconds before triggering a new run")

    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)

    token = args.token.read_text().strip()
    auth = github.Auth.Token(token)
    gh = github.Github(auth=auth)

    repo = gh.get_repo(f"{args.org}/{args.repo}")

    workflow = repo.get_workflow(args.workflow)

    runs = workflow.get_runs(branch="main")

    in_progress = False
    latest_timestamp = None
    for run in runs[:10]:
        print(f"run: {run.created_at} {run.updated_at} {run.name} {run.status} {run.conclusion}")

        if run.status == "completed":
            if latest_timestamp is None or run.updated_at > latest_timestamp:
                latest_timestamp = run.updated_at
        else:
            in_progress = True

    if not latest_timestamp:
        print("skip: no completed run")
        return 0

    time_since_completed = datetime.datetime.now(UTC) - latest_timestamp
    print(f"last update: {time_since_completed}")

    if in_progress:
        print("skip: in progress")
        return 0

    if time_since_completed.total_seconds() < args.cooldown:
        print("skip: recent")
        return 0

    status = workflow.create_dispatch(ref="main")

    print(f"trigger workflow: {status}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
