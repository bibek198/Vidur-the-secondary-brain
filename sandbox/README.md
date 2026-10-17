# decision-brain-sandbox

A small public repo that sends real GitHub webhooks to [Decision Brain](https://github.com/) so the real-time engine can be shown live. GitHub only sends webhooks for repos you own, so the demo can't receive pushes from `kubernetes/enhancements` itself.

It holds one file, copied unchanged from `kubernetes/enhancements` as it was on 2026-09-15 (the cutoff of the demo graph):

- `keps/sig-cloud-provider/2699-add-webhook-hosting-to-ccm/kep.yaml` (status: `implementable`)

**The demo re-enacts a real change.** On 2026-09-30, kubernetes/enhancements PR #6440 changed this KEP to `status: withdrawn`. Making the same edit here sends a `push` webhook. Decision Brain diffs the file before and after the push, and the answer to "Is KEP-2699 still planned?" changes within seconds. The earlier answer is flagged as outdated.

Comments on issues in this repo that mention a KEP (for example `KEP-2699`) arrive as `issue_comment` webhooks. They become new discussion for that KEP: the explanation is marked stale and re-extracted.

Only the demo graph (`brain_demo`) receives these events. The live graph follows the real Kubernetes repositories.
