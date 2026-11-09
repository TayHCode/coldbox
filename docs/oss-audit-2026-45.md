# Weekly OSS audit closeout, 2026-45

Finding `TayHCode/coldbox:secrets_scan:993f72caf269221d363f25b4` reports an IPv4 match in `coldbox`, with SHA-256 fingerprint `4b2228c26597aecab7d5894eb1ec83d915bc2e1a75d758b3b53471ce6aa2c91c`, at audited commit `75d37836d181`.

## Disposition

False positive as a secret. The match is the network address of the standard shared-address CGNAT block in `acl_document`'s blocked networks. It identifies an entire reserved range, not a deployed machine. Removing the exclusion would permit job traffic into that range and weaken isolation.

`release-check.json` already records tree and history approvals for kind `tailnet-ip`, path `coldbox`, fingerprint prefix `4b2228c26597aeca`, with the reason that this is the CGNAT range blocked by the ACL. No additional approval or scanner suppression is needed.

The weekly scanner hashes the matched address, classifies any address in its tailnet range as high severity, and scans added, removed and context lines in Git history. It does not consume these release-check approvals. A source edit cannot remove a historical match from that scanner. No history rewrite, runtime change or public export is part of this closeout.

## Reproduction and verification

Hash the IPv4 matches in `git show 75d37836d181:coldbox` and the current `coldbox`, comparing against the full fingerprint above. The one matching address appears in the blocked-network list in both versions. Confirm the existing tree and history approvals in `release-check.json`.

Run the project verification skill at `.cursor/skills/verify-coldbox/SKILL.md`. The `Acl.test_blocks_lan_tailnet_and_host_but_allows_internet` test asserts that a destination inside the matched range is unreachable. `Acl.test_sync_applies_the_hosts_current_addresses` checks explicit host rejects. The full suite and shellcheck protect the surrounding behavior.

This closeout leaves the public mirror unchanged. An upstream weekly scan can continue reporting the historical match until the audit owner records its false-positive disposition; this document does not claim a clean scan or authorize publication.
