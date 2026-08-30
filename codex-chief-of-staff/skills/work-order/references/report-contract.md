# Worker report contract

Return these fields at closeout:

```text
WORK ID
STATUS
MODE AND AUTHORITY USED
PROJECT AND REPOSITORY
ENVIRONMENT
BRANCH
HEAD SHA
FILES CHANGED
ACCEPTANCE RESULTS
CHECKS RUN AND RESULTS
REAL-INTERFACE PROOF
ARTIFACTS
DEVIATIONS
BLOCKERS OR GATES
RECOMMENDED NEXT STAGE
CALLBACK DELIVERY
```

Use `none` when a field does not apply. Do not omit a failed check or an unverified criterion. `CALLBACK DELIVERY` names the coordinator task ID and whether `send_message_to_thread` succeeded. A Build report must not claim publication. A Publish report must include the live PR URL and exact pushed head. A Land report must include forge confirmation and the merged commit.
