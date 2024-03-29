# Zephyr merge list

This script produces a static HTML page with the list of PRs approved and ready
for merge for the main Zephyr project repository. This is meant to be run
periodically using GitHub actions and the output published using GitHub pages.

## Running locally

Create a GitHub access token and set it in the `GITHUB_TOKEN` environment
variable.

Make sure the output directory exists and run the script:

```console
$ mkdir public
$ ./merge_list.py
x-ratelimit-limit: 5000
x-ratelimit-remaining: 4999
x-ratelimit-reset: 1711729218
x-ratelimit-used: 1
x-ratelimit-resource: core
ignored milestones: ['future']
Latest tag: v3.6.0, freeze mode: False
fetch: 70900
fetch: 70883
...
$ ls public/
index.html
```

## Resource usage

The script normally issues 3 RPC per PR (more if the mergeability status is
stale): one for fetching the pull request, one for the reviews and one for the
events. This can limit how often the script can be run before hitting the
GitHub ratelimit. To help identifying potential issues the current rpc quota is
logged at the start and end of the script.
