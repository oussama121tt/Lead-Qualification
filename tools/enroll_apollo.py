"""Enrol a campaign's APPROVED leads into the matching Apollo sequences.

This is the real multi-touch sender: Apollo runs the 3-step sequence from the
account's own mailboxes (Instantly is not needed). Safety rails, in order:

  1. [apollo.sequences].enabled must be true, otherwise everything is a dry run.
  2. Only leads with review_status = APPROVED, not duplicates, not on the
     do_not_contact registry, with an email and a recommended offer.
  3. Every lead already exported/enrolled once (export_history) is skipped.
  4. Offer -> sequence via config: ai_audit (+ sensitive variant), general_audit,
     pipeline. Leads whose offer has no sequence are reported, never sent.
  5. --dry-run prints the exact plan and touches nothing on Apollo.

After enrolment each lead is recorded in do_not_contact + export_history and
its email_status is set to "enrolled_apollo" (the nightly outcomes sync then
picks up sends/opens/replies by email).

    python tools/enroll_apollo.py --campaign 3 --dry-run
    python tools/enroll_apollo.py --campaign 3
    python tools/enroll_apollo.py --session 12 --dry-run     # any session, not only campaigns
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apollo_client
import campaigns as campaignsmod
import db as dbmod
import dnc as dncmod
import export as exportmod
from runconfig import load_config


def _eligible(conn, session_id: int) -> tuple[list[dict], list[tuple[dict, str]]]:
    leads = dbmod.get_leads_with_scores(conn, session_id=session_id)
    dnc_emails, dnc_domains = dncmod.load_sets(conn)
    exported = {r["lead_id"] for r in conn.execute(
        "SELECT lead_id FROM export_history WHERE session_id = ?", (session_id,)).fetchall()}
    ok, skipped = [], []
    for l in leads:
        if l.get("is_duplicate"):
            skipped.append((l, f"duplicate: {l.get('duplicate_reason')}")); continue
        if l.get("review_status") != "APPROVED":
            skipped.append((l, f"not approved ({l.get('review_status') or 'undecided'})")); continue
        if not (l.get("email") or "").strip():
            skipped.append((l, "no email")); continue
        reason = dncmod.check_lead(l.get("email"), l.get("domain_normalized"), dnc_emails, dnc_domains)
        if reason:
            skipped.append((l, reason)); continue
        if l["id"] in exported:
            skipped.append((l, "already exported/enrolled")); continue
        ok.append(l)
    return ok, skipped


def _sequence_for(cfg, lead: dict) -> str | None:
    offer = lead.get("recommended_offer")
    cats = lead.get("sensitive_data_categories") or []
    if isinstance(cats, str):
        try:
            cats = json.loads(cats)
        except (json.JSONDecodeError, TypeError):
            cats = [cats] if cats else []
    sensitive = bool([c for c in cats if c and c != "none"])
    return cfg.apollo.sequences.sequence_for(offer, sensitive=sensitive)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--campaign", type=int, help="campaign id (its review-queue session is used)")
    g.add_argument("--session", type=int, help="analysis session id")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing on Apollo")
    ap.add_argument("--limit", type=int, default=0, help="enrol at most N leads this run")
    args = ap.parse_args()

    cfg = load_config()
    seqcfg = cfg.apollo.sequences
    live = bool(seqcfg and seqcfg.enabled) and not args.dry_run
    if not live and not args.dry_run:
        print("[enroll] [apollo.sequences].enabled is false -> forcing dry run.")
    dry = not live

    conn = dbmod.get_connection()
    try:
        if args.campaign:
            camp = campaignsmod.get(conn, args.campaign)
            if camp is None or not camp.get("session_id"):
                print(f"[enroll] campaign {args.campaign} not found or has no session yet")
                return 2
            session_id = camp["session_id"]
        else:
            session_id = args.session

        ok, skipped = _eligible(conn, session_id)
        if args.limit:
            ok = ok[: args.limit]
        plan: dict[str, list[dict]] = {}
        no_sequence: list[dict] = []
        for l in ok:
            sid = _sequence_for(cfg, l)
            (plan.setdefault(sid, []) if sid else no_sequence).append(l)

        print(f"[enroll] session {session_id}: {len(ok)} eligible, {len(skipped)} skipped, "
              f"{len(no_sequence)} with no sequence for their offer")
        for sid, ls in plan.items():
            print(f"  sequence {sid}: {len(ls)} lead(s)")
            for l in ls:
                print(f"    - {l['id']:>5} {l.get('email'):<40} {l.get('company_name') or '':<32} offer={l.get('recommended_offer')}")
        for l in no_sequence:
            print(f"    ! {l['id']:>5} {l.get('email'):<40} offer={l.get('recommended_offer')} -> no sequence configured")
        by_reason: dict[str, int] = {}
        for _, r in skipped:
            by_reason[r.split(":")[0]] = by_reason.get(r.split(":")[0], 0) + 1
        if by_reason:
            print("  skipped:", ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())))
        if dry:
            print("[enroll] DRY RUN — nothing sent. Set [apollo.sequences].enabled = true and rerun without --dry-run.")
            return 0

        account_id = apollo_client.email_account_id_for(seqcfg.send_from_email)
        enrolled: list[dict] = []
        for sid, ls in plan.items():
            contact_ids = []
            for l in ls:
                first_line = exportmod._first_line_from(l) if hasattr(exportmod, "_first_line_from") else None
                c = apollo_client.create_contact(l, first_line=first_line)
                contact_ids.append(c["id"])
                l["_contact_id"] = c["id"]
            apollo_client.add_contacts_to_sequence(sid, contact_ids, send_email_from_email_account_id=account_id)
            enrolled.extend(ls)
            print(f"  enrolled {len(ls)} in {sid}")

        # Same guarantees as Ship: never contact twice, remember what went out.
        dncmod.add_many_from_leads(conn, [{"email": l["email"], "domain_normalized": l.get("domain_normalized")}
                                          for l in enrolled], reason="apollo_sequence_enrolled")
        dbmod.record_export(conn, [l["id"] for l in enrolled], session_id=session_id)
        for l in enrolled:
            conn.execute("UPDATE leads SET email_status = 'enrolled_apollo', email_provider = 'apollo' WHERE id = ?", (l["id"],))
        conn.commit()
        print(f"[enroll] done: {len(enrolled)} lead(s) enrolled from {seqcfg.send_from_email}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
