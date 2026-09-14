"""weeks.py, which week a run is for.

Every agent used to compute its own week as date.today().strftime("%G-W%V").
Sunday is ISO weekday 7, the last day of the week that is ending, so a Sunday
build would write W36 while Monday's publish looked for W37. Both would run,
both would exit zero, and the week would produce nothing. That is the same
silent-handoff failure the gates exist to stop, so there is one definition of
it here rather than a copy in each agent.

Build agents ask for target_week: on a Sunday that is tomorrow's week, on any
other day it is today's, so running the chain by hand mid-week still works.
Agents that act on the running week (publish, verify, analyse, report) keep
using today's and must not be changed.
"""

import datetime


def target_week(today=None):
    d = today or datetime.date.today()
    if d.isoweekday() == 7:
        d = d + datetime.timedelta(days=1)
    return d.strftime("%G-W%V")


def current_week(today=None):
    return (today or datetime.date.today()).strftime("%G-W%V")
