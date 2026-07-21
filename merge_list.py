#!/usr/bin/env python3

# Copyright 2024 Google LLC
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass, field
import argparse
import datetime
import github
import html
import json
import os
import re
import sys
import tabulate
import gzip

PER_PAGE = 100

HTML_OUT = "public/index.html"
HTML_PRE = "index.html.pre"
HTML_POST = "index.html.post"

PR_JSON_OUT = "public/pr.json.gz"

CI_JSON_OUT = "public/ci.json"
CI_IGNORE = ["Code Coverage with codecov"]

UTC = datetime.timezone.utc

CI_RUN_NAME = "Run tests with twister"
CI_RUN_MAX_AGE_DAYS = 31

HOTFIX_LABEL = "Hotfix"
TRIVIAL_LABEL = "Trivial"
OVERRIDE_REQUIRED_LABEL = "Override Required"

REVIEW_WINDOW_BIZ_HOURS = 48
REVIEW_WINDOW_TRIVIAL_HOURS = 4


@dataclass
class PRData:
    pr_raw: dict
    pr: github.PullRequest
    assignee: str = field(default=None)
    approvers: set = field(default=None)
    time: bool = field(default=False)
    time_left: int = field(default=None)
    rebaseable: bool = field(default=False)
    hotfix: bool = field(default=False)
    trivial: bool = field(default=False)
    override_required: bool = field(default=False)
    dnm: bool = field(default=False)
    ci_age_days: int = field(default=None)
    ci_run_recent: bool = field(default=False)
    dismissed: bool = field(default=False)
    debug: list = field(default=None)


def print_rate_limit(gh, org):
    response = gh.get_organization(org)
    for header, value in response.raw_headers.items():
        if header.startswith("x-ratelimit"):
            print(f"{header}: {value}")


def calc_biz_hours(ref, delta):
    biz_hours = 0

    for hours in range(int(delta.total_seconds() / 3600)):
        date = ref + datetime.timedelta(hours=hours+1)
        if date.weekday() < 5:
            biz_hours += 1

    return biz_hours


def set_ci_age_data(repo, data):
    pr = data.pr

    pr_age = datetime.datetime.now(UTC) - pr.created_at
    if pr_age < datetime.timedelta(days=CI_RUN_MAX_AGE_DAYS):
        print(f"ci age: skip {pr.number}")
        data.ci_run_recent = True
        return

    runs = repo.get_workflow_runs(head_sha=pr.head.sha)

    target_run = None
    for run in runs:
        if run.name == CI_RUN_NAME:
            target_run = run
            break

    if not target_run:
        return

    run_age = datetime.datetime.now(UTC) - run.run_started_at
    print(f"ci age: {pr.number}: {run_age} {run.html_url}")
    if run_age > datetime.timedelta(days=CI_RUN_MAX_AGE_DAYS):
        data.ci_age_days = run_age.days
        data.ci_run_recent = False
        return

    data.ci_run_recent = True


def evaluate_criteria(repo, number, data):
    print(f"process: {number}")

    pr = data.pr
    author = pr.user.login
    labels = [l.name for l in pr.labels]
    assignees = [a.login for a in pr.assignees]
    rebaseable = pr.rebaseable
    hotfix = HOTFIX_LABEL in labels
    trivial = TRIVIAL_LABEL in labels
    override_required = OVERRIDE_REQUIRED_LABEL in labels

    for label in labels:
        if "DNM" in label:
            data.dnm = True
            break

    if rebaseable is None:
        print(f"re-fetch: {number}")
        pr = repo.get_pull(number)
        rebaseable = pr.rebaseable

    approvers = set()
    reviews = {}
    for review in data.pr.get_reviews():
        reviews[review.id] = review
        if review.user:
            if review.state == 'APPROVED':
                approvers.add(review.user.login)
            elif review.state in ['DISMISSED', 'CHANGES_REQUESTED']:
                approvers.discard(review.user.login)

    assignee_approved = False

    if (hotfix or
        not assignees or
        author in assignees):
        assignee_approved = True

    for approver in approvers:
        if approver in assignees:
            assignee_approved = True

    dismissed = False

    reference_time = pr.created_at
    for event in data.pr.get_issue_events():
        if event.event == 'ready_for_review':
            reference_time = event.created_at
        elif event.event == 'review_dismissed':
            dismissed_review = event.dismissed_review
            review = reviews[dismissed_review['review_id']]

            # Do not trigger for approval dismissal via push.
            if ('dismissal_commit_id' not in dismissed_review and
                dismissed_review['state'] == 'changes_requested' and
                event.actor.login != review.user.login and
                review.user.login not in approvers):
                dismissed = True

    now = datetime.datetime.now(UTC)

    delta = now - reference_time.astimezone(UTC)
    delta_hours = int(delta.total_seconds() / 3600)
    delta_biz_hours = calc_biz_hours(reference_time.astimezone(UTC), delta)

    if hotfix:
        time_left = 0
    elif trivial:
        time_left = REVIEW_WINDOW_TRIVIAL_HOURS - delta_hours
    else:
        time_left = REVIEW_WINDOW_BIZ_HOURS - delta_biz_hours

    set_ci_age_data(repo, data)

    data.assignee = assignee_approved
    data.approvers = approvers
    data.time = time_left <= 0
    data.time_left = time_left
    data.rebaseable = rebaseable
    data.hotfix = hotfix
    data.trivial = trivial
    data.override_required = override_required
    data.dismissed = dismissed

    data.debug = [number, author, assignees, approvers, delta_hours,
                  delta_biz_hours, time_left, rebaseable, hotfix, trivial,
                  override_required, data.ci_run_recent, dismissed]


def merge_status(data):
    if data.rebaseable is False or not data.assignee:
        return "blocked"
    if not data.time:
        return "waiting"
    if data.rebaseable is None:
        return "unknown"
    return "ready"


GATE_ICONS = {
    ("conflict", "pass"): "git-merge",
    ("conflict", "fail"): "git-merge-conflict",
    ("conflict", "unknown"): "circle-question-mark",
    ("approval", "pass"): "user-round-check",
    ("approval", "fail"): "user-round-x",
    ("review", "pass"): "clock-check",
    ("review", "wait"): "clock-3",
}


def gate_icon(gate, state, title, text=""):
    label = html.escape(title, quote=True)
    visible_text = (f'<span class="gate-num">{html.escape(str(text))}</span>'
                    if text else "")
    icon = GATE_ICONS[(gate, state)]
    return (f'<span class="gate-icon gate-{gate} gate-{state}" '
            f'role="img" aria-label="{label}" title="{label}">'
            f'<i data-lucide="{icon}" aria-hidden="true"></i>'
            f'{visible_text}</span>')


def gh_user(login):
    escaped = html.escape(login)
    attr = html.escape(login, quote=True)
    return (f'<a href="https://github.com/{escaped}" '
            f'target="_blank" title="{attr}">{escaped}</a>')


def table_entry(number, data):
    pr = data.pr
    status = merge_status(data)
    url = html.escape(pr.html_url, quote=True)
    title = html.escape(pr.title)
    author = gh_user(pr.user.login)
    assignees = ', '.join(gh_user(a.login)
                          for a in sorted(pr.assignees,
                                          key=lambda user: user.login))
    approvers = ', '.join(gh_user(login) for login in sorted(data.approvers))

    base = html.escape(pr.base.ref)
    base_attr = html.escape(pr.base.ref, quote=True)
    milestone = html.escape(pr.milestone.title) if pr.milestone else ""

    if data.rebaseable is None:
        conflict = gate_icon(
            "conflict", "unknown",
            "Mergeability has not been reported by GitHub yet")
        conflict_search = "mergeability checking unknown"
    elif data.rebaseable:
        conflict = gate_icon("conflict", "pass", "No merge conflicts")
        conflict_search = "conflict-free no merge conflicts"
    else:
        conflict = gate_icon(
            "conflict", "fail", "Has merge conflicts: needs a rebase")
        conflict_search = "merge conflict rebase needed"

    if data.assignee:
        approval = gate_icon(
            "approval", "pass",
            "Approved by an assignee, or no assignee approval required")
        approval_search = "assignee approval passed approved"
    else:
        approval = gate_icon(
            "approval", "fail", "No approval from an assignee yet")
        approval_search = "assignee approval missing"

    if data.time:
        review_time = gate_icon(
            "review", "pass", "The minimum review window has elapsed")
        review_search = "review time elapsed"
    else:
        remaining = f"{data.time_left}h"
        review_time = gate_icon(
            "review", "wait", f"Review time remaining: {remaining}",
            remaining)
        review_search = f"review time waiting {remaining}"

    gate_search = html.escape(
        f"{status} {conflict_search} {approval_search} {review_search}",
        quote=True)
    readiness = (f'<td class="gate" data-search="{gate_search}">'
                 f'<span class="gate-icons">{conflict}{approval}'
                 f'{review_time}</span></td>')

    tags = []
    if data.hotfix:
        tags.append("<span class='tag tag-hotfix'>hotfix</span>")
    if data.trivial:
        tags.append("<span class='tag tag-trivial'>trivial</span>")
    if data.override_required:
        tags.append('<span class="tag tag-override">override required</span>')
    if not data.ci_run_recent:
        age = f"{data.ci_age_days}d" if data.ci_age_days else "stale"
        tags.append(f'<span class="tag tag-oldci">ci {age}</span>')
    if data.dismissed:
        tags.append('<span class="tag tag-dismissed">review dismissed</span>')
    if data.dnm:
        tags.append('<span class="tag tag-dnm">dnm</span>')
    tags_text = ' '.join(tags)

    return f"""
        <tr data-status="{status}" data-base="{base_attr}">
            <td class="num"><a href="{url}">{number}</a></td>
            <td class="title"><a href="{url}">{title}</a> {tags_text}</td>
            <td class="author">{author}</td>
            <td class="people"><span class="handles">{assignees}</span></td>
            <td class="people"><span class="handles">{approvers}</span></td>
            <td>{base}</td>
            <td>{milestone}</td>
            {readiness}
        </tr>
        """


def detect_feature_freeze_tag(repo):
    latest_version = (0, 0, 0)
    tags = []
    for tag in repo.get_tags():
        match = re.match(r"^v([0-9]+)\.([0-9]+)\.([0-9]+)", tag.name)
        if not match:
            continue

        tag_version = tuple(map(int, match.groups()))
        if tag_version[2] != 0:
            continue

        tags.append(tag.name)

        if tag_version > latest_version:
            latest_version = tag_version

    latest_tag = "v%d.%d.%d" % latest_version
    if latest_tag in tags:
        return False, latest_tag

    return True, latest_tag


def run_twister_not_found(runs):
    for run in runs:
        if run.name == "Run tests with twister":
            return False
    return True


def run_twister_canceled(runs):
    for run in runs:
        if run.name == "Run tests with twister" and run.conclusion == "cancelled":
            return True
    return False


CI_ICONS = {
    "ci-pass": "check",
    "ci-fail": "x",
    "ci-cancelled": "ban",
    "ci-running": "refresh-cw",
}


def ci_badge(css, url, text):
    icon = CI_ICONS[css]
    return (f'<a class="ci-badge {css}" href="{html.escape(url, quote=True)}">'
            f'<i data-lucide="{icon}" aria-hidden="true"></i>'
            f'{html.escape(text)}</a>')


def get_ci_status(repo):
    commit = repo.get_branch('main').commit
    runs = repo.get_workflow_runs(branch="main", event="push", head_sha=commit.sha)

    if run_twister_canceled(runs):
        print(f"twister run canceled on {commit.sha}")
        search_commit = commit
        for i in range(10):
            search_commit = search_commit.parents[0]
            print(f"try {search_commit.sha}")
            search_runs = repo.get_workflow_runs(branch="main", event="push", head_sha=search_commit.sha)

            if run_twister_not_found(search_runs) or run_twister_canceled(search_runs):
                continue

            print(f"using commit {search_commit.sha}")
            runs = search_runs
            break

    status = []
    runs_data = []
    for run in runs:
        html_url = run.html_url
        name = run.name

        if name in CI_IGNORE:
            continue

        if run.status == "completed":
            if run.conclusion == "success":
                status.append(ci_badge("ci-pass", html_url, name))
                runs_data.append({"name": name, "status": "pass"})
            elif run.conclusion == "failure":
                status.append(ci_badge("ci-fail", html_url, name))
                runs_data.append({"name": name, "status": "fail"})
            elif run.conclusion == "cancelled":
                status.append(ci_badge("ci-cancelled", html_url, name))
                runs_data.append({"name": name, "status": "cancelled"})
            else:
                print(f"ignoring conclusion: {run.conclusion}")
        elif run.status in ["in_progress", "queued", "waiting", "pending"]:
            delta = datetime.datetime.now(UTC) - run.run_started_at.astimezone(UTC)
            delta_mins = int(delta.total_seconds() / 60)
            jobs = list(run.jobs())
            total = len(jobs)
            completed = sum(1 for j in jobs if j.status == "completed")
            status.append(ci_badge(
                "ci-running", html_url,
                f"{name} {completed}/{total} · {delta_mins}m"))
            runs_data.append({"name": name, "status": "running"})
        else:
            print(f"ignoring status: {run.status}")

    with open(CI_JSON_OUT, "w") as f:
        json.dump({"runs": runs_data}, f, indent=4)

    if not status:
        return '<span class="muted">no data</span>'
    else:
        return ' '.join(sorted(status))


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("-o", "--org", default="zephyrproject-rtos",
                        help="Target Github organisation")
    parser.add_argument("-r", "--repo", default="zephyr",
                        help="Target Github repository")
    parser.add_argument("--self", default=None, help="Self repository path")

    return parser.parse_args(argv)


QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 50, states: OPEN, after: $cursor) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        isDraft
        milestone {
          title
        }
        labels(first: 30) {
          nodes {
            name
          }
        }
        reviewDecision
        statusCheckRollup {
          state
        }
      }
    }
  }
}
"""

def get_prs(gh, org, repo):
    variables = {
            "owner": org,
            "name": repo,
            "cursor": None,
    }

    all_prs = []
    has_next_page = True

    while has_next_page:
        _, resp = gh.requester.graphql_query(QUERY, variables)

        prs = resp["data"]["repository"]["pullRequests"]

        all_prs.extend(prs["nodes"])
        has_next_page = prs["pageInfo"]["hasNextPage"]
        variables["cursor"] = prs["pageInfo"]["endCursor"]

        print(f"query: {len(all_prs)} PRs")

    return all_prs

def we_dont_care(pr):
    try:
        if pr["isDraft"]:
            return True

        override_required = False
        for label in pr["labels"]["nodes"]:
            if "DNM" in label["name"]:
                return True
            if label["name"] == OVERRIDE_REQUIRED_LABEL:
                override_required = True

        if pr['reviewDecision'] != "APPROVED":
            return True

        status_check_rollup = pr["statusCheckRollup"]
        ci_state = status_check_rollup["state"] if status_check_rollup else None
        if ci_state != "SUCCESS" and not override_required:
            return True
    except Exception as e:
        print(f"data error, skipping: {e}, {pr}")
        return True

    return False

def main(argv):
    args = parse_args(argv)

    auth = github.Auth.Token(os.environ.get('GITHUB_TOKEN', None))
    gh = github.Github(auth=auth, per_page=PER_PAGE)

    print_rate_limit(gh, args.org)

    pr_data = {}

    repo = gh.get_repo(f"{args.org}/{args.repo}")
    freeze_mode, latest_tag = detect_feature_freeze_tag(repo)
    print(f"Latest tag: {latest_tag}, freeze mode: {freeze_mode}")

    ci_status = get_ci_status(repo)
    print(f"CI status: {ci_status}")

    all_prs = get_prs(gh, args.org, args.repo)

    with gzip.open(PR_JSON_OUT, "wt") as f:
        json.dump(all_prs, f, indent=4)

    for pr_raw in all_prs:
        if we_dont_care(pr_raw):
            continue

        number = pr_raw["number"]
        milestone = pr_raw["milestone"]

        if freeze_mode and milestone and milestone["title"] > latest_tag:
            print(f"ignoring: {number} milestone={milestone['title']} > {latest_tag}")
            continue

        print(f"fetch: {number}")
        pr = repo.get_pull(number)

        if not (pr.base.ref == "main" or
                (pr.base.ref.startswith("v") and pr.base.ref.endswith("-branch"))):
            print(f"ignoring: {number} ref={pr.base.ref}")
            continue

        pr_data[number] = PRData(pr_raw=pr_raw, pr=pr)

    for number, data in pr_data.items():
        evaluate_criteria(repo, number, data)

    with open(HTML_PRE) as f:
        html_out = f.read()
        timestamp = datetime.datetime.now(UTC).isoformat()

    debug_headers = ["number", "author", "assignees", "approvers",
                     "delta_hours", "delta_biz_hours", "time_left", "Mergeable",
                     "Hotfix", "Trivial", "Override Required", "Dismissed"]
    debug_data = []
    for _, data in pr_data.items():
        debug_data.append(data.debug)
    print(tabulate.tabulate(debug_data, headers=debug_headers))

    status_order = {"ready": 0, "waiting": 1, "unknown": 2, "blocked": 3}

    def sort_key(item):
        number, data = item
        status = merge_status(data)
        wait = data.time_left if status == "waiting" else 0
        return (status_order[status], wait, -number)

    for number, data in sorted(pr_data.items(), key=sort_key):
        html_out += table_entry(number, data)

    with open(HTML_POST) as f:
        html_out += f.read()

    html_out = html_out.replace("{{UPDATE_TIMESTAMP}}", timestamp)
    html_out = html_out.replace("{{CI_STATUS}}", ci_status)
    html_out = html_out.replace("{{REVIEW_WINDOW_BIZ_HOURS}}",
                                str(REVIEW_WINDOW_BIZ_HOURS))
    html_out = html_out.replace("{{REVIEW_WINDOW_TRIVIAL_HOURS}}",
                                str(REVIEW_WINDOW_TRIVIAL_HOURS))

    if freeze_mode:
        phase_text = "Feature freeze"
        phase_detail = f"next release: {latest_tag}"
        phase_hint = ("Only bug fixes and release-blocking changes are "
                      "merged until the release is tagged")
    else:
        phase_text = "Integration"
        phase_detail = f"latest release: {latest_tag}"
        phase_hint = "New features and fixes are merged normally"
    html_out = html_out.replace("{{RELEASE_PHASE}}", phase_text)
    html_out = html_out.replace("{{RELEASE_PHASE_DETAIL}}", phase_detail)
    html_out = html_out.replace("{{RELEASE_PHASE_HINT}}", phase_hint)

    if args.self:
        html_out = html_out.replace("{{REPOSITORY_PATH}}", args.self)

    with open(HTML_OUT, "w") as f:
        f.write(html_out)

    print_rate_limit(gh, args.org)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
