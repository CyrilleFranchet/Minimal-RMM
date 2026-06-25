# Product Requirements — Minimal-RMM

Living product document for **Minimal-RMM** beyond the technical plan. Implementation details stay in `docs/tech-plan.md` and `docs/progress.md`. API and protocol reference: `README.md`.

---

## Vision

Build a **commercially viable** remote monitoring and management (RMM) platform: operators deploy agents on managed Windows hosts, control them through a server (REST, CLI, web UI, MCP), and rely on stable beacons, file transfer, SOCKS relay, and operator tooling.

Today the codebase is a **lab-grade proof of concept**. Productization requires reliability, operator UX, and — critically — **acceptance on real customer endpoints** that run antivirus and endpoint detection and response (EDR).

---

## Non-negotiable: EDR and antivirus compatibility

**If the RMM is routinely blocked or removed by mainstream EDR/AV, it cannot be sold.**

This is a **gating requirement** for any commercial release, not a nice-to-have. Every major feature (agent, persistence options, SOCKS, exfil, shell execution) must be evaluated against: *Will this get flagged on a typical enterprise workstation?*

### What “success” means

| Criterion | Target |
|-----------|--------|
| Agent process | Runs on EDR-protected Windows hosts without default quarantine or kill |
| Beacon traffic | HTTPS (or operator-approved tunnel) not blocked as malicious C2 in normal lab/customer pilots |
| On-disk artifacts | Agent binary/script and persistence paths not instant generic malware detections |
| Operator actions | Default command patterns avoid unnecessary “recon tool” signatures where quieter alternatives exist (see `ai-skills/windows-ad-recon-stealth.md`) |
| Updates | Signed or reputation-backed deliverables so upgrades do not re-trigger detections |

### Engineering principles (product-level)

1. **Legitimate RMM posture** — Authorized deployment only; document customer consent, MDM/GPO distribution, and code signing roadmap. The product should look and behave like software customers **expect** to install, not like opportunistic malware.
2. **Prefer quiet defaults** — Use standard admin paths (PowerShell with `-EncodedCommand`, ADWS/`Get-AD*` over `net.exe` RPC recon, hidden child processes without console flash) where they reduce noise without hiding malicious intent.
3. **Minimize static signatures** — Configurable URLs, secrets, and session IDs; avoid hard-coded IOCs in shipped artifacts; plan for **Authenticode** (or equivalent) signing of the agent package.
4. **Test against real stacks** — Before claiming “enterprise-ready,” validate in lab against representative AV + EDR combinations (Microsoft Defender, common commercial EDR). Track detections, blocks, and remediation in `docs/progress.md`.
5. **No “bypass for unauthorized use”** — Compatibility work targets **managed, consented** deployments. Documentation and code comments must not frame evasion against systems the operator does not own.

### Out of scope (for this PRD)

- Detailed EDR bypass playbooks or obfuscation kits
- Guarantee of zero detections on all vendors forever (signature and ML models change)
- macOS / Linux agents (Windows-first product)

### Open product work (EDR-related)

- [ ] Code-signing strategy for `client_rmm.ps1` or compiled agent wrapper
- [ ] Lab matrix: Defender + at least one commercial EDR on Windows 10/11
- [ ] Document supported deployment methods (GPO, Intune, manual) and expected exclusions (last resort, customer-owned policy)
- [ ] Review persistence (`__INSTALL_PERSIST__`), SOCKS, and keylog modules for detection impact before commercial packaging
- [ ] Optional: telemetry to server when agent detects self-quarantine or failed beacon due to block (operator visibility)

---

## Target users

| Persona | Needs |
|---------|--------|
| **MSP / IT operator** | Session list, shell, files, SOCKS, AI-assisted ops, session history |
| **Lab / red team (authorized)** | Same stack with opsec-aware defaults and skills |
| **Integrator** | REST + MCP for automation |

---

## Core capabilities (already partially shipped)

See `docs/progress.md` for status. High level:

- Beacon-based Windows agent (`client_rmm.ps1`)
- Operator API, CLI, web UI, MCP
- File download/upload, rclone exfil, screenshots, keylog (lab)
- SOCKS5 relay through agent
- Session history, AI assistant with server skills

---

## Related documents

| Document | Role |
|----------|------|
| `docs/tech-plan.md` | Feature-level design backlog |
| `docs/progress.md` | Completed work, gaps, lab validation notes |
| `docs/web-ai-skills.md` | Operator AI skills (including recon opsec) |
| `ai-skills/windows-ad-recon-stealth.md` | Prefer ADWS over noisy RPC recon |
| `README.md` | Setup and security defaults |

---

## Revision log

| Date | Change |
|------|--------|
| 2026-06-17 | Initial PRD; EDR/AV compatibility declared non-negotiable for commercial sale |
