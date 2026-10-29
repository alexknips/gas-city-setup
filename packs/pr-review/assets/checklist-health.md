CUSTOM SCAN INSTRUCTIONS (health-care code: PHI, HIPAA, SOC 2)

Besides the security review above, check the change against this list. It extends the review: an item
here is in scope even when it is not an exploitable vulnerability.

A1. Protected health information (PHI). Ground: HIPAA Security Rule technical safeguards,
45 CFR 164.312: access control (a), audit controls (b), integrity (c), person or entity
authentication (d), transmission security (e).
- Patient data newly stored, or a schema or migration that adds or changes fields holding it:
  who can read it, is it encrypted where the rest is, is access logged.
- Patient data in logs, error messages, exceptions, analytics or telemetry events.
- Patient data in exports (CSV, files, emails) or sent to a third party: analytics, AI/LLM APIs,
  webhooks, CRMs. Name the third party and the fields.
- Writes to an EHR (Athena, Epic, MyChart, FHIR, HL7 and similar): what is written, can it be wrong
  or duplicated, can it be undone, is it logged.
- Real-looking personal data in tests, fixtures or seeds (names with dates of birth, SSNs, phone
  numbers, emails, addresses).

A2. Security and SOC 2. Ground: Trust Services Criteria CC6 (logical access), CC7 (system
monitoring), CC8.1 (change management), plus the security review above.
- Sign-in, sessions, roles, permissions: can a user reach data or actions of another practice,
  patient or role. Missing or weakened authorization checks.
- Secrets and keys: committed, logged, or sent to the client.
- Infrastructure: WAF, IAM, Kubernetes, CDK, Terraform: wider access, public exposure, removed limits.
- CI workflows: permissions, secrets exposure, untrusted input in workflow steps.
- Audit logging: removed, reduced, or missing for a new sensitive action.

A3. Money (billing, plans, credits, pricing): NOT CHECKED. Do not review it and do not imply that you
did. The review states "Not checked: billing, plans, credits, pricing." on its own.

A4. What cannot be undone (very important).
- Destructive migrations (drop, rename, type change that loses data), backfills, bulk deletes or
  updates, and messages sent to patients (SMS, email, push): is there a dry run, a limit, a way back.
- Migration ordering: the PR's migrations must sort after the newest migration on the base branch,
  reuse no sequence number or timestamp, and still apply cleanly on top of the base branch. Rebases
  often break this. Check the migration folder in the working directory against the diff.
- Migrations are reversible, or the PR says why not.

A5. Behaviour patients or practices will notice. Do not guess: ask specific questions, or ask for
evidence (the ticket, screenshots, test output).

A6. No size rule. Review a big PR in full and point at the dangerous hunks.

A7. CI red or skipped, or a serious finding: the outcome is escalate.

B. What every finding must meet.
B1. It names a file and a line, says how it fails, and was checked against the code.
B2. At most 5 findings, worst first. No style nitpicks.
B3. The description matches the diff; behaviour changes have tests; migrations are reversible and in
    order. Report these in `checks`.
B4. One outcome per PR.
