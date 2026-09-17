"""Enrol a campaign's APPROVED leads into the matching Apollo sequences.

This is the real multi-touch sender: Apollo runs the 3-step sequence from the
account's own mailboxes (Instantly is not needed). Safety rails, in order:

  1. The profile [sequences].enabled must be true, otherwise everything is a dry run.
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
from profile import load_profile


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


def _sequence_for(seqs, lead: dict) -> str | None:
    offer = lead.get("recommended_offer")
    cats = lead.get("sensitive_data_categories") or []
    if isinstance(cats, str):
        try:
            cats = json.loads(cats)
        except (json.JSONDecodeError, TypeError):
            cats = [cats] if cats else []
    sensitive = bool([c for c in cats if c and c != "none"])
    return seqs.sequence_for(offer, sensitive=sensitive)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--campaign", type=int, help="campaign id (its review-queue session is used)")
    g.add_argument("--session", type=int, help="analysis session id")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing on Apollo")
    ap.add_argument("--limit", type=int, default=0, help="enrol at most N leads this run")
    ap.add_argument("--all-personalised", action="store_true",
                    help="give every lead its Personal Line (no control arm)")
    args = ap.parse_args()

    seqcfg = load_profile().sequences
    live = bool(seqcfg and seqcfg.enabled) and not args.dry_run
    if not live and not args.dry_run:
        print("[enroll] profile [sequences].enabled is false -> forcing dry run.")
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
        # A/B arms, assigned deterministically by lead id so a re-run keeps the
        # same split, and balanced WITHIN each offer so the arms are comparable.
        share = 1.0 if args.all_personalised else float(seqcfg.personalised_share or 0)
        by_offer: dict[str, list[dict]] = {}
        for l in ok:
            by_offer.setdefault(l.get("recommended_offer") or "none", []).append(l)
        for offer, group in by_offer.items():
            group.sort(key=lambda x: x["id"])
            for i, l in enumerate(group):
                usable = l.get("personal_line_status") == "ok" and (l.get("personal_line") or "").strip()
                l["campaign_arm"] = "personalised" if (usable and (i % 100) < round(share * 100)) else "control"

        plan: dict[str, list[dict]] = {}
        no_sequence: list[dict] = []
        for l in ok:
            sid = _sequence_for(seqcfg, l)
            (plan.setdefault(sid, []) if sid else no_sequence).append(l)

        print(f"[enroll] session {session_id}: {len(ok)} eligible, {len(skipped)} skipped, "
              f"{len(no_sequence)} with no sequence for their offer")
        for sid, ls in plan.items():
            print(f"  sequence {sid}: {len(ls)} lead(s)")
            for l in ls:
                arm = l.get("campaign_arm", "control")
                line = (l.get("personal_line") or "") if arm == "personalised" else ""
                print(f"    - {l['id']:>5} [{arm:<12}] {l.get('email'):<36} {(l.get('company_name') or '')[:26]:<26} {line[:70]}")
        for l in no_sequence:
            print(f"    ! {l['id']:>5} {l.get('email'):<40} offer={l.get('recommended_offer')} -> no sequence configured")
        by_reason: dict[str, int] = {}
        for _, r in skipped:
            by_reason[r.split(":")[0]] = by_reason.get(r.split(":")[0], 0) + 1
        if by_reason:
            print("  skipped:", ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())))
        if dry:
            print("[enroll] DRY RUN — nothing sent. Set profile [sequences].enabled = true and rerun without --dry-run.")
            return 0

        account_id = apollo_client.email_account_id_for(seqcfg.send_from_email)
        field_id = (seqcfg.personal_line_field_id or "").strip()
        enrolled: list[dict] = []
        for sid, ls in plan.items():
            contact_ids = []
            for l in ls:
                # The Personal Line is only ever the stored, guard-passed one.
                # Control-arm leads and leads without a usable line get no
                # field value, so the sequence sends its generic opener.
                line = l.get("personal_line") if (l.get("campaign_arm") == "personalised"
                                                  and l.get("personal_line_status") == "ok") else None
                c = apollo_client.create_contact(l, first_line=line, custom_field_id=field_id if line else None)
                cid = c["id"]
                if line and field_id and not (c.get("typed_custom_fields") or {}).get(field_id):
                    # Contact already existed: create_contact reused it, so set
                    # the field explicitly rather than trusting the create call.
                    try:
                        apollo_client.update_contact_custom_field(cid, field_id, line)
                    except Exception as exc:
                        print(f"    ! could not set Personal Line on {l.get('email')}: {exc}")
                contact_ids.append(cid)
                l["_contact_id"] = cid
            apollo_client.add_contacts_to_sequence(sid, contact_ids, send_email_from_email_account_id=account_id)
            enrolled.extend(ls)
            n_pers = sum(1 for l in ls if l.get("campaign_arm") == "personalised")
            print(f"  enrolled {len(ls)} in {sid} ({n_pers} personalised, {len(ls)-n_pers} control)")

        # Same guarantees as Ship: never contact twice, remember what went out.
        dncmod.add_many_from_leads(conn, [{"email": l["email"], "domain_normalized": l.get("domain_normalized")}
                                          for l in enrolled], reason="apollo_sequence_enrolled")
        dbmod.record_export(conn, [l["id"] for l in enrolled], session_id=session_id)
        for l in enrolled:
            conn.execute("UPDATE leads SET email_status = 'enrolled_apollo', email_provider = 'apollo', "
                         "campaign_arm = ?, email_sent_at = ? WHERE id = ?",
                         (l.get("campaign_arm", "control"), dbmod._now(), l["id"]))
        conn.commit()
        print(f"[enroll] done: {len(enrolled)} lead(s) enrolled from {seqcfg.send_from_email}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
