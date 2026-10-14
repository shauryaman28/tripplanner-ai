"""The eval suite (Phase 33): fifteen trips asked of the planner, and what came back graded.

    scenarios.py   what is asked, and what a right answer looks like
    driver.py      asks it of a running backend, over its API, as a traveller would — and keeps what came back
    measure.py     what can be measured exactly: the searches' parameters, the arithmetic, the dates, the names
    grader.py      what has to be read: Claude Haiku 4.5, given the request, the searches, the plan and the measures
    report.py      a score for each scenario, the table, the history, the verdict

Run with `python scripts/run_eval.py`. Like `loadtest/`, none of this is part of the app: nothing in `src/`
imports it, and the image does not carry it.
"""
