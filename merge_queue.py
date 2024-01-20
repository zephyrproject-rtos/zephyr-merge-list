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


def evaluate_criteria(number, data):
    print(f"process: {number}")

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

    if author in approvers:
        assignee_approved = True

    for approver in approvers:
        if approver in assignees:
            assignee_approved = True

    delta = datetime.datetime.now() - pr.created_at
    delta_hours = delta.total_seconds() / 3600

    # TODO: business time compensation

    enough_time = False
    if "Hotfix" in labels:
        enough_time = True
    elif "Trivial" in labels and delta_hours > 4:
        enough_time = True
    elif delta_hours > 48:
        enough_time = True

    data['assignee'] = assignee_approved
    data['time'] = enough_time


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

    for number, data in pr_data.items():
        url = data['pr'].html_url
        title = data['pr'].title
        author = data['pr'].user.login
        assignees = ', '.join(a.login for a in data['pr'].assignees)

        approvers_list = []
        for review in data['reviews']:
            if review.user and review.state == 'APPROVED':
                approvers_list.append(review.user.login)
        approvers = ', '.join(approvers_list)

        base = data['pr'].base.ref

        PASS = "<span class=appproved>&check;</span>"
        FAIL = "<span class=blocked>&#10005;</span>"

        assignee = PASS if data['assignee'] else FAIL
        time = PASS if data['time'] else FAIL

        html_out += f"""
            <tr>
            <td><a href="{url}">{number}</a></td>
            <td><a href="{url}">{title}</a></td>
            <td>{author}</td>
            <td>{assignees}</td>
            <td>{approvers}</td>
            <td>{base}</td>
            <td>{assignee}</td>
            <td>{time}</td>
          </tr>
          """

    with open(HTML_POST) as f:
        html_out += f.read()

    with open(HTML_OUT, "w") as f:
        f.write(html_out)

    print_rate_limit(gh, args.org)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
