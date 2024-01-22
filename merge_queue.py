#!/usr/bin/env python3

import argparse
import datetime
import json
import os
import sys
from github import Github, GithubException

token = os.environ["GITHUB_TOKEN"]

PER_PAGE = 100

HTML_OUT = "public/index.html"
HTML_PRE = "index.html.pre"
HTML_POST = "index.html.post"

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
    pr = data['pr']
    author = pr.user.login
    labels = [l.name for l in pr.labels]
    assignees = [a.login for a in pr.assignees]

    reviews = data['reviews']
    approvers = set()
    for review in reviews:
        if review.user and review.state == 'APPROVED':
            approvers.add(review.user.login)

    assignee_approved = False

    if not assignees or author in assignees:
        assignee_approved = True

    for approver in approvers:
        if approver in assignees:
            assignee_approved = True

    utc = datetime.timezone.utc

    reference_time = pr.created_at
    for event in data['events']:
        if event.event == 'ready_for_review':
            # use the first undraft as reference
            reference_time = event.created_at
            break
    now = datetime.datetime.now(utc)

    delta = now - reference_time.astimezone(utc)
    delta_hours = delta.total_seconds() / 3600
    delta_biz_hours = calc_biz_hours(reference_time.astimezone(utc), delta)

    hotfix = "Hotfix" in labels
    trivial = "Trivial" in labels

    enough_time = False
    if hotfix:
        enough_time = True
    elif trivial and delta_hours > 4:
        enough_time = True
    elif delta_biz_hours > 48:
        enough_time = True

    data['assignee'] = assignee_approved
    data['time'] = enough_time
    data['hotfix'] = hotfix
    data['trivial'] = trivial

    print(f"process {number}: {author} {assignees} {approvers} {delta_hours} {delta_biz_hours} {hotfix} {trivial}")


def table_entry(number, data):
    url = data['pr'].html_url
    title = data['pr'].title
    author = data['pr'].user.login
    assignees = ', '.join(a.login for a in data['pr'].assignees)

    approvers_set = set()
    for review in data['reviews']:
        if review.user and review.state == 'APPROVED':
            approvers_set.add(review.user.login)
    approvers = ', '.join(approvers_set)

    base = data['pr'].base.ref

    PASS = "<span class=appproved>&check;</span>"
    FAIL = "<span class=blocked>&#10005;</span>"

    assignee = PASS if data['assignee'] else FAIL
    time = PASS if data['time'] else FAIL

    if data['assignee'] and data['time']:
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
        print(f"fetching: {number}")
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
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%d/%m/%Y %H:%M:%S %Z")
        html_out = html_out.replace("UPDATE_TIMESTAMP", timestamp)

    for number, data in pr_data.items():
        html_out += table_entry(number, data)

    with open(HTML_POST) as f:
        html_out += f.read()

    with open(HTML_OUT, "w") as f:
        f.write(html_out)

    print_rate_limit(gh, args.org)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
