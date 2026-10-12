# ctransfer Privacy Policy

**Effective October 12, 2026.**

This Privacy Policy describes how Christopher Pestano ("**we**," "**us**," or "**our**") collects, uses, shares and deletes personal information in connection with ctransfer: the hosted control plane at `api.ctransfer.itschrispy.com`, the cloud virtual machines it starts for you ("**VMs**"), the website at `ctransfer.itschrispy.com`, and the `ctransfer` client and Claude Code plugin when used with them (together, the "**Service**"). Capitalized terms not defined here have the meaning given in our [Terms of Use](https://ctransfer.itschrispy.com/terms).

**Summary.** We collect what we need to move your Claude Code sessions to a VM and back: your GitHub identity, the sessions you send, and the credentials you choose to store. We do not sell your information, use it for advertising, or use it to train AI models. Bundles are deleted within 7 days, VMs are deleted when their session ends, and `ctransfer account delete` removes the rest. If you are a California resident, see [State privacy rights](#state-privacy-rights). If you are in the European Economic Area or the United Kingdom ("**Europe**"), see [Notice to European users](#notice-to-european-users).

## Personal information we collect

**When you sign in.** You sign in with GitHub. GitHub gives us your numeric GitHub user ID, your username and the date your GitHub account was created (we use it to check the account's age and do not keep it). The GitHub token issued during sign-in is used once to identify you and is not stored.

**Credentials you store.** The Claude token you store with `ctransfer token set` and the secrets you store with `ctransfer secret set` (for example, a GitHub token or API keys). They are stored encrypted, the part of the Service that answers your requests cannot read them back, and they are given only to VMs you launch, when each VM starts.

**Sessions you send (Your Content).** When you push a session, start a new one, or create a schedule, the client uploads a bundle that can contain: the session's transcript (your prompts, Claude's replies and the output of tools it ran), `/rewind` checkpoints, the project's Claude Code memory, the project's files including uncommitted and untracked changes, your Claude Code configuration (`CLAUDE.md`, settings, skills, agents, commands, output styles) and your MCP server configuration. The client skips files that look like credentials and MCP servers that contain literal tokens, on a best-effort basis. When a session comes home, the VM uploads an export bundle with the same kinds of content. Anything in your project or transcript, including personal information about you or others, travels in these bundles.

**Records about your sessions.** For each session: its Claude Code session ID, the path of the project on your machine (which may include your computer's user name), the Claude Code version, the SSH public key the client generated for it, its state and timestamps, bundle sizes, a list of the files in its export, whether Claude was busy or idle, and the VM time it used. For each schedule: its cron expression, prompt and settings, and for each run, Claude's final message (up to 8,000 characters) and any error output.

**While a VM runs.** Your session runs on the VM with Claude Code, which connects directly to Anthropic using the Claude account or API key you provided, and to any other services your session uses. We do not see that traffic. The VM reports to us only whether Claude is busy or idle, and, for scheduled runs, the run's result.

**If you buy the Pro plan or credit.** Stripe, as merchant of record, collects your payment details, email address and billing address at checkout; we never see your card or bank details. Stripe tells us your Stripe customer ID, your subscription's status and renewal date, and the amount of each purchase (without tax). We keep those and your credit balance.

**Communications.** If you email us, we keep your email address and our correspondence.

**Technical information.** Our hosting providers process your IP address and request details in order to deliver the Service. We do not record IP addresses in our records. Our operational logs record events and errors, which may include details of a failed request. The website does not use cookies, analytics or other tracking technologies, and loads no third-party content. When you sign in on the website's account page, your browser keeps your sign-in token in its local storage until you sign out; signing in there sends you to GitHub, and paying sends you to Stripe. The client sends no telemetry; it stores your sign-in token and the SSH keys for your sessions on your own machine.

## How we use personal information

We use personal information to:

- **provide the Service**: sign you in, store and move your sessions, start and stop VMs, give each VM your session and credentials, run your schedules, and show you your sessions and usage;
- **bill for the Pro plan**: know which plan you are on, add the credit you buy, and charge your VM usage against it;
- **enforce limits and keep the Service secure**: apply the free tier's limits, prevent abuse (for example, one account per person), investigate security incidents and violations of the Terms, and keep the Service running within its budget;
- **communicate with you**: answer your messages and tell you about changes to the Service or these policies; and
- **comply with law**: respond to lawful requests and legal process, and protect our rights and those of others.

We do not look at Your Content except when you ask us to (for example, to help with a problem), when needed to investigate abuse or a security incident, or when required by law. We do not sell personal information, share it for cross-context behavioral advertising, use it for profiling, or use it to train AI models.

## How we share personal information

- **Service providers.** Amazon Web Services hosts the Service in the United States (`us-east-1`): storage, databases, encrypted parameter storage, serverless functions, VMs and content delivery. GitHub provides sign-in. Zoho provides our email. They process personal information on our behalf.
- **Stripe.** If you buy the Pro plan or credit, Stripe processes the payment as merchant of record, under its own [privacy policy](https://stripe.com/privacy). We share your GitHub user ID with Stripe so payments reach the right account.
- **Services you use.** Your VMs run Claude Code, which sends your session to Anthropic under your own agreement with Anthropic. Your sessions may also connect to services you configure, such as GitHub repositories or MCP servers. These are your choices, and those services' own privacy policies apply.
- **Authorities and others.** Law enforcement, government authorities and private parties, when we believe in good faith that it is required by law or necessary to protect rights, safety or property.
- **Business transfers.** An acquirer, successor or assignee, in connection with a merger, acquisition, sale of assets or similar transaction, subject to this Privacy Policy.

## Retention

| Information | Kept |
|---|---|
| Push and export bundles | 7 days after upload, then deleted automatically |
| A schedule's snapshot of the session | until you delete the schedule (`ctransfer cron rm`) |
| VMs and what is on them | until the session is brought home or ended; a VM is brought home after 2 hours idle or 24 hours at most |
| Claude token and secrets | until you remove them (`ctransfer token rm`, `ctransfer secret rm`) |
| Your account, session records, schedules and run results | while your account exists |
| Your plan, Stripe customer and subscription IDs, and credit balance | while your account exists |
| Operational logs | 14 days |
| VM infrastructure state (includes your GitHub user ID and session IDs) | up to 30 days after the VM is deleted |
| Emails | as long as needed to handle your request, and as required by law |

**Deleting your account.** `ctransfer account delete` immediately stops your VMs, cancels your Pro subscription, and deletes your bundles, schedules and their snapshots, Claude token, secrets, account record, billing record, session details and run results. It also asks Stripe to delete your Stripe customer; Stripe keeps the records of past payments it must keep as merchant of record. To keep the free tier's limits fair, we keep a record of the VM time each of your sessions used (your GitHub user ID, timestamps, VM hours and bundle sizes, without session details) for 35 days, then delete it. Operational logs and VM infrastructure state expire on the schedule above. You can also ask us to delete your account by emailing chris@itschrispy.com.

## Your choices

- **See your information.** `ctransfer ls`, `ctransfer status`, `ctransfer cron ls`, `ctransfer cron logs`, `ctransfer secret ls` (names only) and `ctransfer usage` show what we hold about your sessions. `ctransfer pull` returns a session's export to you. To get a copy of anything else, email us.
- **Delete information.** `ctransfer rm` ends a session, `ctransfer cron rm` deletes a schedule, `ctransfer token rm` and `ctransfer secret rm` remove credentials, and `ctransfer account delete` deletes your account.
- **Don't send it.** Only sessions you push, start or schedule are uploaded. Keep anything you do not want uploaded out of those projects and sessions.
- **Do Not Track and Global Privacy Control.** We do not track you across sites and do not sell or share personal information, so these signals do not change how we process it.

## Security

Credentials are stored as encrypted parameters that only the part of the Service that hands them to your VMs can read. Bundles are encrypted at rest, transferred over TLS, and reachable only through signed links that expire within an hour. Each VM gets one-time and per-session secrets, and we store only their hashes. No system is perfectly secure, and we cannot guarantee the security of your information.

## Children

The Service is not intended for anyone under 18. If you believe a child has given us personal information, contact us and we will delete it.

## Changes to this Privacy Policy

We may update this Privacy Policy. If we make material changes, we will update the effective date and notify you in the client or on the website, or by email if we have your address. Changes take effect when posted unless we say otherwise.

## How to contact us

Email chris@itschrispy.com with questions or to exercise any privacy right.

## State privacy rights

This section applies to residents of U.S. states whose privacy laws apply to us (including California, Colorado, Connecticut, Virginia and others), to the extent those laws apply.

**Your rights.** Depending on your state, you may have the right to:

- **Know and access**: the categories of personal information we have collected, their sources and purposes, the categories of third parties we disclose it to, and a copy of the personal information we hold about you;
- **Correct** inaccurate personal information;
- **Delete** personal information we have collected from you;
- **Opt out** of the sale or sharing of personal information, targeted advertising and certain profiling. We do none of these;
- **Appeal** our decision on your request; and
- **Not be discriminated against** for exercising these rights.

**How to exercise them.** Use the commands above, or email chris@itschrispy.com from any address with your GitHub username. To verify that the request is yours, we may ask you to confirm it from the signed-in client or through your GitHub account. You may use an authorized agent; we may ask for proof of their authority and to verify your identity directly. To appeal a decision, reply to our response with "Appeal" in the subject.

**Sensitive personal information.** Your account credentials (your sign-in token, Claude token and secrets) are sensitive personal information under California law. We use them only to provide the Service, and not to infer characteristics about you.

**Personal information we collected in the past 12 months:**

| Category (California law) | What | Purpose | Disclosed for a business purpose to | Sold or shared |
|---|---|---|---|---|
| Identifiers | GitHub user ID and username; session IDs; SSH public keys; Stripe customer ID | providing the Service, billing, limits, security | hosting, sign-in and payment providers | no |
| Commercial information | your plan, purchases (amounts without tax) and credit balance | billing | payment provider | no |
| Account credentials (sensitive) | sign-in token; Claude token and secrets you store | providing the Service | hosting provider | no |
| Internet or electronic activity | session states, timestamps, VM usage | providing the Service, limits, security | hosting provider | no |
| Your Content (may include any category, such as names or professional information in your files) | session bundles, prompts, run results | providing the Service | hosting provider | no |
| Communications | emails and their content | support | email provider | no |

We collect this information from you, from your sessions and VMs, and from GitHub. We do not knowingly collect personal information of anyone under 16.

**California "Shine the Light."** We do not disclose personal information to third parties for their direct marketing purposes.

## Notice to European users

This section applies only to individuals in Europe. "Personal information" here means "personal data" under the EU and UK General Data Protection Regulation ("**GDPR**").

**Controller.** Christopher Pestano is the controller of your personal information. Contact: chris@itschrispy.com.

**Legal bases.**

| Purpose | Legal basis |
|---|---|
| Providing the Service (sign-in, moving sessions, running VMs and schedules, storing credentials, billing for the Pro plan) | performance of our contract with you (the Terms) |
| Enforcing limits, preventing abuse, security, keeping VM usage records for 35 days after account deletion | our legitimate interests in operating a fair, secure free service |
| Answering your messages | our legitimate interests, or performance of our contract with you |
| Complying with law and protecting rights | compliance with legal obligations, and our legitimate interests |

**Your rights.** You may ask us to give you access to your personal information, correct it, delete it, give you a copy in a machine-readable format, or restrict its processing, and you may object to processing based on our legitimate interests. Email chris@itschrispy.com; we may need to verify your identity. You also have the right to lodge a complaint with the data protection authority where you live: in the EEA, see <https://edpb.europa.eu/about-edpb/about-edpb/members_en>; in the UK, the Information Commissioner's Office (<https://ico.org.uk>).

**No automated decisions.** We do not make decisions with legal or similarly significant effects about you based solely on automated processing.

**Processing in the United States.** We are based in the United States and the Service is hosted there, so your personal information is processed in the United States, which the EU has not found to provide an equivalent level of data protection for all recipients. We process it there because it is necessary to provide the Service you request from us.

---

This Privacy Policy is adapted from a template by General Legal, PC, published under CC0. General Legal has not reviewed it.
