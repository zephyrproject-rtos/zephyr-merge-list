#!/usr/bin/env python3

from github import Github, GithubException
import argparse
import datetime
import json
import os
import sys
import tabulate

token = os.environ["GITHUB_TOKEN"]

PER_PAGE = 100

HTML_OUT = "public/index.html"
HTML_PRE = "index.html.pre"
HTML_POST = "index.html.post"

UTC = datetime.timezone.utc

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

    pr = data['pr']
    author = pr.user.login
    labels = [l.name for l in pr.labels]
    assignees = [a.login for a in pr.assignees]
    mergeable = pr.mergeable
    hotfix = "Hotfix" in labels
    trivial = "Trivial" in labels

    approvers = set()
    for review in data['reviews']:
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
    for event in data['events']:
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


    data['assignee'] = assignee_approved
    data['time'] = time_left <= 0
    data['time_left'] = time_left
    data['mergeable'] = mergeable
    data['hotfix'] = hotfix
    data['trivial'] = trivial

    data['debug'] = [number, author, assignees, approvers, delta_hours, delta_biz_hours,
                     time_left, mergeable, hotfix, trivial]


def table_entry(number, data):
    pr = data['pr']
    url = pr.html_url
    title = pr.title
    author = pr.user.login
    assignees = ', '.join(sorted(a.login for a in pr.assignees))

    approvers_set = set()
    for review in data['reviews']:
        if review.user and review.state == 'APPROVED':
            approvers_set.add(review.user.login)
    approvers = ', '.join(sorted(approvers_set))

    base = pr.base.ref
    if pr.milestone:
        milestone = pr.milestone.title
    else:
        milestone = ""

    PASS = "<span class=approved>&check;</span>"
    FAIL = "<span class=blocked>&#10005;</span>"

    mergeable = PASS if data['mergeable'] else FAIL
    assignee = PASS if data['assignee'] else FAIL
    time = PASS if data['time'] else FAIL + f" {data['time_left']}h left"

    if data['mergeable'] and data['assignee'] and data['time']:
        tr_class = ""
    else:
        tr_class = "draft"

    tags = []
    if data['hotfix']:
        tags.append("H")
    if data['trivial']:
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

    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)

    token = os.environ.get('GITHUB_TOKEN', None)
    gh = Github(token, per_page=PER_PAGE)

    print_rate_limit(gh, args.org)

    pr_data ={}

    query = f"is:pr is:open repo:{args.org}/{args.repo} review:approved status:success -label:DNM draft:false"
    pr_issues = gh.search_issues(query=query)
    for issue in pr_issues:
        number = issue.number
        print(f"fetch: {number}")
        pr = issue.as_pull_request()
        pr_data[number] = {
                'issue': issue,
                'pr': pr,
                'reviews': pr.get_reviews(),
                'events': pr.get_issue_events(),
                }

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
        debug_data.append(data['debug'])
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
