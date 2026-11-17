# Next work: #1264 slice 2, GitHub activity in the end-of-day goal report

The design is `design-1264.md` (revision 2, gh CLI). Slice 1 is PR #1267.
Slice 2 adds the report integration.

## Do first: #1270, the slice 1 review follow-ups

1. **Pin the restore and rollback wiring with tests.** The calls are at
   `workspace/production.py:903` and `:1072`, and deleting either leaves every
   test green.
2. **Pin the `owner_identity_id` filter in `effective_grants.py:182`** with a
   test that uses two identities.
3. **Decide the lost-identity case before any scheduled collection.** A
   password login without a continuity cookie or recovery code enrolls a new
   identity. The old identity's source stays `active` and collects, but no
   login can see it. Options:
   - authenticated operators of the same workspace can see and stop sources
     owned by any identity;
   - unattended collection requires a live login of the owning identity.

   This doesn't affect local no-login mode. Also fix the panel wording
   "Enroll ownership for this login" so it says to recover existing ownership
   first.
4. **Small fixes:** an ADR-011 sentence on logout; audit lineage for runs the
   operator triggers; precise guide wording for identity revoke.

## Slice 2 scope (from the design)

- **Collection order:** the 21:00 end-of-day job, and the manual report route,
  first await the collector for the report day. Then the report is built from
  the stored rows. There is no second cron.
- **Report keys:** add only `github_activity` and `goal_evidence`. Leave `body`
  and `goal_alignment` untouched. With no source configured, the prompt must be
  byte-identical to today's (golden test).
- **Prompt:** a fenced "GitHub activity" section with `[G#]` ids and no URLs.
  Goals get `[A#]` refs that include their parent goal.
- **Model output:** the model returns a `<goal_evidence>` block. Code then:
  - validates every id;
  - rolls sub-goal evidence up to parent goals;
  - lists the goals with no evidence;
  - strips any URL from the model's text. Links come only from stored rows.
- **Degraded states:** one function produces the status sentence for every
  degraded state. No state fails the report.
- **Also in slice 2:** a reader API, a Reports panel section, and the deferred
  activity kinds `pr_reviewed`, `review_comment` and `issue_comment`.
- **Rules:** tests use `FakeGhRunner` and fake models only, with no network.
  No time gates, sleeps or retries.

## The operator wants

- **Assessment against goals:** "assessing the work against the goals" for
  each goal, with screen and GitHub evidence kept apart. The goals are
  "Create AGI" (with quarterly sub-goals), "Create AGI to benefit humanity" and
  "Family time".
- **Fable 5.2 at high effort** to review the slice 2 PR.
