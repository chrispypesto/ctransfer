# ctransfer Terms of Use

**Version 1.2. Last revised: October 12, 2026.**

ctransfer (the "**Service**") is owned and operated by Christopher Pestano, an individual ("**we**," "**us**," or "**our**"). The Service consists of the hosted control plane at `api.ctransfer.itschrispy.com`, the cloud virtual machines it starts for you ("**VMs**"), the website at `ctransfer.itschrispy.com` (the "**Site**"), and the `ctransfer` command-line client and Claude Code plugin when used with them.

These Terms of Use ("**Terms**") govern your use of the Service. By signing in to the Service (for example with `ctransfer login`), or by otherwise accessing or using it, you agree to these Terms on behalf of yourself or the entity you represent, and you confirm that you have the authority to do so. You must be at least 18 years old to use the Service. If you do not agree to these Terms, do not use the Service.

**IMPORTANT: PLEASE READ SECTION 13 CAREFULLY.** It contains an agreement to resolve disputes through binding individual arbitration instead of in court, and includes a waiver of class action rights and jury trial rights. You have 30 days to opt out of the arbitration agreement, as described in Section 13.

## 1. Accounts

1. **Creating an Account.** You sign in with your GitHub account. You agree that the GitHub account you use is your own, and you may hold only one ctransfer account. You can delete your account at any time with `ctransfer account delete`. We may suspend or terminate your account as described in Section 10.
2. **Account Security.** You are responsible for keeping your sign-in token, the SSH keys the client creates, and any credentials you store with the Service confidential, and for all activity that occurs under your account. If you believe your account has been accessed without your authorization, notify us immediately at chris@itschrispy.com. We are not liable for any losses resulting from your failure to keep your credentials secure.

## 2. The Service

1. **What it does.** The Service copies a Claude Code session you choose (its transcript, checkpoints, project memory, your Claude Code configuration and the project's files) to a VM, runs Claude Code there on your behalf, and copies the result back when you ask, when the VM is idle or old, or when a limit is reached. It can also run tasks you schedule.
2. **Free tier and limits.** The Service has a free tier, subject to usage limits (such as the number of VMs, launches per day, VM hours per month and bundle sizes) that `ctransfer usage` shows. We may change these limits, pause new launches, or bring running VMs home at any time, including to stay within our own budget. VMs are temporary: each is deleted after its session is brought home or ended.
3. **Pro plan and credit.** The Pro plan is a monthly subscription with its own, higher limits and larger VMs. You can buy it, add credit and manage it only on the Site's account page.
    1. *Who you pay.* Payments are processed by Stripe, which sells the Pro plan and credit to you as merchant of record ("Sold through Link"). Stripe's terms apply to the purchase, Stripe collects any sales tax or VAT, and Stripe's Link service sends your receipts and handles payment questions, refunds and disputes.
    2. *Credit.* Each paid month includes the amount of VM usage shown on the Site, which ends with that month and does not carry over. Credit you add (top-ups) does not expire while your account exists, but it can be spent only while you are on the Pro plan: if you cancel, it stays on your account and is used again if you subscribe again. Pro VMs are charged from your credit for every hour, or part of an hour, they exist, at the hourly rate for their size shown on the Site when the VM was launched; the month's included credit is used first. When your credit can no longer pay for the next hour, the Service brings your Pro VMs home and launches no more until you add credit, and an hour already started may take your balance slightly below zero, which comes out of your next credit. If a payment for credit is refunded or disputed, we take back the credit it bought, in proportion to the amount refunded or disputed, even if that leaves your balance below zero; Pro VMs are then not launched until credit you add brings it back above the cost of an hour, and if a dispute ends with the payment kept, the credit is returned. Credit has no cash value, cannot be transferred, and is not refundable except where required by law or where Stripe issues a refund.
    3. *Renewal and cancellation.* The subscription renews automatically each month at the price shown when you subscribed until you cancel it, which you can do at any time on the Site's account page; the Pro plan then lasts until the end of the month you paid for, and credit you added stays on your account as described above. We will tell you before changing the price or the hourly rates, and changes apply from your next renewal or to VMs launched afterwards. Deleting your account cancels the subscription at once, and any credit left is lost.
4. **Changes to the Service.** We may modify, suspend, or discontinue the Service (or any part of it) at any time, with or without notice. We are not liable to you or any third party for any such modification, suspension, or discontinuation.
5. **No Support Obligation.** We have no obligation to provide you with support or maintenance for the Service.
6. **Open-source client.** The `ctransfer` client and plugin are licensed under the MIT License, and your use of their source code is governed by that license. These Terms govern your use of the hosted Service.

## 3. Your Content

1. **Ownership.** "**Your Content**" means everything you or your sessions send to the Service: session transcripts, code and other files, configuration, prompts, schedule definitions, the results of runs, and the credentials you store. You keep all rights in Your Content.
2. **License to us.** You grant us a limited, worldwide, non-exclusive, royalty-free license to host, copy, transmit and process Your Content only as needed to operate the Service for you (for example, to store a bundle, give it to your VM, and return the VM's export to you), to keep the Service secure, and to comply with law. This license ends when Your Content is deleted from the Service, as described in the Privacy Policy.
3. **Your responsibility.** You are responsible for Your Content and confirm that you have the rights needed to upload it and to let the Service process it. Do not upload content you are not allowed to share with a cloud provider. Credential-looking files are skipped by the client on a best-effort basis only; you remain responsible for what a session contains.
4. **No backup.** The Service is not a backup or storage service. Bundles are deleted automatically after a short period (see the Privacy Policy), and a VM's changes that are not brought home are lost when the VM is deleted. Keep your own copies of anything important.

## 4. Acceptable Use

You may not use the Service, or let anyone use it through your account, to:

1. break any law or regulation, or infringe or misappropriate anyone's rights;
2. mine cryptocurrency, or run workloads unrelated to working on your own software projects with Claude Code;
3. send spam, scan or attack networks or systems, or run open proxies, VPN exit nodes or other relays for third parties;
4. host or distribute malware, or content that is illegal or that exploits or harms minors;
5. evade or exceed usage limits, including by creating or using more than one account;
6. access another user's data, probe or test the Service's security without our written permission, or interfere with or disrupt the Service;
7. resell, sublicense or provide the Service to third parties; or
8. violate the terms of third-party services you use on a VM, including Anthropic's terms for Claude Code.

We may investigate suspected violations and remove content, stop VMs, or suspend accounts without notice to enforce this Section.

## 5. Third-Party Services

1. **Claude Code and Anthropic.** VMs run Claude Code, which is software by Anthropic, PBC, on the Claude account or API key you provide. Your use of Claude Code is governed by your own agreement with Anthropic, and any usage is billed to you under that agreement. ctransfer is not affiliated with, endorsed by, or sponsored by Anthropic.
2. **Other services.** The Service relies on GitHub for sign-in and on Amazon Web Services for hosting, and your sessions may connect to other services you configure (for example, GitHub repositories or MCP servers). We do not control, endorse, or take responsibility for any third-party service. You use them at your own risk, and their own terms and privacy practices apply.
3. **Release.** To the fullest extent permitted by law, you release us and our agents, successors, and assigns from all claims, demands, and damages of any kind arising out of or related to third-party services. If you are a California resident, you waive California Civil Code Section 1542, which provides: "A general release does not extend to claims which the creditor or releasing party does not know or suspect to exist in his or her favor at the time of executing the release, which if known by him or her must have materially affected his or her settlement with the debtor or released party."

## 6. Ownership and Feedback

1. **Ownership.** Except for Your Content and the open-source client, all intellectual property rights in the Service and the Site belong to us or our suppliers. These Terms do not transfer any ownership rights to you. All rights not expressly granted are reserved.
2. **Restrictions.** You may not access the Service in a way that circumvents its authentication or limits, or copy, sell, or commercially exploit the hosted Service itself.
3. **Feedback.** If you share feedback or suggestions about the Service with us, you grant us a perpetual, irrevocable, worldwide, non-exclusive, fully paid, royalty-free license to use that feedback in any manner and for any purpose, without attribution. Please do not submit feedback that you consider proprietary or confidential.

## 7. Privacy

Your use of the Service is also governed by our Privacy Policy, available at <https://ctransfer.itschrispy.com/privacy> and incorporated into these Terms by reference. It describes what we collect, how we use it, and how long we keep it. If there is a conflict between these Terms and the Privacy Policy about personal data, the Privacy Policy controls. The Site does not use cookies or tracking technologies.

## 8. Indemnification

You agree to defend, indemnify, and hold us and our agents harmless from any claims and reasonable costs or attorneys' fees arising out of (i) your use of the Service, (ii) Your Content, (iii) your violation of these Terms, or (iv) your violation of any applicable law or of anyone's rights. We may assume control of the defense of any such claim at your expense, and you agree to cooperate with our defense. You agree not to settle any such claim without our prior written consent. We will make reasonable efforts to notify you promptly of any claim we become aware of.

## 9. Disclaimers and Limitation of Liability

1. **Disclaimers.** THE SERVICE IS PROVIDED "AS IS" AND "AS AVAILABLE." TO THE FULLEST EXTENT PERMITTED BY LAW, WE AND OUR SUPPLIERS DISCLAIM ALL WARRANTIES, EXPRESS OR IMPLIED, INCLUDING WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE, AND NON-INFRINGEMENT. WE DO NOT WARRANT THAT THE SERVICE WILL BE UNINTERRUPTED, ERROR-FREE, OR SECURE, THAT SESSIONS OR FILES WILL BE TRANSFERRED COMPLETELY OR CORRECTLY, OR THAT YOUR CONTENT WILL NOT BE LOST. WHERE APPLICABLE LAW REQUIRES WARRANTIES, THEY ARE LIMITED TO 90 DAYS FROM YOUR FIRST USE.
2. **Limitation of Liability.** TO THE MAXIMUM EXTENT PERMITTED BY LAW: (A) WE AND OUR SUPPLIERS WILL NOT BE LIABLE FOR ANY LOST PROFITS, LOST DATA, COSTS OF SUBSTITUTE PRODUCTS, OR ANY INDIRECT, CONSEQUENTIAL, INCIDENTAL, SPECIAL, EXEMPLARY, OR PUNITIVE DAMAGES ARISING FROM OR RELATED TO THESE TERMS OR YOUR USE OF (OR INABILITY TO USE) THE SERVICE; AND (B) OUR TOTAL LIABILITY TO YOU FOR ANY CLAIM ARISING UNDER THESE TERMS IS CAPPED AT THE GREATER OF (i) $50 USD AND (ii) THE AMOUNT YOU PAID US UNDER THESE TERMS IN THE SIX MONTHS BEFORE THE INCIDENT GIVING RISE TO THE CLAIM. THE EXISTENCE OF MULTIPLE CLAIMS DOES NOT INCREASE THIS CAP.

## 10. Term and Termination

These Terms remain in effect while you use the Service. You may stop using the Service and delete your account at any time. We may suspend or terminate your access (including stopping your VMs and deleting your account) at any time and for any reason, including if we believe you have violated these Terms. We are not liable to you for any such termination. On termination, Sections 3.3, 3.4 and 5 through 13 survive.

## 11. State-Specific Legal Notices

The provisions in this Section apply only to users subject to the laws of the states named below. If a provision in this Section conflicts with another provision of these Terms, the state-specific provision controls for users subject to that state's laws.

1. **California.** Under California Civil Code Section 1789.3, California users are entitled to the following consumer rights notice: the Service is provided by Christopher Pestano. To file a complaint regarding the Service, or to receive further information about its use, contact us at chris@itschrispy.com. You may also contact the Complaint Assistance Unit of the Division of Consumer Services of the California Department of Consumer Affairs in writing at 1625 N. Market Blvd., Suite N112, Sacramento, CA 95834, or by telephone at (800) 952-5210. California residents may have additional rights under the California Consumer Privacy Act; see the Privacy Policy.
2. **Colorado, Connecticut, Virginia and other states with consumer privacy laws.** Residents of these states may have rights under their state's privacy law, including rights to access, correct, delete and obtain a copy of their personal data. We do not sell personal data or use it for targeted advertising or profiling. See the Privacy Policy for how to exercise your rights.
3. **Nevada.** Nevada residents have the right under Nevada Revised Statutes Chapter 603A to direct us not to sell certain information we have collected about them. We do not sell personal information; to make a request anyway, contact chris@itschrispy.com.

## 12. General

1. **Changes to Terms.** We may update these Terms from time to time. If we make material changes, we will notify you by a notice in the client or on the Site, or by email if we have your address. Your continued use of the Service after notice of changes means you accept the updated Terms.
2. **Governing Law.** These Terms and any dispute arising out of or related to these Terms or the Service are governed by the laws of the State of California, without regard to its conflict-of-law principles. For any claim or dispute not subject to the arbitration provisions in Section 13, you and we consent to the exclusive jurisdiction and venue of the state and federal courts located in California. Notwithstanding the foregoing: (a) either party may bring an action in any court of competent jurisdiction for injunctive or other equitable relief to protect its intellectual property rights; and (b) either party may bring an individual action in small claims court for claims within that court's jurisdictional limits.
3. **Export.** You agree not to use the Service in violation of U.S. export control or sanctions laws, or from a country or by a person subject to U.S. sanctions.
4. **Electronic Communications.** By using the Service, you consent to receiving communications from us electronically (in the client, on the Site, or by email). These communications satisfy any legal requirement for written notice.
5. **Accessibility.** We aim for the Site to conform to the Web Content Accessibility Guidelines (WCAG) 2.1, Level AA. If you have difficulty using the Site or the client, contact us at chris@itschrispy.com and we will make reasonable efforts to help.
6. **Entire Agreement.** These Terms (together with the Privacy Policy) are the entire agreement between you and us regarding the Service. If any provision of these Terms is found invalid or unenforceable, it will be modified to the minimum extent necessary to be valid, and the remaining provisions will continue in effect. Our failure to enforce any provision is not a waiver of it. The word "including" means "including without limitation." You may not assign these Terms without our prior written consent; we may assign them freely. These Terms bind any permitted assignees.
7. **Trademarks.** "Claude" and "Claude Code" are trademarks of Anthropic, PBC, and "GitHub" is a trademark of GitHub, Inc.; they are used here only to describe compatibility. You may not use our names or logos without our prior written consent.
8. **Contact.** chris@itschrispy.com

## 13. Dispute Resolution

Please read this section carefully. It affects your legal rights, including your right to sue in court and your right to a jury trial.

1. **Applicability.** Except as described below, you and we agree to resolve all disputes arising out of or relating to the Service or these Terms through binding individual arbitration, not in court. Exceptions: (i) claims that qualify for small claims court, brought on an individual basis; and (ii) requests for equitable relief related to intellectual property. This arbitration agreement applies to all claims, including those that arose before you agreed to these Terms.
2. **Try to Resolve First.** Before starting arbitration, the parties agree to try to resolve the dispute informally. The party raising the dispute must send written notice (an "**Informal Notice**") to the other party; our notice address is chris@itschrispy.com. Within 45 days of receiving an Informal Notice, the parties will meet by phone or video in good faith to try to resolve the dispute. If the dispute is not resolved within 60 days of the Informal Notice, either party may start arbitration.
3. **Arbitration Rules.** Arbitrations will be administered by JAMS (www.jamsadr.com). Claims under $250,000 (excluding fees and interest) will use JAMS' Streamlined Arbitration Rules; larger claims will use JAMS' Comprehensive Arbitration Rules. Unless the parties agree otherwise, arbitration will be conducted in the county where you live. All arbitration materials and documents are confidential.
4. **Arbitration Request.** The arbitration request must include: (i) your contact information and GitHub username; (ii) a description of the claims and supporting facts; (iii) the relief you are seeking and a good-faith damages estimate; (iv) confirmation that you completed the informal resolution process; and (v) proof of any required filing fee payment.
5. **Authority of Arbitrator.** The arbitrator has authority to resolve all arbitrable disputes, including questions about the scope and enforceability of this arbitration agreement, except that courts (not arbitrators) will decide: (i) challenges to the class action waiver below; (ii) disputes about arbitration fees; (iii) whether a condition precedent to arbitration has been satisfied; and (iv) which version of this agreement applies. The arbitrator may award the same relief as a court, but on an individual basis only. The arbitrator's award is final and binding, and judgment may be entered in any court with jurisdiction.
6. **Waiver of Jury Trial.** BY AGREEING TO ARBITRATION, YOU AND WE WAIVE THE RIGHT TO A TRIAL BY JUDGE OR JURY FOR ALL COVERED CLAIMS.
7. **Waiver of Class Actions.** ALL DISPUTES MUST BE BROUGHT ON AN INDIVIDUAL BASIS. NEITHER YOU NOR WE MAY BRING CLAIMS AS A PLAINTIFF OR CLASS MEMBER IN ANY CLASS, REPRESENTATIVE, OR COLLECTIVE PROCEEDING. The arbitrator may only award relief on an individual basis. If a court finds this class action waiver unenforceable as to a specific claim, that claim may be litigated in a state or federal court in California; all other claims remain subject to arbitration.
8. **Attorneys' Fees.** Each party bears its own attorneys' fees unless the arbitrator finds a claim was frivolous or brought for an improper purpose.
9. **Batch Arbitration.** If 100 or more substantially similar arbitration demands are filed against us within a 30-day period by the same law firm or coordinated group, JAMS will batch them into groups of 100 and appoint one arbitrator per batch, with one set of fees per batch.
10. **Opt-Out.** You may opt out of this arbitration agreement within 30 days of first accepting these Terms by emailing chris@itschrispy.com. Your notice must include your name, your GitHub username, and a clear statement that you wish to opt out. Opting out does not affect any other part of these Terms.
11. **Severability.** If any part of this arbitration agreement is found invalid, it will be modified to the minimum extent necessary to make it enforceable; the rest of the agreement remains in effect.

---

These Terms are adapted from a template by General Legal, PC, published under CC0. General Legal has not reviewed them.
