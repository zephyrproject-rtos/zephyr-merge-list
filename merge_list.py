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
import time
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

PR_PAGE_SIZE = 100
DETAILS_BATCH_SIZE = 20
REVIEW_PAGE_SIZE = 100
TIMELINE_PAGE_SIZE = 100

REVIEW_WINDOW_BIZ_HOURS = 48
REVIEW_WINDOW_TRIVIAL_HOURS = 4


@dataclass
class PRData:
    pr_raw: dict
    pr: dict
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


RATE = {"cost": 0, "remaining": None}


def graphql_query(gh, query, variables):
    """graphql_query, keeping a running total of what the run has spent.

    Actions GITHUB_TOKEN gets 1000 graphql points per hour per repository,
    rather than the 5000 a user token gets, so it is worth watching.
    """
    _, resp = gh.requester.graphql_query(query, variables)

    rate_limit = resp["data"].get("rateLimit")
    if rate_limit:
        RATE["cost"] += rate_limit["cost"]
        RATE["remaining"] = rate_limit["remaining"]

    return resp


def print_graphql_cost(label, started):
    print(f"{label}: {time.monotonic() - started:.1f}s, "
          f"{RATE['cost']} points spent, {RATE['remaining']} remaining")


def print_rate_limit(gh, org):
    response = gh.get_organization(org)
    for header, value in response.raw_headers.items():
        if header.startswith("x-ratelimit"):
            print(f"{header}: {value}")


def parse_time(value):
    """Parse a graphql DateTime, which is ISO-8601 with a trailing Z."""
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def calc_biz_hours(ref, delta):
    biz_hours = 0

    for hours in range(int(delta.total_seconds() / 3600)):
        date = ref + datetime.timedelta(hours=hours+1)
        if date.weekday() < 5:
            biz_hours += 1

    return biz_hours


def set_ci_age_data(repo, data):
    pr = data.pr
    number = pr["number"]

    pr_age = datetime.datetime.now(UTC) - parse_time(pr["createdAt"])
    if pr_age < datetime.timedelta(days=CI_RUN_MAX_AGE_DAYS):
        print(f"ci age: skip {number}")
        data.ci_run_recent = True
        return

    runs = repo.get_workflow_runs(head_sha=pr["headRefOid"])

    target_run = None
    for run in runs:
        if run.name == CI_RUN_NAME:
            target_run = run
            break

    if not target_run:
        return

    run_age = datetime.datetime.now(UTC) - run.run_started_at
    print(f"ci age: {number}: {run_age} {run.html_url}")
    if run_age > datetime.timedelta(days=CI_RUN_MAX_AGE_DAYS):
        data.ci_age_days = run_age.days
        data.ci_run_recent = False
        return

    data.ci_run_recent = True


def graphql_rebaseable(pr_raw):
    """Return the rebaseable tri-state (True/False/None) from GraphQL data.

    GitHub works out mergeability in the background, so a pull request that has
    not been tested yet reports mergeable=UNKNOWN. canBeRebased is a non-null
    Boolean and reads False during that window, which is indistinguishable from
    a genuine conflict, so only trust it once mergeable has settled.
    """
    if pr_raw["mergeable"] == "UNKNOWN":
        return None

    return pr_raw["canBeRebased"]


def evaluate_criteria(repo, number, data):
    print(f"process: {number}")

    pr = data.pr
    author = pr["author"]["login"] if pr["author"] else None
    labels = [l["name"] for l in pr["labels"]["nodes"]]
    assignees = [a["login"] for a in pr["assignees"]["nodes"]]
    rebaseable = graphql_rebaseable(data.pr_raw)
    hotfix = HOTFIX_LABEL in labels
    trivial = TRIVIAL_LABEL in labels
    override_required = OVERRIDE_REQUIRED_LABEL in labels

    for label in labels:
        if "DNM" in label:
            data.dnm = True
            break

    # Last opinionated review per user wins, so walk them in order. The
    # graphql connection is already chronological, sort anyway so the state
    # machine below does not silently depend on that.
    approvers = set()
    for review in sorted(pr["reviews"]["nodes"], key=lambda r: r["createdAt"]):
        if review["author"]:
            login = review["author"]["login"]
            if review["state"] == 'APPROVED':
                approvers.add(login)
            elif review["state"] in ['DISMISSED', 'CHANGES_REQUESTED']:
                approvers.discard(login)

    assignee_approved = False

    if (hotfix or
        not assignees or
        author in assignees):
        assignee_approved = True

    for approver in approvers:
        if approver in assignees:
            assignee_approved = True

    dismissed = False

    reference_time = parse_time(pr["createdAt"])
    for item in pr["timelineItems"]["nodes"]:
        if item["__typename"] == 'ReadyForReviewEvent':
            reference_time = parse_time(item["createdAt"])
        elif item["__typename"] == 'ReviewDismissedEvent':
            review = item["review"]
            if not review or not review["author"] or not item["actor"]:
                continue
            reviewer = review["author"]["login"]

            # Do not trigger for approval dismissal via push.
            if (item["pullRequestCommit"] is None and
                item["previousReviewState"] == 'CHANGES_REQUESTED' and
                item["actor"]["login"] != reviewer and
                reviewer not in approvers):
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
    url = html.escape(pr["url"], quote=True)
    title = html.escape(pr["title"])
    author = gh_user(pr["author"]["login"]) if pr["author"] else ""
    assignees = ', '.join(
            gh_user(login)
            for login in sorted(a["login"] for a in pr["assignees"]["nodes"]))
    approvers = ', '.join(gh_user(login) for login in sorted(data.approvers))

    base = html.escape(pr["baseRefName"])
    base_attr = html.escape(pr["baseRefName"], quote=True)
    milestone = html.escape(pr["milestone"]["title"]) if pr["milestone"] else ""

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
  rateLimit {
    cost
    remaining
  }
  repository(owner: $owner, name: $name) {
    pullRequests(first: PR_PAGE_SIZE, states: OPEN, after: $cursor) {
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
        baseRefName
        reviewDecision
        statusCheckRollup {
          state
        }
        mergeable
        canBeRebased
      }
    }
  }
}
""".replace("PR_PAGE_SIZE", str(PR_PAGE_SIZE))


def get_prs(gh, org, repo):
    variables = {
            "owner": org,
            "name": repo,
            "cursor": None,
    }

    all_prs = []
    has_next_page = True
    started = time.monotonic()

    while has_next_page:
        resp = graphql_query(gh, QUERY, variables)

        prs = resp["data"]["repository"]["pullRequests"]

        all_prs.extend(prs["nodes"])
        has_next_page = prs["pageInfo"]["hasNextPage"]
        variables["cursor"] = prs["pageInfo"]["endCursor"]

        print(f"query: {len(all_prs)} PRs")

    print_graphql_cost(f"query: {len(all_prs)} PRs", started)

    return all_prs

REVIEW_PAGE_FRAGMENT = """
fragment reviewPage on PullRequestReviewConnection {
  nodes {
    state
    createdAt
    author {
      login
    }
  }
  pageInfo {
    hasNextPage
    endCursor
  }
}
"""

TIMELINE_ITEM_TYPES = "[READY_FOR_REVIEW_EVENT, REVIEW_DISMISSED_EVENT]"

TIMELINE_PAGE_FRAGMENT = """
fragment timelinePage on PullRequestTimelineItemsConnection {
  nodes {
    __typename
    ... on ReadyForReviewEvent {
      createdAt
    }
    ... on ReviewDismissedEvent {
      createdAt
      previousReviewState
      actor {
        login
      }
      pullRequestCommit {
        id
      }
      review {
        author {
          login
        }
      }
    }
  }
  pageInfo {
    hasNextPage
    endCursor
  }
}
"""

PR_DETAILS_FRAGMENT = """
fragment prDetails on PullRequest {
  number
  url
  title
  createdAt
  baseRefName
  headRefOid
  author {
    login
  }
  assignees(first: 20) {
    nodes {
      login
    }
  }
  labels(first: 30) {
    nodes {
      name
    }
  }
  milestone {
    title
  }
  reviews(first: REVIEW_PAGE_SIZE) {
    ...reviewPage
  }
  timelineItems(first: TIMELINE_PAGE_SIZE, itemTypes: TIMELINE_ITEM_TYPES) {
    ...timelinePage
  }
}
""".replace("REVIEW_PAGE_SIZE", str(REVIEW_PAGE_SIZE)) \
   .replace("TIMELINE_PAGE_SIZE", str(TIMELINE_PAGE_SIZE)) \
   .replace("TIMELINE_ITEM_TYPES", TIMELINE_ITEM_TYPES)

PR_ALIAS = """
    prNUMBER: pullRequest(number: NUMBER) {
      ...prDetails
    }"""

DETAILS_QUERY = """
query($owner: String!, $name: String!) {
  rateLimit {\n    cost\n    remaining\n  }\n  repository(owner: $owner, name: $name) {ALIASES
  }
}
""" + PR_DETAILS_FRAGMENT + REVIEW_PAGE_FRAGMENT + TIMELINE_PAGE_FRAGMENT

REVIEWS_PAGE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  rateLimit {
    cost
    remaining
  }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviews(first: REVIEW_PAGE_SIZE, after: $cursor) {
        ...reviewPage
      }
    }
  }
}
""".replace("REVIEW_PAGE_SIZE", str(REVIEW_PAGE_SIZE)) + REVIEW_PAGE_FRAGMENT

TIMELINE_PAGE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  rateLimit {
    cost
    remaining
  }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      timelineItems(first: TIMELINE_PAGE_SIZE, itemTypes: TIMELINE_ITEM_TYPES,
                    after: $cursor) {
        ...timelinePage
      }
    }
  }
}
""".replace("TIMELINE_PAGE_SIZE", str(TIMELINE_PAGE_SIZE)) \
   .replace("TIMELINE_ITEM_TYPES", TIMELINE_ITEM_TYPES) + TIMELINE_PAGE_FRAGMENT


def complete_connection(gh, variables, number, pr, name, query):
    """Page through a connection that did not fit in the batched query."""
    connection = pr[name]

    page_variables = dict(variables, number=number)
    while connection["pageInfo"]["hasNextPage"]:
        print(f"page: {number} {name}")
        page_variables["cursor"] = connection["pageInfo"]["endCursor"]
        resp = graphql_query(gh, query, page_variables)
        connection = resp["data"]["repository"]["pullRequest"][name]
        pr[name]["nodes"].extend(connection["nodes"])

    pr[name]["pageInfo"] = connection["pageInfo"]


def fetch_details_batch(gh, variables, numbers):
    """Run one aliased query, return the nodes that came back keyed by number."""
    aliases = "".join(PR_ALIAS.replace("NUMBER", str(n)) for n in numbers)
    resp = graphql_query(
            gh, DETAILS_QUERY.replace("ALIASES", aliases), variables)

    prs = resp["data"]["repository"]

    return {n: prs[f"pr{n}"] for n in numbers if prs.get(f"pr{n}")}


def get_pr_details(gh, org, repo, numbers):
    """Fetch the per-PR data for the whole merge list in a few queries.

    This replaces the get_pull() + get_reviews() + get_issue_events() REST
    calls that used to run once per pull request, which was three round trips
    each, serially, for every pull request that made it past the filter.
    """
    variables = {"owner": org, "name": repo}
    details = {}
    started = time.monotonic()

    for start in range(0, len(numbers), DETAILS_BATCH_SIZE):
        batch = numbers[start:start + DETAILS_BATCH_SIZE]
        try:
            details.update(fetch_details_batch(gh, variables, batch))
        except Exception as e:
            # A single bad pull request fails the whole aliased query, so
            # retry the batch one at a time rather than losing all of it.
            print(f"details: batch failed, retrying individually: {e}")
            for number in batch:
                try:
                    details.update(fetch_details_batch(gh, variables, [number]))
                except Exception as e:
                    print(f"details: skipping {number}: {e}")

        print(f"details: {len(details)}/{len(numbers)} PRs")

    # Long lived pull requests can overflow a single page of either connection.
    for number, pr in details.items():
        complete_connection(gh, variables, number, pr, "reviews",
                            REVIEWS_PAGE_QUERY)
        complete_connection(gh, variables, number, pr, "timelineItems",
                            TIMELINE_PAGE_QUERY)

    print_graphql_cost(f"details: {len(details)} PRs", started)

    return details


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

    candidates = {}
    for pr_raw in all_prs:
        if we_dont_care(pr_raw):
            continue

        number = pr_raw["number"]
        milestone = pr_raw["milestone"]

        if freeze_mode and milestone and milestone["title"] > latest_tag:
            print(f"ignoring: {number} milestone={milestone['title']} > {latest_tag}")
            continue

        base = pr_raw["baseRefName"]
        if not (base == "main" or
                (base.startswith("v") and base.endswith("-branch"))):
            print(f"ignoring: {number} ref={base}")
            continue

        candidates[number] = pr_raw

    print(f"fetch: {len(candidates)} PRs")
    details = get_pr_details(gh, args.org, args.repo, list(candidates))

    for number, pr_raw in candidates.items():
        if number not in details:
            print(f"ignoring: {number} no detail data")
            continue

        pr_data[number] = PRData(pr_raw=pr_raw, pr=details[number])

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
