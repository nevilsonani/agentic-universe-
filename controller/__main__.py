"""CLI.

  python -m controller run    --url http://127.0.0.1:8700 [--db runs/ctl/state.db] [--view runs/ctl/supervisor.html]
  python -m controller why    --db runs/ctl/state.db --workflow ks_cancel --at "D3 14:00"
  python -m controller heard  --db runs/ctl/state.db --person ks_cancel:p0001234 [--day 3]
  python -m controller view   --db runs/ctl/state.db [--out supervisor.html] [--serve 8800]
  python -m controller health --db runs/ctl/state.db          # loop health + open alerts, for on-call
"""
from __future__ import annotations

import argparse
import logging
import sys
import time


def main(argv=None):
    ap = argparse.ArgumentParser(prog="controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", required=True)
    r.add_argument("--db", default="runs/controller/state.db")
    r.add_argument("--view", default=None, help="write the supervisor view HTML here every 6 sim-hours")
    r.add_argument("--no-call-log", action="store_true", help="skip the per-call audit table (perf experiments)")
    r.add_argument("--max-ticks", type=int, default=None, help="stop after N ticks (crash tests)")
    r.add_argument("-v", "--verbose", action="store_true")
    w = sub.add_parser("why")
    w.add_argument("--db", required=True)
    w.add_argument("--workflow", required=True)
    w.add_argument("--at", required=True, help='e.g. "D3 14:00"')
    w.add_argument("--json", action="store_true")
    h = sub.add_parser("heard")
    h.add_argument("--db", required=True)
    h.add_argument("--person", required=True)
    h.add_argument("--day", type=int, default=None)
    v = sub.add_parser("view")
    v.add_argument("--db", required=True)
    v.add_argument("--out", default=None)
    v.add_argument("--serve", type=int, default=None)
    he = sub.add_parser("health")
    he.add_argument("--db", required=True)
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if getattr(a, "verbose", False) else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    if a.cmd == "run":
        from .loop import Controller
        t0 = time.time()
        c = Controller(a.url, a.db, store_calls=not a.no_call_log, view_path=a.view, max_ticks=a.max_ticks)
        out = c.run()
        logging.getLogger("controller").info("done in %.0fs: %s", time.time() - t0, out)
    elif a.cmd == "why":
        from .audit import why
        print(why(a.db, a.workflow, a.at, as_json=a.json))
    elif a.cmd == "heard":
        from .audit import heard
        print(heard(a.db, a.person, a.day))
    elif a.cmd == "view":
        from .view import render_html, serve
        if a.serve:
            serve(a.db, a.serve)
        else:
            html = render_html(a.db)
            if a.out:
                open(a.out, "w", encoding="utf-8").write(html)
            else:
                sys.stdout.write(html)
    elif a.cmd == "health":
        from .audit import health
        print(health(a.db))


if __name__ == "__main__":
    main()
