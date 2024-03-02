#!/usr/bin/env python3

from dataclasses import dataclass, field
import argparse
import datetime
import github
import os
import sys
import tabulate

token = os.environ["GITHUB_TOKEN"]

PER_PAGE = 100

HTML_OUT = "public/index.html"
HTML_PRE = "index.html.pre"
HTML_POST = "index.html.post"

PASS = "<span class=approved>&check;</span>"
FAIL = "<span class=blocked>&#10005;</span>"
UNKNOWN = "<span class=unknown>?</span>"

UTC = datetime.timezone.utc


@dataclass
class PRData:
    issue: github.Issue
    pr: github.PullRequest
    assignee: str = field(default=None)
    approvers: set = field(default=None)
    time: bool = field(default=False)
    time_left: int = field(default=None)
    mergeable: bool = field(default=False)
    hotfix: bool = field(default=False)
    trivial: bool = field(default=False)
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


def evaluate_criteria(number, data):
    print(f"process: {number}")

    pr = data.pr
    author = pr.user.login
    labels = [l.name for l in pr.labels]
    assignees = [a.login for a in pr.assignees]
    mergeable = pr.mergeable
    hotfix = "Hotfix" in labels
    trivial = "Trivial" in labels

    approvers = set()
    for review in data.pr.get_reviews():
        if review.user and review.state == 'APPROVED':
            approvers.add(review.user.login)

    assignee_approved = False

    if (hotfix or
        not assignees or
        author in assignees):
        assignee_approved = True

    for approver in approvers:
        if approver in assignees:
            assignee_approved = True

    reference_time = pr.created_at
    for event in data.pr.get_issue_events():
        if event.event == 'ready_for_review':
            reference_time = event.created_at
    now = datetime.datetime.now(UTC)

    delta = now - reference_time.astimezone(UTC)
    delta_hours = int(delta.total_seconds() / 3600)
    delta_biz_hours = calc_biz_hours(reference_time.astimezone(UTC), delta)

    if hotfix:
        time_left = 0
    elif trivial:
        time_left = 4 - delta_hours
    else:
        time_left = 48 - delta_biz_hours

    data.assignee = assignee_approved
    data.approvers = approvers
    data.time = time_left <= 0
    data.time_left = time_left
    data.mergeable = mergeable
    data.hotfix = hotfix
    data.trivial = trivial

    data.debug = [number, author, assignees, approvers, delta_hours,
                  delta_biz_hours, time_left, mergeable, hotfix, trivial]


def table_entry(number, data):
    pr = data.pr
    url = pr.html_url
    title = pr.title
    author = pr.user.login
    assignees = ', '.join(sorted(a.login for a in pr.assignees))
    approvers = ', '.join(sorted(data.approvers))

    base = pr.base.ref
    if pr.milestone:
        milestone = pr.milestone.title
    else:
        milestone = ""

    if data.mergeable is None:
        mergeable = UNKNOWN
    elif data.mergeable == True:
        mergeable = PASS
    else:
        mergeable = FAIL
    assignee = PASS if data.assignee else FAIL
    time = PASS if data.time else FAIL + f" {data.time_left}h left"

    if (data.mergeable is None or data.mergeable == True) and data.assignee and data.time:
        tr_class = ""
    else:
        tr_class = "draft"

    tags = []
    if data.hotfix:
        tags.append("H")
    if data.trivial:
        tags.append("T")
    tags_text = ' '.join(tags)

    return f"""
        <tr class="{tr_class}">
            <td><a href="{url}">{number}</a></td>
            <td><a href="{url}">{title}</a></td>
            <td>{author}</td>
            <td>{assignees}</td>
            <td>{approvers}</td>
            <td>{base}</td>
            <td>{milestone}</td>
            <td>{mergeable}</td>
            <td>{assignee}</td>
            <td>{time}</td>
            <td>{tags_text}</td>
        </tr>
        """


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("-o", "--org", default="zephyrproject-rtos",
                        help="Github organisation")
    parser.add_argument("-r", "--repo", default="zephyr",
                        help="Github repository")
    parser.add_argument("-i", "--ignore-milestones", default="future",
                        help="Comma separated list of milestones to ignore")
    parser.add_argument("-l", "--ignore-labels", default="",
                        help="Comma separated list of labels to ignore")

    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)

    token = os.environ.get('GITHUB_TOKEN', None)
    gh = github.Github(token, per_page=PER_PAGE)

    print_rate_limit(gh, args.org)

    pr_data = {}

    if args.ignore_milestones:
        ignore_milestones = args.ignore_milestones.split(",")
        print(f"ignored milestones: {ignore_milestones}")
    else:
        ignore_milestones = []

    if args.ignore_labels:
        ignore_labels = args.ignore_labels.split(",")
        print(f"ignored labels: {ignore_labels}")
    else:
        ignore_labels = []

    query = f"is:pr is:open repo:{args.org}/{args.repo} review:approved status:success -label:DNM draft:false"
    pr_issues = gh.search_issues(query=query)
    for issue in pr_issues:
        number = issue.number

        if issue.milestone and issue.milestone.title in ignore_milestones:
            print(f"ignoring: {number} milestone={issue.milestone.title}")
            continue

        skip = False
        for label in issue.labels:
            if label.name in args.ignore_labels:
                print(f"ignoring: {number} label={label.name}")
                skip = True
                break
        if skip:
            continue

        print(f"fetch: {number}")
        pr = issue.as_pull_request()
        pr_data[number] = PRData(issue=issue, pr=pr)

    for number, data in pr_data.items():
        evaluate_criteria(number, data)

    with open(HTML_PRE) as f:
        html_out = f.read()
        timestamp = datetime.datetime.now(UTC).strftime("%d/%m/%Y %H:%M:%S %Z")
        html_out = html_out.replace("UPDATE_TIMESTAMP", timestamp)

    debug_headers = ["number", "author", "assignees", "approvers",
                     "delta_hours", "delta_biz_hours", "time_left", "Mergeable",
                     "Hotfix", "Trivial"]
    debug_data = []
    for _, data in pr_data.items():
        debug_data.append(data.debug)
    print(tabulate.tabulate(debug_data, headers=debug_headers))

    for number, data in pr_data.items():
        html_out += table_entry(number, data)

    with open(HTML_POST) as f:
        html_out += f.read()

    with open(HTML_OUT, "w") as f:
        f.write(html_out)

    print_rate_limit(gh, args.org)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
