"""`python -m oncall_memory.review` -- the human gate, as a terminal command.

A CLI and not an HTTP endpoint because the endpoint would live in
`packages/oncall-service/.../main.py`, which is P2's file. The review path is
P1's to own end to end, so it is a command in P1's package; an endpoint for the
incident UI is a later ask, and nothing here prevents one -- both would append
to the same log.

WHAT THIS DELIBERATELY CANNOT DO. There is no `--approve-all`, no
`--yes-to-everything` and no way to approve without naming an approver. The
whole value of this gate is that a person read the sentence, and a flag that
approves a list unread would reproduce exactly the failure the gate exists to
prevent while leaving an audit trail that says a person was there.

`reject` requires `--reason`. That is the policy's rule, and the guide wants one
rejected lesson and its reason as a deliverable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .drafting import DraftingUnavailable, model_id, openrouter_drafter
from .proposals import STATUS_FOR, ProposalLog, ProposalLogError
from .reflexion import ReflexionRefused, propose

#: Printed above every pending list. A proposal is not guidance, and the one
#: place a human meets these lessons should say so without being asked.
_BANNER = "proposed lessons -- status `inferred`, NOT applied by the agent until approved"


def _default_approver() -> str:
    return os.environ.get("ONCALL_REVIEWER") or os.environ.get("USER") or ""


def _render(lesson: dict[str, Any], *, verbose: bool = False) -> str:
    lines = [f"{lesson['id']}  [{lesson.get('status', '?')}]  {lesson['statement']}"]
    if verbose:
        pre = lesson.get("preconditions") or {}
        lines.append(f"    service:       {lesson.get('service') or '-'}")
        lines.append(f"    preconditions: service={pre.get('service') or '-'!r}")
        lines.append(f"                   symptom={pre.get('symptom') or '-'!r}")
        lines.append(f"    derived_from:  {lesson.get('derived_from') or '-'}")
        lines.append(f"    evidence:      {', '.join(lesson.get('evidence_refs') or []) or '-'}")
        if lesson.get("reason"):
            lines.append(f"    reason:        {lesson['reason']}")
    return "\n".join(lines)


def _cmd_list(log: ProposalLog, args: argparse.Namespace) -> int:
    rows = log.pending()
    print(_BANNER)
    print(f"log: {log.path}")
    if not rows:
        print("  (nothing pending)")
        return 0
    for lesson in rows:
        print(_render(lesson, verbose=args.verbose))
    return 0


def _cmd_rejected(log: ProposalLog, args: argparse.Namespace) -> int:
    rows = log.rejected()
    print(f"refused lessons, kept as counterexamples ({log.path})")
    if not rows:
        print("  (none)")
        return 0
    for lesson in rows:
        print(_render(lesson, verbose=True))
    return 0


def _cmd_show(log: ProposalLog, args: argparse.Namespace) -> int:
    events = log.history(args.lesson_id)
    if not events:
        print(f"no proposal {args.lesson_id!r} in {log.path}", file=sys.stderr)
        return 1
    # A lesson's CURRENT status comes from its last event, not from the stored
    # row: the decision record deliberately carries the lesson as it was
    # REVIEWED, which still reads `inferred`.
    latest = events[-1]
    print(
        _render(
            {
                **latest["lesson"],
                "status": STATUS_FOR[latest["event"]],
                "reason": latest.get("reason") or "",
            },
            verbose=True,
        )
    )
    print("  history:")
    for row in events:
        who = f" by {row['by']}" if row.get("by") else ""
        why = f" -- {row['reason']}" if row.get("reason") else ""
        print(f"    {row['at']}  {row['event']}{who}{why}")
    return 0


def _cmd_propose(log: ProposalLog, args: argparse.Namespace) -> int:
    run = json.loads(Path(args.run).read_text(encoding="utf-8"))
    drafter = None
    if args.draft_with_model:
        drafter = openrouter_drafter(args.model)
        print(f"drafting the statement with {model_id(args.model)}")
    lesson = propose(run, alert=args.alert, service=args.service, drafter=drafter)
    log.propose(lesson)
    print(_BANNER)
    print(_render(lesson.as_dict(), verbose=True))
    print(f"\nrecorded in {log.path}. Nothing applies it until a person approves it.")
    return 0


def _cmd_approve(log: ProposalLog, args: argparse.Namespace) -> int:
    row = log.approve(args.lesson_id, by=args.by)
    print(f"approved {args.lesson_id} by {row['by']} at {row['at']}")
    print("it is in the playbook now, and playbook_hash has changed")
    return 0


def _cmd_reject(log: ProposalLog, args: argparse.Namespace) -> int:
    row = log.reject(args.lesson_id, by=args.by, reason=args.reason)
    print(f"rejected {args.lesson_id} by {row['by']} at {row['at']}")
    print(f"reason kept with it: {row['reason']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m oncall_memory.review",
        description="Review lessons the reflexion loop proposed. A person decides.",
    )
    parser.add_argument("--log", type=Path, default=None, help="proposal log (default: artifacts/)")
    sub = parser.add_subparsers(dest="command")

    listing = sub.add_parser("list", help="pending proposals")
    listing.add_argument("-v", "--verbose", action="store_true")
    listing.set_defaults(func=_cmd_list)

    refused = sub.add_parser("rejected", help="refused lessons and their reasons")
    refused.set_defaults(func=_cmd_rejected)

    show = sub.add_parser("show", help="one lesson and every decision about it")
    show.add_argument("lesson_id")
    show.set_defaults(func=_cmd_show)

    offer = sub.add_parser("propose", help="propose a lesson from a finished run record")
    offer.add_argument("--run", required=True, help="a RunRecord JSON file")
    offer.add_argument("--alert", required=True, help="the alert text that paged")
    offer.add_argument("--service", required=True, help="the service the alert named")
    offer.add_argument(
        "--draft-with-model",
        action="store_true",
        help="let a model write the sentence (needs OPENROUTER_API_KEY)",
    )
    offer.add_argument("--model", default=None)
    offer.set_defaults(func=_cmd_propose)

    accept = sub.add_parser("approve", help="accept a proposal into the playbook")
    accept.add_argument("lesson_id")
    accept.add_argument("--by", default=_default_approver())
    accept.set_defaults(func=_cmd_approve)

    refuse = sub.add_parser("reject", help="refuse a proposal, with a reason")
    refuse.add_argument("lesson_id")
    refuse.add_argument("--reason", required=True)
    refuse.add_argument("--by", default=_default_approver())
    refuse.set_defaults(func=_cmd_reject)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        args = parser.parse_args([*(argv or []), "list"])
    log = ProposalLog(args.log)
    try:
        return int(args.func(log, args))
    except (ProposalLogError, ReflexionRefused, DraftingUnavailable) as refusal:
        # Refusals are the point of this command, so they print as refusals and
        # exit non-zero rather than as a traceback.
        print(f"refused: {refusal}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read the run record: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
